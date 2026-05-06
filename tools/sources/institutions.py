"""Toronto institutional-points source — schools, places of worship, parks, etc.

Loads a curated set of Toronto Open Data CKAN datasets that publish
non-residential land uses as point features (and the parks dataset as
polygons), unions them into a single STRtree, and exposes a per-parcel test
`is_institutional(parcel, idx)` that returns `(hit: bool, category: str | None)`.

The build pipeline (`tools/build_parcels.py`) consumes this index to force
`score = 0` on any parcel whose polygon contains an institutional point
(or intersects an institutional polygon for parks). This replaces the
frontend bandaid (`looksInstitutional()` in goldmines.html) with an
ETL-side, data-grounded filter — the proper fix per the 2026-05-02
brainstorm-list item #1.

Categories included (per the deep TransformTO crawl + CKAN survey):
  - school              — TDSB + TCDSB + private (school-locations-all-types)
  - place_of_worship    — places-of-worship
  - park                — parks (POLYGONS — only intersects test, not contains)
  - library             — library-branch-general-information
  - fire                — fire-station-locations
  - police              — police-facility-locations
  - ambulance           — ambulance-station-locations
  - long_term_care      — long-term-care-locations-city-operated
  - community_facility  — parks-and-recreation-facilities
  - child_care          — licensed-child-care-centres

NOT included (deferred for v1.3+ or out of scope):
  - Hospitals: no clean Toronto Open Data dataset; the existing addressed-mall
    frontend bandaid catches hospital corridors as collateral.
  - TTC subway shapefile: would need feature-type filtering (lines vs station
    polygons); deferred to avoid catching real residences across the street
    from a station. The existing addressed-mall pattern + unaddressed-strip
    pattern catch the worst TTC offenders.
  - Major retail / shopping centres: no direct CKAN dataset; the addressed
    CR/CRE + lot > 5000 m² frontend pattern handles malls (Yorkgate etc.).

Loud-failure pattern matches `heritage.py`: schema drift in any source
dataset surfaces as an ETL crash, not silently-wrong output.

Cache footprint: ~12 MB total across the 9 GeoJSON files + 1 SHP zip (parks).
Network on cold cache: ~12 MB; on warm cache: zero.
"""

import io
import json
import logging
import zipfile
from pathlib import Path
from typing import NamedTuple

import ijson  # streaming JSON
import shapefile  # pyshp
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from . import _http

_log = logging.getLogger(__name__)

# Dataset registry. Each entry: (category, cache_filename, download_url, format).
# Format ∈ {"geojson", "shp_zip"}. URLs resolved via CKAN package_show on
# 2026-05-02 — re-resolve if Toronto rotates resource IDs (rare but possible).
_DATASETS: tuple[tuple[str, str, str, str], ...] = (
    (
        "school",
        "institutions_schools.geojson",
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/1a714b5c-64c0-4cdf-9739-0086f80fb3ee/resource/f1160f3f-a651-40ed-914e-07b670ac5aec/download/school-locations-all-types-data-4326.geojson",
        "geojson",
    ),
    (
        "place_of_worship",
        "institutions_places_of_worship.geojson",
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/8e22e693-3394-4dfa-8dc0-eb436db38603/resource/666b514c-04ae-4434-b682-64620cb87114/download/places-of-worship-4326.geojson",
        "geojson",
    ),
    (
        "park",
        "institutions_parks.zip",
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/2aac8903-23ff-4072-ab72-b76cac44ad89/resource/9f53c253-a47e-497f-8a07-528f7d7aad90/download/parks-wgs84.zip",
        "shp_zip",
    ),
    (
        "library",
        "institutions_libraries.geojson",
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/f5aa9b07-da35-45e6-b31f-d6790eb9bd9b/resource/5f4950b4-c727-4e54-8d0d-972e198268d6/download/tpl-branch-general-information-4326.geojson",
        "geojson",
    ),
    (
        "fire",
        "institutions_fire.geojson",
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/a6ce5495-8e2b-421a-ab11-964569416f31/resource/4a9bb96b-da5e-4c67-aaf4-3f8f4f311430/download/fire-station-locations-4326.geojson",
        "geojson",
    ),
    (
        "police",
        "institutions_police.geojson",
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/9aeefa17-27e8-4dd9-b74d-80f7f9eb85ac/resource/c0176e24-8b76-4bb2-96fa-61cc1af2a065/download/police-facility-locations-4326.geojson",
        "geojson",
    ),
    (
        "ambulance",
        "institutions_ambulance.geojson",
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/bb8e12a7-edf4-456f-ba77-43be57885a45/resource/2a7d74f7-e73c-4d04-9fd9-3a516c9e6205/download/ambulance-station-locations-4326.geojson",
        "geojson",
    ),
    (
        "long_term_care",
        "institutions_ltc.geojson",
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/308a036a-ceb5-488a-859f-4d7dc2fd592d/resource/6bac587f-d8a7-4403-b279-8f1cf05ed20a/download/long-term-care-locations-4326.geojson",
        "geojson",
    ),
    (
        "community_facility",
        "institutions_community_facilities.geojson",
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/cbea3a67-9168-4c6d-8186-16ac1a795b5b/resource/f6cdcd50-da7b-4ede-8e60-c3cdba70b559/download/parks-and-recreation-facilities-4326.geojson",
        "geojson",
    ),
    (
        "child_care",
        "institutions_child_care.geojson",
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/059d37c6-d88b-42fb-b230-ec6a5ec74c24/resource/4f5ef49d-15ee-4d73-8b66-90303c5ef746/download/child-care-centres-4326.geojson",
        "geojson",
    ),
)

CATEGORIES: tuple[str, ...] = tuple(d[0] for d in _DATASETS)


class InstitutionsIndex(NamedTuple):
    """Bundle of institutional geometries consumed by `tools/build_parcels.py`.

    Three lists align by index — `geometries[i]`, `categories[i]`,
    `is_polygon[i]` describe the same record. `tree` is the spatial index
    over `geometries`.

    Test pattern (see `tools/build_parcels.py`):
        idx = compute_institutions(cache)
        for i in idx.tree.query(parcel.geometry):
            g = idx.geometries[i]
            if idx.is_polygon[i]:
                if parcel.geometry.intersects(g):
                    return (True, idx.categories[i])
            else:  # point
                if parcel.geometry.contains(g):
                    return (True, idx.categories[i])
        return (False, None)
    """
    tree: STRtree
    geometries: list[BaseGeometry]
    categories: list[str]
    is_polygon: list[bool]


def _ensure_cached(cache_dir: Path, filename: str, url: str) -> Path:
    """One-shot cache: return the cached path; download once if missing."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / filename
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading %s → %s", url, cached)
    _http.download_with_retries(url, cached)
    return cached


def _iter_geojson(path: Path):
    """Yield `(geometry, props)` per Feature in a GeoJSON file. Streaming."""
    with path.open("rb") as fp:
        for feat in ijson.items(fp, "features.item"):
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                g = shape(geom)
            except Exception as e:
                _log.warning("skipping unparseable geometry in %s: %s", path.name, e)
                continue
            if g.is_empty:
                continue
            yield g, (feat.get("properties") or {})


def _iter_shp_zip(path: Path):
    """Yield `(geometry, record_dict)` per record in a zipped Shapefile."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shp = next((n for n in names if n.lower().endswith(".shp") and not n.lower().endswith(".shp.xml")), None)
        shx = next((n for n in names if n.lower().endswith(".shx")), None)
        dbf = next((n for n in names if n.lower().endswith(".dbf")), None)
        if not (shp and dbf):
            raise RuntimeError(f"{path.name}: missing .shp or .dbf in zip")
        with z.open(shp) as shp_fp, z.open(dbf) as dbf_fp:
            shx_fp = z.open(shx) if shx else None
            try:
                reader = shapefile.Reader(shp=shp_fp, shx=shx_fp, dbf=dbf_fp)
                fields = [f[0] for f in reader.fields[1:]]  # skip deletion flag
                for sr in reader.iterShapeRecords():
                    s = sr.shape
                    if not s.points:
                        continue
                    try:
                        g = shape(s.__geo_interface__)
                    except Exception as e:
                        _log.warning("skipping unparseable shape in %s: %s", path.name, e)
                        continue
                    if g.is_empty:
                        continue
                    yield g, dict(zip(fields, sr.record))
            finally:
                if shx_fp is not None:
                    shx_fp.close()


def compute_institutions(cache_dir: Path) -> InstitutionsIndex:
    """Load every registered dataset and return a single combined STRtree.

    Cold-cache cost: ~12 MB download spread over 10 datasets.
    Warm-cache cost: ~3 seconds parse-and-index.

    Loud-failure: a dataset missing or unparseable raises rather than silently
    skipping (matches the heritage source loader's contract).
    """
    cache = Path(cache_dir)
    geometries: list[BaseGeometry] = []
    categories: list[str] = []
    is_polygon: list[bool] = []
    counts: dict[str, int] = {}

    for category, filename, url, fmt in _DATASETS:
        path = _ensure_cached(cache, filename, url)
        kept = 0
        if fmt == "geojson":
            for g, _props in _iter_geojson(path):
                geometries.append(g)
                categories.append(category)
                # Most institutional GeoJSONs ship as POINT features. If a
                # dataset surprises us with a polygon, treat as polygon.
                is_polygon.append(g.geom_type not in ("Point", "MultiPoint"))
                kept += 1
        elif fmt == "shp_zip":
            for g, _record in _iter_shp_zip(path):
                geometries.append(g)
                categories.append(category)
                is_polygon.append(g.geom_type not in ("Point", "MultiPoint"))
                kept += 1
        else:
            raise ValueError(f"unknown format {fmt!r} for {category}")
        counts[category] = kept

    _log.info(
        "institutions: %d total geometries loaded (%s)",
        len(geometries),
        ", ".join(f"{cat}={n}" for cat, n in counts.items()),
    )

    tree = STRtree(geometries)
    return InstitutionsIndex(
        tree=tree,
        geometries=geometries,
        categories=categories,
        is_polygon=is_polygon,
    )


def is_institutional(parcel_geom: BaseGeometry, idx: InstitutionsIndex) -> tuple[bool, str | None]:
    """Test parcel polygon against the institutions index.

    Returns `(True, category)` on first hit, `(False, None)` otherwise.
    Point-vs-polygon dispatch is per-record via `idx.is_polygon[i]`:
      - Points: parcel.contains(point) — fires when the institution's
        registered address geocodes inside this parcel.
      - Polygons (parks): institution_polygon.contains(parcel_rep_point) —
        fires when the parcel's representative point sits INSIDE the
        institution polygon. Crucially NOT a plain `intersects` test,
        because plenty of real residences share a boundary with parks
        (back-yard onto a park is a common Toronto pattern); they'd be
        false-positives under intersects but not under "centroid inside."
    """
    rep_point = parcel_geom.representative_point()
    for i in idx.tree.query(parcel_geom):
        g = idx.geometries[i]
        if idx.is_polygon[i]:
            if g.contains(rep_point):
                return True, idx.categories[i]
        else:
            if parcel_geom.contains(g):
                return True, idx.categories[i]
    return False, None
