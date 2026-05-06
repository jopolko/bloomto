"""Toronto Basement Flooding Study Areas — flood-risk overlay source.

The City of Toronto publishes Basement Flooding Study Areas: polygons
delineating sanitary subsewersheds where basement flooding is a known
concern (older combined-sewer infrastructure, July 2013 / 2024 storm
damage). It's NOT TRCA's Ontario Reg. 41/24 riverine floodplain (which is
river/ravine flood risk, not sewer-system flood risk), but it IS Toronto's
own published flood-signal dataset and directly affects development:
basement flooding history affects insurance, foundation cost, drainage
grading, and storm-water management requirements at site-plan approval.

Per parcel we test: representative-point inside any flood-study-area
polygon → set `inFloodingStudyArea = True`. Mirrors the heritage spatial
fallback shape so `build_parcels.py` consumes it via `is_in_flooding_area`.

Future v1.3+: add TRCA Reg 41/24 riverine floodplain via TRCA ArcGIS portal
(separate dataset, not on Toronto CKAN — needs ArcGIS REST integration).
"""

import logging
from pathlib import Path
from typing import NamedTuple

import ijson
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from . import _http

CACHE_FILENAME = "basement_flooding_study_areas.geojson"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "5db3bfca-9df0-45fa-811c-de1561b124e6/resource/"
    "aad79898-9658-4adc-991f-5feb5008db58/download/"
    "basement-flooding-study-areas-4326.geojson"
)

_log = logging.getLogger(__name__)


class FloodIndex(NamedTuple):
    """Bundle of flood-study polygons consumed by `tools/build_parcels.py`."""
    tree: STRtree
    polygons: list[BaseGeometry]


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading Basement Flooding Study Areas → %s", cached)
    _http.download_with_retries(RESOURCE_URL, cached)
    return cached


def compute_flood_index(cache_dir: Path) -> FloodIndex:
    """Load the basement-flooding-study-areas GeoJSON into an STRtree of polygons."""
    path = _ensure_cached(Path(cache_dir))
    polygons: list[BaseGeometry] = []
    with path.open("rb") as fp:
        for feat in ijson.items(fp, "features.item"):
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                g = shape(geom)
            except Exception as e:
                _log.warning("skipping unparseable flood polygon: %s", e)
                continue
            if g.is_empty:
                continue
            polygons.append(g)
    _log.info("flood: %d basement-flooding-study-area polygons loaded", len(polygons))
    return FloodIndex(tree=STRtree(polygons), polygons=polygons)


def is_in_flooding_area(parcel_geom: BaseGeometry, idx: FloodIndex) -> bool:
    """True iff the parcel's representative point lies inside any
    basement-flooding-study-area polygon. Uses the centroid-inside test
    (not boundary intersection) so parcels that merely share a boundary
    with a study area aren't false-positives.
    """
    rep_point = parcel_geom.representative_point()
    for i in idx.tree.query(parcel_geom):
        if idx.polygons[i].contains(rep_point):
            return True
    return False
