"""Address Points — Toronto One Address Repository (CKAN, daily).

Toronto's `address-points-municipal-toronto-one-address-repository` dataset
publishes every municipal address point as a GeoJSON feature with the
canonical street number, sub-suffix (e.g., `A` / `B`), and centroid
coordinates. Daily-refreshed; ~860K records citywide.

## Why we care

The structure-type problem is the biggest data-quality gap in BloomTO's
broader cohort: 12,377 parcels are classifier-derived (~76% accurate) and
the cross-boundary classifier underclaims detached in suburbs (garages
within 1.5m trigger false-attached).

Address Points gives a clean ground-truth signal: **a parcel polygon
that contains ≥2 distinct address points is almost certainly attached
housing.** Two semi-detached units share a parcel polygon and get assigned
addresses like "100" + "100A" or "100" + "102". A row of 4 townhomes on
one parcel polygon has 4 address points. A standalone detached house has
exactly 1.

## Pipeline shape

`tools/build_parcels.py` calls `build_address_points_index(cache_dir)`
once at startup, then for each parcel polygon does a spatial-tree query
to count distinct address points inside. The result is consumed by the
structure-type waterfall as a NEW tier between OSM and the cross-boundary
classifier:

    permit  →  osm  →  address_points  →  classifier  →  vacant

The address_points tier ONLY produces "semi" or "row" verdicts (never
"detached" — single-point parcels just pass through to the next tier so
the classifier still gets to weigh in on their detached/attached call).
That preserves the precision of the existing ground-truth sources while
filling the suburban-detached false-attached gap with new attached truth.

## Cache

Single GeoJSON file ~80MB on disk; refreshed every 24h. The CKAN dataset
has no datastore-active CSV, so we fetch the GeoJSON resource directly
via HTTPS. Streamed to disk to avoid OOM.
"""

import json
import logging
import shutil
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import requests
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

from tools.sources._address import normalize_address

CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action"
PACKAGE_ID = "address-points-municipal-toronto-one-address-repository"
RESOURCE_NAME_HINT = "Address Points - 4326.geojson"
CACHE_FILENAME = "address_points.geojson"
CACHE_TTL_S = 24 * 3600

# User-Agent required by Toronto's CDN to avoid 403 on programmatic fetch.
_UA = "BloomTO/0.1 (multiplex parcel finder; contact via project repo)"

_log = logging.getLogger(__name__)


@dataclass
class AddressPoint:
    """One municipal address record."""
    address_full: str          # "1871 Davenport Rd"
    address_norm: str          # normalized for join with parcel address
    lo_num: str                # "100"
    lo_num_suf: str | None     # "A" / "B" / None
    hi_num: str | None         # "102" if range, else None
    address_class: str         # "L" (Land), etc.
    point: Point               # WGS84 (lon, lat)


class AddressPointIndex(NamedTuple):
    """Spatial index for "how many address points fall inside this parcel?".

    `points[i]` and `records[i]` align — STRtree.query returns indices
    into `points`, which we map back to `records` for the AddressPoint data.
    """
    tree: STRtree
    points: list[Point]
    records: list[AddressPoint]


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_TTL_S


def _resolve_geojson_url() -> str:
    pkg = requests.get(
        f"{CKAN_BASE}/package_show",
        params={"id": PACKAGE_ID},
        timeout=30,
    ).json()["result"]
    # Prefer the WGS84 GeoJSON (matches our parcel CRS for spatial join).
    for r in pkg.get("resources", []):
        if (r.get("format", "").upper() == "GEOJSON"
                and "4326" in (r.get("name") or "")):
            return r["url"]
    # Fallback to any GeoJSON resource.
    for r in pkg.get("resources", []):
        if r.get("format", "").upper() == "GEOJSON":
            return r["url"]
    raise RuntimeError(f"no GeoJSON resource found in {PACKAGE_ID}")


def _ensure_cached(cache_path: Path) -> Path:
    if _is_cache_fresh(cache_path):
        _log.info("address_points: using cached %s (%.1f MB)",
                  cache_path, cache_path.stat().st_size / 1e6)
        return cache_path

    url = _resolve_geojson_url()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    _log.info("address_points: fetching from CKAN…")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1 << 20)  # 1MB chunks
    tmp.replace(cache_path)
    _log.info("address_points: cached %s (%.1f MB)",
              cache_path, cache_path.stat().st_size / 1e6)
    return cache_path


def _clean(v) -> str | None:
    """Toronto's address-point CSV stuffs the literal string "None" into
    nullable fields. Treat that and empty strings as None for our purposes.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "none":
        return None
    return s


def _format_address(props: dict) -> tuple[str, str]:
    """Reconstruct the canonical address from numeric pieces.

    Prefers the explicit `ADDRESS_FULL` field when present; falls back
    to LO_NUM + LO_NUM_SUF + LINEAR_NAME_FULL composition.
    """
    full = _clean(props.get("ADDRESS_FULL"))
    if full:
        return full, normalize_address(full)
    num = _clean(props.get("LO_NUM")) or ""
    suf = _clean(props.get("LO_NUM_SUF")) or ""
    name = _clean(props.get("LINEAR_NAME_FULL")) or ""
    parts = [p for p in (f"{num}{suf}" if suf else num, name) if p]
    raw = " ".join(parts)
    return raw, normalize_address(raw)


def build_address_points_index(cache_dir: Path) -> AddressPointIndex:
    """Fetch + parse + spatial-index the full address-points dataset.

    Returns an `AddressPointIndex` whose STRtree can answer
    "which address points fall inside parcel polygon X" in O(log n).
    Run once per ETL invocation; the tree itself is ~10MB in memory.
    """
    cache = Path(cache_dir)
    geojson_path = _ensure_cached(cache / CACHE_FILENAME)
    _log.info("address_points: parsing GeoJSON…")
    with geojson_path.open(encoding="utf-8") as fp:
        data = json.load(fp)

    records: list[AddressPoint] = []
    points: list[Point] = []
    skipped = 0
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        # Toronto's address-points feed wraps each point in a MultiPoint
        # with a single coordinate pair. Accept both shapes.
        if gtype == "Point":
            pair = coords
        elif gtype == "MultiPoint" and coords:
            pair = coords[0]
        else:
            skipped += 1
            continue
        if not pair or len(pair) < 2:
            skipped += 1
            continue
        lon, lat = float(pair[0]), float(pair[1])
        props = feat.get("properties") or {}
        raw_addr, norm_addr = _format_address(props)
        if not norm_addr:
            skipped += 1
            continue
        records.append(AddressPoint(
            address_full=raw_addr,
            address_norm=norm_addr,
            lo_num=_clean(props.get("LO_NUM")) or "",
            lo_num_suf=_clean(props.get("LO_NUM_SUF")),
            hi_num=_clean(props.get("HI_NUM")),
            address_class=_clean(props.get("ADDRESS_CLASS")) or "",
            point=Point(lon, lat),
        ))
        points.append(records[-1].point)

    _log.info(
        "address_points: parsed %d records (skipped %d non-point/empty)",
        len(records), skipped,
    )
    tree = STRtree(points)
    return AddressPointIndex(tree=tree, points=points, records=records)


def points_in_parcel(idx: AddressPointIndex, parcel: Polygon) -> list[AddressPoint]:
    """Return all AddressPoint records whose centroid falls inside `parcel`.

    The STRtree query is a bounding-box pre-filter; we then apply
    `parcel.contains(point)` for the precise hit test. Both inputs must be
    in the same CRS (WGS84 / EPSG:4326).
    """
    candidate_idxs = idx.tree.query(parcel)
    out: list[AddressPoint] = []
    for i in candidate_idxs:
        pt = idx.points[int(i)]
        if parcel.contains(pt) or parcel.touches(pt):
            out.append(idx.records[int(i)])
    return out


def classify_attachment_from_points(
    pts: list[AddressPoint],
) -> tuple[str | None, str]:
    """Distill a structure-type verdict from a parcel's address-point set.

    Returns `(structure_type, reason)` where `structure_type` is one of
    `"semi"`, `"row"`, or `None` (no verdict). The classifier is
    deliberately conservative: single-point parcels return `None` so the
    downstream cross-boundary classifier still gets to weigh in. The aim
    is to FLIP false-detached suburban parcels (where the address-point
    set proves attached), not to override the cross-boundary classifier
    on detached calls.

    Rules:
    - Deduplicate on `address_full` (a parcel can have duplicate point
      records for the same address — e.g., front-door + side-door).
    - 1 distinct address → no verdict (`None, "single_address"`).
    - 2 distinct addresses → "semi".
    - 3+ distinct addresses → "row".
    """
    seen: set[str] = set()
    for ap in pts:
        seen.add(ap.address_full.upper().strip())
    n = len(seen)
    if n <= 1:
        return None, "single_address"
    if n == 2:
        return "semi", "two_addresses"
    return "row", f"{n}_addresses"
