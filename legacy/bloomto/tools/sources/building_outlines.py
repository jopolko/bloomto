"""Toronto Building Outlines source — per-parcel building coverage ratio.

Per Task 2 of the parcel-multiplex-readiness spec, the CKAN dataset ships in
8 forms; we use the WGS84 CSV resource (273 MB) with embedded GeoJSON
`MultiPolygon` per row in the `geometry` column. See `tools/README.md` §
Building Outlines Source for the trade-off table and the locked
`SUBTYPE_CODE == 9003` filter rationale (defends against non-building features
like canopy/awning rows that may share the file).

Per-parcel coverage formula (Req 11.4):
    buildingCoverageRatio = clamp(sum(geodesic_area(parcel ∩ building)) / parcel.area_m2, 0, 1)

Geodesic area via `pyproj.Geod.geometry_area_perimeter` matches the
`Parcel.area_m2` denominator's units (also geodesic per `zoning.py:iter_parcels`),
so the ratio is dimensionless and accurate at Toronto's latitude regardless of
the source's WGS84 units.
"""

import csv
import json
import logging
import sys
from pathlib import Path
from typing import Iterable

from pyproj import Geod
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from . import _http
from .zoning import Parcel

PACKAGE_ID = "topographic-mapping-building-outlines"
RESOURCE_ID = "41372651-b2eb-4f1e-91d9-b5280b2f0ccd"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "09a930cc-2a52-49b2-866d-52ac7f769a73/resource/"
    f"{RESOURCE_ID}/download/building-outlines-4326.csv"
)
CACHE_FILENAME = "building_outlines.csv"

SUBTYPE_CODE_FIELD = "SUBTYPE_CODE"
GEOM_FIELD = "geometry"
BUILDING_SUBTYPE_CODE = 9003

# Some MultiPolygon geometry strings exceed the stdlib csv default 131 KB cell
# limit (matches the SolarTO source). Raise the limit at import time so the
# first call doesn't trip the cap.
csv.field_size_limit(sys.maxsize)

_GEOD = Geod(ellps="WGS84")
_log = logging.getLogger(__name__)


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading Building Outlines CSV (~273 MB) → %s", cached)
    _http.download_with_retries(RESOURCE_URL, cached)
    return cached


def _load_building_polygons(cache_path: Path) -> list[BaseGeometry]:
    """Stream the CSV and return parsed building MultiPolygon geometries.

    Filters strictly to `SUBTYPE_CODE == 9003` (the canonical "Building Outline"
    code) — the dataset's schema admits other subtypes (e.g. canopy/awning) and
    we don't want those padding the coverage ratio.
    """
    geoms: list[BaseGeometry] = []
    seen = 0
    skipped_subtype = 0
    skipped_no_geom = 0
    skipped_bad_geom = 0
    with cache_path.open(encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            seen += 1
            try:
                if int(row.get(SUBTYPE_CODE_FIELD, "0")) != BUILDING_SUBTYPE_CODE:
                    skipped_subtype += 1
                    continue
            except (ValueError, TypeError):
                skipped_subtype += 1
                continue
            geom_raw = row.get(GEOM_FIELD) or ""
            if not geom_raw:
                skipped_no_geom += 1
                continue
            try:
                geom = shape(json.loads(geom_raw))
            except Exception:
                skipped_bad_geom += 1
                continue
            geoms.append(geom)
    _log.info(
        "building_outlines: %d rows seen, %d footprints kept "
        "(skipped: wrong_subtype=%d, no_geom=%d, bad_geom=%d)",
        seen, len(geoms), skipped_subtype, skipped_no_geom, skipped_bad_geom,
    )
    return geoms


def compute_coverage(
    parcels: Iterable[Parcel],
    cache_dir: Path,
) -> dict[str, float]:
    """Compute the per-parcel building-coverage ratio.

    Returns a `{parcel_id: coverage}` dict where coverage ∈ [0, 1]. A parcel
    with no candidate buildings (vacant lot) gets `0.0` — distinguished from
    "missing data" only by the parallel `solarShadowQuality` field per design.

    Memory: holds all building polygons in memory simultaneously (one STRtree
    over ~500k MultiPolygons). At Toronto scale this is the v1.2 ETL's largest
    persistent allocation; the streaming-then-tree pattern keeps peak memory
    bounded by the parsed-geometry footprint, not the on-disk CSV size.
    """
    cached = _ensure_cached(Path(cache_dir))
    geoms = _load_building_polygons(cached)

    if not geoms:
        _log.warning("building_outlines: zero footprints loaded — every parcel will get 0.0 coverage")
        return {p.parcel_id: 0.0 for p in parcels}

    tree = STRtree(geoms)

    coverage_by_id: dict[str, float] = {}
    parcels_total = 0
    parcels_with_buildings = 0
    skipped_zero_area = 0

    for parcel in parcels:
        parcels_total += 1

        if parcel.area_m2 <= 0:
            coverage_by_id[parcel.parcel_id] = 0.0
            skipped_zero_area += 1
            continue

        building_area_m2 = 0.0
        any_hit = False
        for idx in tree.query(parcel.geometry):
            building = geoms[idx]
            try:
                inter = parcel.geometry.intersection(building)
            except Exception:
                continue
            if inter.is_empty:
                continue
            area_signed, _ = _GEOD.geometry_area_perimeter(inter)
            building_area_m2 += abs(area_signed)
            any_hit = True

        if any_hit:
            parcels_with_buildings += 1

        ratio = building_area_m2 / parcel.area_m2
        coverage_by_id[parcel.parcel_id] = max(0.0, min(1.0, ratio))

    _log.info(
        "building_outlines: %d footprints loaded; coverage computed for %d parcels "
        "(%d with ≥1 building intersection, %d skipped zero-area)",
        len(geoms), parcels_total, parcels_with_buildings, skipped_zero_area,
    )
    return coverage_by_id
