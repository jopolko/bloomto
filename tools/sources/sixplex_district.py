"""Sixplex-eligible district overlay (Toronto June 2025 multiplex by-law).

In June 2025, Toronto City Council expanded the as-of-right multiplex
permit pathway to allow up to 6 dwelling units (sixplex) — but ONLY in:

  1. Toronto and East York Community Council District (the central
     pre-amalgamation Toronto + East York region — south of roughly
     Eglinton Ave, between the Humber River and Victoria Park Ave).
  2. Ward 23 (Scarborough North).

Outside these zones, the citywide as-of-right cap is 4 (since 2023).

We resolve eligibility per-parcel via:

- Toronto Community Council Boundaries dataset (GeoJSON) — gives the
  exact T&EY District polygon. Point-in-polygon test against the parcel's
  representative point.
- Ward 23 (Scarborough North) — approximated by the lat/lng bounding
  box (43.79–43.85 lat, -79.30 to -79.20 lng). The bbox is conservative
  vs the actual irregular ward shape; a developer relying on the flag
  always confirms the exact lot via the City portal before pro forma.

The flag is a coarse "your lot is in an eligible district" signal; the
actual sixplex permit gate also requires standards compliance (lot
frontage, setbacks, etc.) handled separately at site plan approval.
"""

import logging
from pathlib import Path
from typing import NamedTuple

import ijson
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from . import _http

CACHE_FILENAME = "community_council_boundaries.geojson"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "5709d6ff-75a3-493d-864d-ca1b49711074/resource/"
    "cc935c56-dbcd-4035-b156-a7f8f8eae68b/download/"
    "community-council-boundaries-data-4326.geojson"
)

# T&EY district name as it appears in the AREA_NAME field of the source.
TEY_AREA_NAME = "Toronto and East York Community Council"

# Ward 23 (Scarborough North) bounding box. Approximate — see module docstring.
# Conservative envelope: real ward boundary is somewhat smaller in places, so
# this slightly over-flags. The developer's site-plan-approval check is the
# authoritative gate; this flag is a "candidate lot" indicator.
WARD_23_BBOX = {
    "lat_min": 43.785,
    "lat_max": 43.855,
    "lng_min": -79.305,
    "lng_max": -79.185,
}

_log = logging.getLogger(__name__)


class SixplexIndex(NamedTuple):
    tey_polygons: list[BaseGeometry]   # multipolygon as a list of polygons
    tey_tree: STRtree


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading Community Council Boundaries → %s", cached)
    _http.download_with_retries(RESOURCE_URL, cached)
    return cached


def compute_sixplex_index(cache_dir: Path) -> SixplexIndex:
    """Load the T&EY District polygon for point-in-polygon tests."""
    path = _ensure_cached(Path(cache_dir))
    polys: list[BaseGeometry] = []
    with path.open("rb") as fp:
        for feat in ijson.items(fp, "features.item"):
            props = feat.get("properties") or {}
            if props.get("AREA_NAME") != TEY_AREA_NAME:
                continue
            try:
                g = shape(feat.get("geometry") or {})
            except Exception as e:
                _log.warning("sixplex_district: skipping unparseable T&EY geom: %s", e)
                continue
            if g.is_empty:
                continue
            # MultiPolygon → flatten to polygons for STRtree (faster bbox prune)
            if g.geom_type == "MultiPolygon":
                for sub in g.geoms:
                    if not sub.is_empty:
                        polys.append(sub)
            else:
                polys.append(g)
    _log.info(
        "sixplex_district: %d T&EY District polygon(s) loaded "
        "(Ward 23 bbox: lat[%.3f-%.3f], lng[%.3f-%.3f])",
        len(polys),
        WARD_23_BBOX["lat_min"], WARD_23_BBOX["lat_max"],
        WARD_23_BBOX["lng_min"], WARD_23_BBOX["lng_max"],
    )
    return SixplexIndex(tey_polygons=polys, tey_tree=STRtree(polys))


def is_sixplex_eligible(parcel_geom: BaseGeometry, idx: SixplexIndex) -> bool:
    """True iff the parcel's representative point is in T&EY District OR
    inside the Ward 23 (Scarborough North) bounding box.
    """
    pt = parcel_geom.representative_point()
    # Cheap bbox check first (Ward 23)
    if (WARD_23_BBOX["lat_min"] <= pt.y <= WARD_23_BBOX["lat_max"]
            and WARD_23_BBOX["lng_min"] <= pt.x <= WARD_23_BBOX["lng_max"]):
        return True
    # T&EY polygon check
    for i in idx.tey_tree.query(pt):
        if idx.tey_polygons[i].contains(pt):
            return True
    return False
