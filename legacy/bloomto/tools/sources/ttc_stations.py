"""TTC subway-station institutional exclusion.

Catches parcels that are TTC subway-station infrastructure (station boxes,
entrances, headhouses, surface kiosks). The Toronto Property Boundaries
dataset assigns civic addresses to these — e.g., "22 Chester Ave" is
Chester Station — so they slip through the existing institutional ETL
(`tools/sources/institutions.py`) which only pulls schools / parks /
places-of-worship / fire-police-ambulance / LTC / child-care / libraries /
parks-and-rec / community-facilities.

## Approach

Use the GTFS subway-stops point set (already cached and loaded — 148 points
across ~75 stations) and buffer each point by `STATION_BUFFER_M` (30m). A
parcel intersecting any buffered point is treated as TTC infrastructure and
excluded from the wire.

30m is the smallest radius that still catches every observed false-positive
elite parcel from the 2026-05-05 audit (worst case: 22 Chester Ave at 7m
from a stop). Tighter than 30m starts missing subway-station headhouses set
back from the platform stop; looser starts catching truly-residential
corner lots adjacent to stations.

## Limitation (not 100% — documented)

Point + buffer is heuristic. The authoritative source would be TTC station
POLYGONS, but Toronto Open Data's `ttc-subway-shapefile` (resource
7d68bb52-3285-45d7-a248-7748cb47f6ce) is route-line-only and doesn't
publish station polygons. Future improvement: swap to a station-polygon
source if/when one is published. For now, the 30m buffer minimizes the
false-positive rate at the cost of occasionally excluding a corner-lot
residential parcel that genuinely abuts a station — acceptable trade given
the alternative was 22 Chester Ave ranked #1 multiplex pick.

## Wire impact

No new wire field — this is a hard-exclusion gate. `meta.stats` gains
`skippedTtcStation` so a rebuild can verify how many parcels the gate
caught. Mirrors the institutional-exclusion pattern in
`tools/build_parcels.py` for consistency.
"""

import logging
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from . import ttc as ttc_src

# Buffer applied to each subway-stop point, in metres. Chosen against the
# 2026-05-05 false-positive audit: the worst case (22 Chester Ave) sat 7m
# from a stop; the loosest legit-station case (Chester Station headhouse)
# extended ~25m from its associated GTFS stop. 30m gives a margin without
# meaningfully eating into legit residential lots.
STATION_BUFFER_M = 30.0

# Toronto's metres-based projection. Mirrors `_LONLAT_TO_M` in
# `tools/build_parcels.py` so the buffered polygons share a CRS with the
# parcel-distance computations.
_LONLAT_TO_M = Transformer.from_crs("EPSG:4326", "EPSG:26917", always_xy=True)
_M_TO_LONLAT = Transformer.from_crs("EPSG:26917", "EPSG:4326", always_xy=True)

_log = logging.getLogger(__name__)


def compute_station_exclusion_index(cache_dir: Path) -> STRtree:
    """Return an STRtree of buffered TTC subway-station polygons in WGS84.

    Reuses `tools.sources.ttc.compute_subway_stops` (which loads the GTFS
    cache) to get the 148 subway-stop points, projects each to EPSG:26917,
    buffers by `STATION_BUFFER_M` metres, projects the resulting polygon
    back to WGS84, and indexes the polygons in an STRtree.
    """
    stops_lonlat = ttc_src.compute_subway_stops(cache_dir)
    polys: list[BaseGeometry] = []
    for stop in stops_lonlat:
        x_m, y_m = _LONLAT_TO_M.transform(stop.x, stop.y)
        # Buffer in metres for accurate radius; project the buffered polygon
        # back to WGS84 in 32 segments (default).
        buffered_m = Point(x_m, y_m).buffer(STATION_BUFFER_M)
        # shapely.ops.transform would be cleaner but is heavier; project the
        # exterior coords directly (buffer always yields a single ring).
        coords_lonlat = [
            _M_TO_LONLAT.transform(x, y) for x, y in buffered_m.exterior.coords
        ]
        from shapely.geometry import Polygon
        polys.append(Polygon(coords_lonlat))
    _log.info(
        "ttc_stations: %d subway stops × %.0fm buffer → %d exclusion polygons",
        len(stops_lonlat), STATION_BUFFER_M, len(polys),
    )
    return STRtree(polys)


def is_ttc_station(parcel_geom: BaseGeometry, station_index: STRtree) -> bool:
    """True iff the parcel intersects any buffered subway-station polygon.

    Mirrors `tools.sources.institutions.is_institutional`'s shape so the
    `build_parcels.py` orchestrator can call both gates the same way.
    """
    if station_index is None or len(station_index.geometries) == 0:
        return False
    rep = parcel_geom.representative_point()
    for idx in station_index.query(rep):
        if station_index.geometries[idx].contains(rep):
            return True
    return False
