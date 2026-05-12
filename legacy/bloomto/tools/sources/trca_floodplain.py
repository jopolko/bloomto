"""TRCA Regulated Area (Ont. Reg. 41/24) — riverine flood + valley + wetland overlay.

Toronto and Region Conservation Authority (TRCA) regulates development on
riverine floodplains, hazardous slopes, and provincially-significant
wetlands under Ontario Regulation 41/24 (formerly O. Reg. 166/06). The
regulatory limit polygon is the union of:
- Floodplain (regulatory storm flood elevation)
- Meander belt (river migration corridor)
- Erosion / steep-slope hazard
- Wetlands (Provincially Significant)
- Watercourse setbacks (typically 30m from top-of-bank)

A parcel intersecting this regulated area requires TRCA approval before any
development — building permits are rarely granted on floodplain land for new
residential. This is the discriminating buy/no-buy flood signal devs need;
the basement-flooding-study-areas dataset (`tools/sources/flood.py`) flags
combined-sewer service zones (universal across BloomTO's emit set), but
TRCA Regulated Area carries the actual permit gate.

Source: TRCA Open Data Portal Hub
  https://trca-camaps.opendata.arcgis.com/datasets/trca-regulated-area
  ArcGIS Item: 77304275d0214ca99d146248f4b2baa5

The download endpoint is async — POSTing/GETting the export URL returns 202
with a job-status JSON until the export completes (~30-60s on TRCA's side),
then 302-redirects to the cached GeoJSON. Polygons are published in
EPSG:26917 (UTM Zone 17N) and are reprojected to EPSG:4326 in-process.

Per parcel: representative-point inside any regulated polygon → set
`inRegulatedArea = True`. Mirrors flood.py's API shape so build_parcels.py
consumes both layers identically.
"""

import logging
import time
from pathlib import Path
from typing import NamedTuple

import ijson
import requests
from pyproj import Transformer
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.strtree import STRtree

CACHE_FILENAME = "trca_regulated_area.geojson"
ITEM_ID = "77304275d0214ca99d146248f4b2baa5"
DOWNLOAD_URL = (
    f"https://trca-camaps.opendata.arcgis.com/api/download/v1/items/"
    f"{ITEM_ID}/geojson?layers=0"
)

# Source CRS published by TRCA. Reprojected to WGS84 for compatibility with
# the rest of the BloomTO pipeline (parcel polygons + neighborhood polygons
# all in EPSG:4326).
SOURCE_CRS = "EPSG:26917"
TARGET_CRS = "EPSG:4326"

_log = logging.getLogger(__name__)

# Async-export polling knobs. The export typically completes in 10-60s; we
# allow up to 5 min before bailing.
_POLL_SECONDS = 5
_MAX_POLL_ATTEMPTS = 60


class TrcaIndex(NamedTuple):
    """Bundle of TRCA Regulated Area polygons consumed by `build_parcels.py`."""
    tree: STRtree
    polygons: list[BaseGeometry]


def _ensure_cached(cache_dir: Path) -> Path:
    """Return path to a cached TRCA GeoJSON, fetching if absent.

    The TRCA Hub download is async: GET returns 202 with a status JSON
    until the export is ready, then 302-redirects to the cached file.
    requests follows the redirect automatically; the GeoJSON arrives as
    the final response body. We poll the URL with a short delay between
    attempts and treat a non-JSON response (or a JSON without the
    `status: ExportingData/Pending` key) as a successful download.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("triggering TRCA Regulated Area export → %s", cached)
    for attempt in range(_MAX_POLL_ATTEMPTS):
        with requests.get(DOWNLOAD_URL, stream=True, timeout=180) as r:
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            # The status-JSON response is small (<5KB); the actual GeoJSON
            # is many MB. Use length to disambiguate when Content-Type
            # doesn't help.
            content = r.content
            if b'"status"' in content[:200] and b'"FeatureCollection"' not in content[:200]:
                # Still exporting — wait and retry.
                _log.info("  TRCA export in progress (attempt %d/%d, %d bytes)",
                          attempt + 1, _MAX_POLL_ATTEMPTS, len(content))
                time.sleep(_POLL_SECONDS)
                continue
            cached.write_bytes(content)
            _log.info("TRCA download: %d bytes", len(content))
            return cached
    raise RuntimeError(
        f"TRCA export did not complete after {_MAX_POLL_ATTEMPTS} polls "
        f"({_POLL_SECONDS * _MAX_POLL_ATTEMPTS}s)"
    )


def compute_trca_index(cache_dir: Path) -> TrcaIndex:
    """Load TRCA Regulated Area polygons → STRtree of WGS84 polygons.

    Uses ijson to stream the (large) GeoJSON one feature at a time, then
    reprojects each polygon from EPSG:26917 → EPSG:4326 in-process. The
    reprojection is one-time cost during build setup (a few seconds for
    ~6,300 polygons), not per-parcel.
    """
    path = _ensure_cached(Path(cache_dir))
    transformer = Transformer.from_crs(SOURCE_CRS, TARGET_CRS, always_xy=True)
    polygons: list[BaseGeometry] = []
    with path.open("rb") as fp:
        for feat in ijson.items(fp, "features.item"):
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                g = shape(geom)
            except Exception as e:
                _log.warning("skipping unparseable TRCA polygon: %s", e)
                continue
            if g.is_empty:
                continue
            try:
                projected = shapely_transform(transformer.transform, g)
            except Exception as e:
                _log.warning("skipping unprojectable TRCA polygon: %s", e)
                continue
            if projected.is_empty:
                continue
            polygons.append(projected)
    _log.info("trca: %d regulated-area polygons loaded (reprojected to WGS84)",
              len(polygons))
    return TrcaIndex(tree=STRtree(polygons), polygons=polygons)


def is_in_regulated_area(parcel_geom: BaseGeometry, idx: TrcaIndex) -> bool:
    """True iff the parcel's representative point lies inside any TRCA
    regulated polygon. Mirrors flood.is_in_flooding_area's centroid-inside
    semantics so boundary-touching parcels aren't false-positives.
    """
    rep_point = parcel_geom.representative_point()
    for i in idx.tree.query(parcel_geom):
        if idx.polygons[i].contains(rep_point):
            return True
    return False
