"""Toronto Heritage Register source — tiered status + address points.

The CKAN resource ships as a zipped Esri Shapefile (`HRAP_<YYYYMMDD>_OpenData.shp`,
POINT geometry, ~12,327 records, WGS84). Each record is the geocoded centroid of
one listed property's primary street address.

The DBF carries three legally-distinct protection levels under the `STATUS` field:
  - `Part IV`: individually designated by by-law (hard block — demolition prohibited
    without an OMB hearing). Canonical wire value: `"part_iv"`.
  - `Part V`: in a Heritage Conservation District (friction, not blocker — design
    review applies but multiplex conversion is often approvable). Canonical: `"part_v"`.
  - `Listed`: on the watchlist, not legally designated (demolition allowed after a
    60-day notice). Canonical: `"listed"`.

This module reads the SHP + SHX + DBF from the cached zip via `pyshp`, exposes:
  - `STATUS_PART_IV`/`STATUS_PART_V`/`STATUS_LISTED` (canonical wire values)
  - `more_restrictive(a, b)` (tie-break helper for multi-record-per-parcel)
  - `normalize_address(text)` (closed-set street-type normalizer for address-join)
  - `HeritageIndex` (NamedTuple bundling point_tree, points, statuses, addresses,
    address_to_status)
  - `compute_heritage(cache_dir)` returning a populated HeritageIndex

Consumer pattern (see `tools/build_parcels.py:_resolve_heritage_status`):
    idx = compute_heritage(cache)
    # Try address-join first
    status = idx.address_to_status.get(parcel_normalized_address)
    if status is None:
        # Fall back to point-in-parcel
        for i in idx.point_tree.query(parcel.geometry):
            if parcel.geometry.contains(idx.points[i]):
                status = more_restrictive(status, idx.statuses[i])

Loud-failure invariant: an unrecognized DBF `STATUS` raises `ValueError` per the
same convention as the zone-class-coverage helper — a future Toronto schema change
surfaces as an ETL crash, not a silently-wrong output.
"""

import io
import logging
import zipfile
from pathlib import Path
from typing import NamedTuple

import shapefile  # pyshp
from shapely.geometry import Point
from shapely.strtree import STRtree

from . import _http
from ._address import STREET_TYPE_ABBREVIATIONS, normalize_address  # noqa: F401  (re-exported for backwards-compat callers)

PACKAGE_ID = "heritage-register"
RESOURCE_ID = "108b1080-d048-439f-a9e8-e8d6cd81bddb"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "e41da515-5ad1-4bc3-85ea-18ec9e55cd33/resource/"
    f"{RESOURCE_ID}/download/heritage_register_address_points_wgs84.zip"
)
CACHE_FILENAME = "heritage.shp.zip"

# Canonical wire values. The DBF's `STATUS` field uses the human-readable forms
# (`"Part IV"`, `"Part V"`, `"Listed"`); the wire format and downstream code use
# these snake_case canonical values.
STATUS_PART_IV = "part_iv"
STATUS_PART_V = "part_v"
STATUS_LISTED = "listed"

KNOWN_STATUSES = frozenset({STATUS_PART_IV, STATUS_PART_V, STATUS_LISTED})

# Single source of truth for DBF→canonical translation. Unknown DBF values raise
# ValueError in the loader (loud-failure pattern; see module docstring).
_DBF_STATUS_MAP = {
    "Part IV": STATUS_PART_IV,
    "Part V": STATUS_PART_V,
    "Listed": STATUS_LISTED,
}

# Most-restrictive precedence: when multiple heritage records resolve to the
# same parcel (Part IV inside a Part V district, for example), pick the
# strictest. `None` is treated as zero precedence.
STATUS_PRECEDENCE = {
    None: 0,
    STATUS_LISTED: 1,
    STATUS_PART_V: 2,
    STATUS_PART_IV: 3,
}


def more_restrictive(a: str | None, b: str | None) -> str | None:
    """Return the higher-precedence status between `a` and `b`.

    Treats `None` as zero precedence. On equal precedence, returns `a` (deterministic).
    Raises `KeyError` if either argument is a non-None string outside `KNOWN_STATUSES` —
    that's a loud-failure path; callers must canonicalize before invoking.
    """
    return a if STATUS_PRECEDENCE[a] >= STATUS_PRECEDENCE[b] else b


# `STREET_TYPE_ABBREVIATIONS` and `normalize_address` were relocated to
# `tools/sources/_address.py` 2026-05-04 so the building-permits loader can
# share them. Re-exported above via the top-of-file import for backwards
# compatibility.

class HeritageIndex(NamedTuple):
    """Bundle of heritage state consumed by `tools/build_parcels.py`.

    All list-shaped fields align by index — `points[i]`, `statuses[i]`,
    `addresses[i]` describe the same record. `point_tree` is the spatial index
    over `points` (used for the point-in-parcel fallback). `address_to_status`
    is the address-join lookup table; collisions on the same normalized address
    are resolved by `more_restrictive` so the dict deterministically holds the
    strictest tier. `address_to_indices` is the reverse index — mapping each
    normalized address to the list of record indices that share it — so the
    orchestrator can mark claimed indices in O(1) per address-join hit.

    Fields:
        point_tree: STRtree over the heritage points (for point-in-parcel fallback).
        points: 1:1 list of `Point` geometries (lon, lat) per record.
        statuses: 1:1 list of canonical status strings (one of `KNOWN_STATUSES`).
        addresses: 1:1 list of normalized addresses (`""` if blank in the DBF).
        address_to_status: pre-built dict for O(1) address-join lookup answer.
        address_to_indices: reverse index for O(1) claimed-marking on hit.
    """
    point_tree: STRtree
    points: list[Point]
    statuses: list[str]
    addresses: list[str]
    address_to_status: dict[str, str]
    address_to_indices: dict[str, list[int]]


_log = logging.getLogger(__name__)


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading Heritage Register SHP zip → %s", cached)
    _http.download_with_retries(RESOURCE_URL, cached)
    return cached


def _iter_records_from_zip(zip_path: Path):
    """Yield `(Point, canonical_status, raw_address)` per heritage record.

    Reads SHP + SHX + DBF in lockstep via `pyshp`'s `iterShapeRecords()`; the
    DBF read is required for tier classification (Part IV / Part V / Listed).

    Skips records with no/bad geometry silently — the caller (`compute_heritage`)
    is responsible for the count and the INFO log line.

    Raises `ValueError` for any DBF `STATUS` value outside `_DBF_STATUS_MAP` —
    loud-failure pattern (a future Toronto schema change surfaces as ETL crash,
    not silently-wrong output).

    Yields:
        (point, canonical_status, raw_address):
            point: shapely Point in WGS84 lon/lat order.
            canonical_status: one of `STATUS_PART_IV`, `STATUS_PART_V`, `STATUS_LISTED`.
            raw_address: DBF `ADDRESS` field verbatim (e.g. `"17  SALISBURY AVE"`);
                the caller normalizes via `normalize_address`.
    """
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        shp_name = next(
            (n for n in names if n.lower().endswith(".shp") and not n.lower().endswith(".shp.xml")),
            None,
        )
        if shp_name is None:
            raise RuntimeError(
                f"heritage: no .shp file inside {zip_path}; got {names!r}"
            )
        base = shp_name[:-4]
        shx_name = base + ".shx"
        if shx_name not in names:
            # Some bundles use uppercase; be forgiving.
            for candidate in (base + ".SHX", base + ".Shx"):
                if candidate in names:
                    shx_name = candidate
                    break
            else:
                raise RuntimeError(
                    f"heritage: no .shx file inside {zip_path}; got {names!r}"
                )
        dbf_name = base + ".dbf"
        if dbf_name not in names:
            for candidate in (base + ".DBF", base + ".Dbf"):
                if candidate in names:
                    dbf_name = candidate
                    break
            else:
                raise RuntimeError(
                    f"heritage: no .dbf file inside {zip_path}; got {names!r}"
                )

        shp_buf = io.BytesIO(z.read(shp_name))
        shx_buf = io.BytesIO(z.read(shx_name))
        dbf_buf = io.BytesIO(z.read(dbf_name))

    reader = shapefile.Reader(shp=shp_buf, shx=shx_buf, dbf=dbf_buf)
    if reader.shapeTypeName != "POINT":
        raise RuntimeError(
            f"heritage: expected POINT geometry, got {reader.shapeTypeName!r} "
            f"(see tools/README.md § Heritage Source for the locked geometry)"
        )

    for sr in reader.iterShapeRecords():
        if not sr.shape.points:
            continue
        raw_status = sr.record["STATUS"]
        canonical = _DBF_STATUS_MAP.get(raw_status)
        if canonical is None:
            raise ValueError(
                f"heritage: unrecognized DBF STATUS {raw_status!r}; "
                f"expected one of {sorted(_DBF_STATUS_MAP)}"
            )
        raw_address = sr.record["ADDRESS"] or ""
        lon, lat = sr.shape.points[0]
        yield Point(lon, lat), canonical, raw_address


def compute_heritage(cache_dir: Path) -> HeritageIndex:
    """Load the Heritage Register and return a `HeritageIndex` for the orchestrator.

    Reads SHP + DBF in one pass via `_iter_records_from_zip`, normalizes each
    record's address, builds an `address_to_status` dict (collisions on the same
    normalized address resolve via `more_restrictive`), and constructs an
    STRtree over the points.

    Loud-failure: any unrecognized DBF `STATUS` value propagates as `ValueError`
    from the iterator. No partial state is materialized — the caller crashes
    cleanly before any downstream consumer sees the index.

    Caller pattern (see `tools/build_parcels.py:_resolve_heritage_status`):
        idx = compute_heritage(cache)
        # Address-join first
        status = idx.address_to_status.get(parcel_normalized_address)
        if status is None:
            # Point-in-parcel fallback
            for i in idx.point_tree.query(parcel.geometry):
                if parcel.geometry.contains(idx.points[i]):
                    status = more_restrictive(status, idx.statuses[i])
    """
    zip_path = _ensure_cached(Path(cache_dir))

    points: list[Point] = []
    statuses: list[str] = []
    addresses: list[str] = []
    address_to_status: dict[str, str] = {}
    address_to_indices: dict[str, list[int]] = {}
    counts = {STATUS_PART_IV: 0, STATUS_PART_V: 0, STATUS_LISTED: 0}

    for pt, status, raw_address in _iter_records_from_zip(zip_path):
        normalized = normalize_address(raw_address)
        i = len(points)
        points.append(pt)
        statuses.append(status)
        addresses.append(normalized)
        counts[status] += 1
        if normalized:
            existing = address_to_status.get(normalized)
            address_to_status[normalized] = more_restrictive(existing, status)
            address_to_indices.setdefault(normalized, []).append(i)

    _log.info(
        "heritage: %d Part IV / %d Part V / %d Listed loaded "
        "(%d total, %d unique addresses)",
        counts[STATUS_PART_IV], counts[STATUS_PART_V], counts[STATUS_LISTED],
        len(points), len(address_to_status),
    )

    return HeritageIndex(
        point_tree=STRtree(points),
        points=points,
        statuses=statuses,
        addresses=addresses,
        address_to_status=address_to_status,
        address_to_indices=address_to_indices,
    )
