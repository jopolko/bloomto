"""Compute `potential` (Missing-Middle dwelling capacity) per neighborhood by joining
Property Boundaries parcels against the Zoning By-law's residential zone classes,
multiplying by the per-zone unit cap from `tools/zoning_multipliers.json`.

Also exposes the two v1.2 building blocks `iter_parcels` and `load_zone_index`,
consumed by the parcel-level ETL (`tools/build_parcels.py`). `compute_potential`
itself is refactored on top of these helpers — behavior is byte-identical to v1.1.

See `tools/README.md` § Zoning + Property Source for resource ids, schema, and risks.
"""

import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import ijson
import requests
from pyproj import Geod
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from .neighborhoods import Neighborhood

ZONING_PACKAGE_ID = "zoning-by-law"
ZONING_RESOURCE_ID = "d75fa1ed-cd04-4a0b-bb6d-2b928ffffa6e"
ZONING_RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "34927e44-fc11-4336-a8aa-a0dfb27658b7/resource/"
    f"{ZONING_RESOURCE_ID}/download/zoning-area-4326.geojson"
)
ZONING_CACHE = "zoning_area.geojson"

PROPERTY_PACKAGE_ID = "property-boundaries"
PROPERTY_RESOURCE_ID = "4d4943a6-98ec-4442-9ced-f600f5bc8d27"
PROPERTY_RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "1acaa8b0-f235-4df6-8305-02025ccdeb07/resource/"
    f"{PROPERTY_RESOURCE_ID}/download/property-boundaries-4326.geojson"
)
PROPERTY_CACHE = "property_boundaries.geojson"

MULTIPLIERS_FILE = Path(__file__).resolve().parent.parent / "zoning_multipliers.json"

_GEOD = Geod(ellps="WGS84")
_log = logging.getLogger(__name__)


@dataclass
class Parcel:
    """One Property Boundaries feature, normalized for downstream scoring.

    `geometry` is the WGS84 polygon/multipolygon as parsed by shapely.
    `centroid` is `(lon, lat)` of the shapely centroid (NOT representative_point —
    consumers needing a topology-safe interior point should call
    `geometry.representative_point()` directly). `area_m2` is geodesic, computed
    via `pyproj.Geod.geometry_area_perimeter` so it's correct at Toronto's
    latitude regardless of the source's `STATEDAREA` string.
    """
    parcel_id: str
    address: str | None
    geometry: BaseGeometry
    centroid: tuple[float, float]
    area_m2: float


def _download_with_retries(url: str, dest: Path) -> None:
    backoffs = (0.5, 1.0, 2.0)
    for attempt in range(len(backoffs) + 1):
        try:
            with requests.get(url, stream=True, timeout=600) as r:
                r.raise_for_status()
                with dest.open("wb") as fp:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            fp.write(chunk)
            return
        except (requests.RequestException, OSError) as e:
            if attempt == len(backoffs):
                raise
            wait = backoffs[attempt]
            _log.warning("download %s failed (attempt %d): %s — retrying in %ss",
                         url, attempt + 1, e, wait)
            time.sleep(wait)


def _ensure_cached(cache_dir: Path, filename: str, url: str, label: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / filename
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading %s → %s", label, cached)
    _download_with_retries(url, cached)
    return cached


def _load_multipliers() -> dict[str, int]:
    """Load the per-zone-class unit-cap table from `tools/zoning_multipliers.json`.

    The returned dict is the *complete* set of ZN_ZONE codes the ETL recognizes;
    callers must use `lookup_multiplier` (which raises `KeyError` on unknown
    codes) rather than `dict.get(zone_class, default)` so a future Toronto by-law
    amendment that introduces a new code surfaces as a loud failure rather than
    silently producing wrong scores. The legacy `_default` JSON entry was
    retired with the v1.2 zone-class-coverage fix.
    """
    with MULTIPLIERS_FILE.open(encoding="utf-8") as fp:
        raw = json.load(fp)
    table: dict[str, int] = {}
    for k, v in raw.items():
        if k == "_comment":
            continue
        table[k] = int(v["max_units_per_lot"])
    return table


def lookup_multiplier(zone_class: str, multipliers: dict[str, int]) -> int:
    """Resolve `zone_class` to a per-lot unit cap.

    Returns 0 when `zone_class` is empty (parcel sits outside any zoning polygon —
    a known no-op state, not an unknown-code state). Raises `KeyError` when
    `zone_class` is non-empty but absent from `multipliers`, with a message
    pointing the operator at `tools/zoning_multipliers.json`.
    """
    if not zone_class:
        return 0
    if zone_class not in multipliers:
        raise KeyError(
            f"unrecognized ZN_ZONE class {zone_class!r}; "
            f"add it to tools/zoning_multipliers.json"
        )
    return multipliers[zone_class]


def _clean_address_field(v: object) -> str | None:
    """Coerce blanks / "None" strings to actual None — see the rationale in
    iter_parcels comments. Pulled out so iter_parcel_records can share it.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "none":
        return None
    return s


def _build_address(props: dict) -> str | None:
    """Assemble the civic address string from a Property Boundaries feature's
    properties dict. Returns None when both number and street are blank/'None'.
    """
    number = _clean_address_field(props.get("ADDRESS_NUMBER"))
    street = _clean_address_field(props.get("LINEAR_NAME_FULL"))
    if number or street:
        return " ".join(p for p in (number, street) if p).strip() or None
    return None


def parcel_from_record(record: dict) -> "Parcel | None":
    """Materialize a Parcel from a lightweight record dict (parcel_id, address,
    geometry_dict). Does the slow GEOS work — shapely shape() + geodesic
    area — that iter_parcel_records intentionally defers to the per-parcel
    consumer. Returns None when the geometry can't be parsed (caller skips).

    Used in the multiprocessing fast-path: parent thread streams cheap dicts
    via `iter_parcel_records`, workers each call `parcel_from_record` so
    parsing is parallelized across cores instead of blocking the parent.
    """
    geom_dict = record.get("geometry_dict")
    if not geom_dict:
        return None
    try:
        parcel_geom = shape(geom_dict)
    except Exception:
        return None
    c = parcel_geom.centroid
    try:
        area_m2 = abs(_GEOD.geometry_area_perimeter(parcel_geom)[0])
    except Exception:
        area_m2 = 0.0
    return Parcel(
        parcel_id=record["parcel_id"],
        address=record["address"],
        geometry=parcel_geom,
        centroid=(c.x, c.y),
        area_m2=area_m2,
    )


def iter_parcel_records(cache_dir: Path) -> Iterator[dict]:
    """Lightweight streaming variant of `iter_parcels` for multiprocessing.

    Yields plain dicts with `parcel_id`, `address`, and `geometry_dict` (the
    raw GeoJSON geometry, NOT a shapely object). Multiprocessing-friendly:
    dicts pickle cheaply (~10× faster than shapely-via-WKT), and the slow
    GEOS work (`shape()` + geodesic area) gets deferred to workers via
    `parcel_from_record`. Net effect: parsing parallelizes across cores
    instead of bottlenecking the parent.

    For sequential mode use the eager `iter_parcels` instead — it's the
    same I/O cost but materializes Parcels in-process so the loop body can
    use `parcel.geometry` directly without a per-call `parcel_from_record`.
    """
    cache = Path(cache_dir)
    property_path = _ensure_cached(cache, PROPERTY_CACHE, PROPERTY_RESOURCE_URL,
                                   "Property Boundaries GeoJSON (~475 MB)")

    with property_path.open("rb") as fp:
        # use_float=True coerces ijson's default Decimal coords to plain
        # floats inline — avoids a json.dumps/loads roundtrip per record
        # (saves ~30s on 528K parcels) and shrinks pickle size by ~30%.
        for feat in ijson.items(fp, "features.item", use_float=True):
            geom = feat.get("geometry")
            if not geom:
                continue
            props = feat.get("properties") or {}
            raw_pid = props.get("PARCELID")
            parcel_id = str(raw_pid) if raw_pid not in (None, "") else ""
            yield {
                "parcel_id": parcel_id,
                "address": _build_address(props),
                "geometry_dict": geom,
            }


def iter_parcels(cache_dir: Path) -> Iterator[Parcel]:
    """Stream Property Boundaries features as `Parcel` records.

    Eager variant — does the shapely/geodesic work in the iterator. Used by
    the sequential per-parcel path (`workers <= 1`) and by tests. The
    multiprocessing fast-path uses `iter_parcel_records` + `parcel_from_record`
    instead so the GEOS work parallelizes.

    Skips features with missing or unparseable geometry silently — those are
    dropped by the same defensive guards v1.1's `compute_potential` already used.
    """
    cache = Path(cache_dir)
    property_path = _ensure_cached(cache, PROPERTY_CACHE, PROPERTY_RESOURCE_URL,
                                   "Property Boundaries GeoJSON (~475 MB)")

    with property_path.open("rb") as fp:
        for feat in ijson.items(fp, "features.item"):
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                parcel_geom = shape(geom)
            except Exception:
                continue

            props = feat.get("properties") or {}
            raw_pid = props.get("PARCELID")
            parcel_id = str(raw_pid) if raw_pid not in (None, "") else ""

            address = _build_address(props)

            c = parcel_geom.centroid
            try:
                area_m2 = abs(_GEOD.geometry_area_perimeter(parcel_geom)[0])
            except Exception:
                area_m2 = 0.0

            yield Parcel(
                parcel_id=parcel_id,
                address=address,
                geometry=parcel_geom,
                centroid=(c.x, c.y),
                area_m2=area_m2,
            )


def load_zone_index(cache_dir: Path) -> tuple[STRtree, list[str]]:
    """Load the Zoning By-law GeoJSON as an STRtree + parallel `ZN_ZONE` labels.

    Returns `(tree, zone_classes)` where `tree.geometries[i]` is the polygon
    parallel to `zone_classes[i]`. Consumers querying `tree.query(point)` get
    bbox-candidate indices into both arrays.
    """
    cache = Path(cache_dir)
    zoning_path = _ensure_cached(cache, ZONING_CACHE, ZONING_RESOURCE_URL,
                                 "Zoning Area GeoJSON (~49 MB)")

    _log.info("loading zoning polygons...")
    with zoning_path.open(encoding="utf-8") as fp:
        zdata = json.load(fp)
    zone_geoms: list[BaseGeometry] = []
    zone_classes: list[str] = []
    for feat in zdata.get("features") or []:
        if not feat.get("geometry"):
            continue
        zone_geoms.append(shape(feat["geometry"]))
        zone_classes.append((feat.get("properties") or {}).get("ZN_ZONE") or "")
    _log.info("zoning: %d zone polygons", len(zone_geoms))
    return STRtree(zone_geoms), zone_classes


def compute_potential(neighborhoods: list[Neighborhood],
                      existing_by_name: dict[str, int],
                      cache_dir: Path
                      ) -> tuple[dict[str, int], list[str]]:
    """Returns `(potential_by_name, fallback_names)`. Per neighborhood: estimated
    residential dwelling capacity = sum over residential parcels of the per-zone unit
    cap. Defensive floor: `potential = max(potential, existing)` so headroom is never
    negative. Neighborhoods with zero residential parcels keep `potential = existing`
    (zero headroom) and appear in `fallback_names`.
    """
    cache = Path(cache_dir)
    multipliers = _load_multipliers()
    _log.info("zoning: loaded %d zone-class multipliers", len(multipliers))

    zone_tree, zone_classes = load_zone_index(cache)

    polygons = [n.polygon for n in neighborhoods]
    nb_tree = STRtree(polygons)

    potential_by_idx = [0] * len(neighborhoods)
    parcels_attributed = 0
    parcels_residential = 0
    parcels_no_zone = 0
    parcels_no_neighborhood = 0
    parcels_total = 0

    _log.info("streaming property boundaries (~475 MB) via iter_parcels...")
    for parcel in iter_parcels(cache):
        parcels_total += 1
        rep = parcel.geometry.representative_point()

        # Look up zone class via the zoning STRtree.
        zone_class = ""
        for zi in zone_tree.query(rep):
            if zone_tree.geometries[zi].contains(rep):
                zone_class = zone_classes[zi]
                break
        if not zone_class:
            parcels_no_zone += 1
            continue

        multiplier = lookup_multiplier(zone_class, multipliers)
        if multiplier == 0:
            continue
        parcels_residential += 1

        # Attribute to a neighborhood.
        for ni in nb_tree.query(rep):
            if polygons[ni].contains(rep):
                potential_by_idx[ni] += multiplier
                parcels_attributed += 1
                break
        else:
            parcels_no_neighborhood += 1

        if parcels_total % 100_000 == 0:
            _log.info("  ... %d parcels processed (%d residential)",
                      parcels_total, parcels_residential)

    _log.info("zoning: %d total parcels, %d residential, %d attributed, "
              "%d no-zone, %d no-neighborhood",
              parcels_total, parcels_residential, parcels_attributed,
              parcels_no_zone, parcels_no_neighborhood)

    potential_by_name: dict[str, int] = {}
    fallback_names: list[str] = []
    floor_applied = 0
    for nb, pot in zip(neighborhoods, potential_by_idx):
        existing = existing_by_name.get(nb.name, 0)
        if pot == 0:
            potential_by_name[nb.name] = existing
            fallback_names.append(nb.name)
            continue
        if pot < existing:
            _log.info("zoning floor: %s pot=%d < existing=%d — clamping to existing",
                      nb.name, pot, existing)
            pot = existing
            floor_applied += 1
        potential_by_name[nb.name] = pot

    _log.info("zoning: %d fallback names, defensive floor applied to %d neighborhoods",
              len(fallback_names), floor_applied)
    return potential_by_name, fallback_names


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    from .neighborhoods import fetch_neighborhoods
    from .census import compute_census
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tools/cache")
    nbs = fetch_neighborhoods(cache)
    _, existing, _ = compute_census(nbs, cache)
    pot, fb = compute_potential(nbs, existing, cache)
    samples = [(n.name, existing.get(n.name, 0), pot[n.name]) for n in nbs[:5]]
    print(f"potential: {len(pot)} entries")
    print(f"  first 5 (name, existing, potential): {samples}")
    top = sorted(pot.items(), key=lambda kv: -kv[1])[:5]
    print(f"  top 5 by potential: {top}")
    print(f"fallbacks: {len(fb)} {fb[:5]}{'...' if len(fb) > 5 else ''}")
