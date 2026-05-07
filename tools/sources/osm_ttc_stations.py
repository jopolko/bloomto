"""TTC station + transit-infrastructure exclusion via OpenStreetMap polygons.

Replaces the buffered-points heuristic in `tools/sources/ttc_stations.py`
(2026-05-06 first attempt — 30m buffer around 148 GTFS stops, missed
station-adjacent parcels like 9 Bedford Rd at 128m from St. George).

## Why OSM, not TTC

TTC publishes only GTFS (stop POINTS, route LINES) and a route-line
shapefile via Toronto Open Data. Their internal CAD/GIS with station
building polygons is not released (security: escape routes; legal:
leased land; internal: proprietary). OpenStreetMap contributors traced
TTC subway station footprints from the same satellite imagery the
city's Property Boundaries dataset is drawn against — so the polygons
match parcel geometry tightly. Toronto's OSM coverage of TTC
infrastructure is mature (10+ years).

## What we exclude

- TTC subway stations (Lines 1, 2, 4 — Yonge-University, Bloor-Danforth, Sheppard)
- Subway-station building footprints (the main station box + ancillary
  ventilation / mechanical buildings within 200m)
- Bus terminals attached to subway stations (Kipling, Kennedy, Don Mills, etc.)
- Streetcar barns / depots (Russell Yard, Roncesvalles Carhouse, etc.)

## What we DON'T exclude (separate concerns)

- GO Train stations (Metrolinx, not TTC; a separate dataset issue)
- Light rail stations (Eglinton Crosstown — soon-to-open; user may want
  these excluded later but the data isn't stable yet since the line is
  pre-revenue)
- TTC operations / admin buildings not adjacent to revenue stations

## Caching + freshness

Overpass API responses are cached to `tools/cache/osm_ttc_stations.geojson`.
Re-fetched only when the cached file is missing OR older than the rebuild
cadence (~weekly+). Manual cache bust: delete the file before next rebuild.

## Wire impact

No new wire field — hard-exclusion gate. `meta.stats.skippedTtcStation`
counter (already added in the 2026-05-06 first iteration) measures gate
effectiveness.
"""

import json
import logging
import time
from pathlib import Path

import requests
from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

CACHE_FILENAME = "osm_ttc_stations.geojson"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Toronto bounding box (a bit generous on edges to catch the suburban stations
# at Kipling / Kennedy / Finch).
TORONTO_BBOX = (43.58, -79.65, 43.86, -79.10)  # (south, west, north, east)

# Overpass query: TTC subway stations as polygons + as nodes (we'll associate
# nearby building polygons with them client-side). Excludes GO stations
# (Metrolinx, network!=TTC) and light_rail (Eglinton Crosstown — pre-revenue).
#
# 2026-05-06 expansion: added railway=subway_entrance because each station has
# 4-12 separate entrance nodes scattered around it (e.g., St. George has
# "Bedford Road Entrance" and "OISE Entrance" tagged this way), and those
# entrances often land on parcels DISTINCT from the main station box —
# producing the 9 Bedford Rd / 11 Bedford Rd false-positive elites the
# previous query missed.
OVERPASS_QUERY = f"""
[out:json][timeout:90];
(
  // Subway station NODES — canonical point per station
  node["station"="subway"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // Subway station POLYGONS (some stations mapped as ways, esp. Ontario Line future stops)
  way["station"="subway"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // Train-station BUILDINGS (the subway-station box itself; we filter to
  // those within proximity of a subway node so GO-only stations are dropped)
  way["building"="train_station"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // Bus terminals (Kipling, Kennedy, Don Mills attached to subway stations)
  way["amenity"="bus_station"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // Subway entrance NODES — separate headhouses scattered around stations
  // (Bedford Road Entrance, OISE Entrance, etc.). Buffered with the same
  // fallback radius applied to plain station nodes.
  node["railway"="subway_entrance"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // Subway entrance POLYGONS (rare — most are nodes, but a few are mapped as
  // building outlines when the entrance is a freestanding headhouse).
  way["railway"="subway_entrance"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // Light rail stations (Eglinton Crosstown, Finch West LRT). Eglinton
  // opened 2025; geometry is now stable. User direction 2026-05-06: any
  // station infrastructure should be caught regardless of system.
  node["station"="light_rail"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["station"="light_rail"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["public_transport"="station"]["network"~"GO|Metrolinx|Eglinton"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // Streetcar / tram-stop POLYGONS only (transit centers, transit
  // platforms mapped as ways). Bus_stop / tram_stop NODES are road-
  // shoulder pinpoints, not parcels — adding them would over-reach.
  way["railway"="tram_stop"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["public_transport"="platform"]["network"="TTC"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
);
out body geom tags;
""".strip()

# Building polygons farther than this from any subway node are dropped — most
# likely GO Train stations or unrelated buildings. 250m catches station-attached
# bus terminals and ancillary ventilation buildings without overreaching.
BUILDING_TO_STATION_MAX_M = 250.0

# Fallback buffer for subway stations that exist as a NODE in OSM but have no
# building polygon mapped. The tighter 30m used in the prior buffered-points
# implementation missed Bedford Rd cases (~128m); we go to 50m here as a
# pessimistic pass — still narrower than the over-reach of 100m+.
NODE_FALLBACK_BUFFER_M = 50.0
# Entrance nodes are precise (mapped right at the head-house door). 30m
# is the inflection point where both 11 Bedford Rd (entrance node 41m
# from centroid) and 9 Bedford Rd (4930m² plaza, headhouse at one edge)
# get caught via parcel-polygon intersection — the entrance buffer reaches
# the parcel boundary at ~30m, even when the centroid is farther.
# Empirically calibrated on the elite set: 20m catches 27 elites (no
# Bedford); 30m catches 58 (both Bedford); 50m catches 143 (with too
# many adjacent residential false positives). 30m strikes the cleanest
# precision/recall balance for principled inclusion in the wire.
ENTRANCE_FALLBACK_BUFFER_M = 30.0

CACHE_TTL_S = 7 * 24 * 3600  # weekly refresh cadence

_log = logging.getLogger(__name__)

# WGS84 → EPSG:26917 for accurate metre buffers.
from pyproj import Transformer  # noqa: E402
_LONLAT_TO_M = Transformer.from_crs("EPSG:4326", "EPSG:26917", always_xy=True)
_M_TO_LONLAT = Transformer.from_crs("EPSG:26917", "EPSG:4326", always_xy=True)


def _haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6_371_008.8
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_TTL_S


def _fetch_overpass(cache_path: Path) -> dict:
    """Fetch the OSM query and return the parsed JSON. Caches to disk."""
    if _is_cache_fresh(cache_path):
        _log.info("osm_ttc_stations: using cached %s", cache_path)
        with cache_path.open(encoding="utf-8") as fp:
            return json.load(fp)

    _log.info("osm_ttc_stations: fetching from Overpass API…")
    # Overpass main mirror requires a non-default User-Agent and prefers
    # GET with `data` URL-encoded (returns 406 on x-www-form-urlencoded
    # POST, and on requests' default `python-requests/X` UA).
    resp = requests.get(
        OVERPASS_URL,
        params={"data": OVERPASS_QUERY},
        headers={"User-Agent": "BloomTO/1.2 (https://joshuaopolko.com/bloomto)"},
        timeout=120,
    )
    resp.raise_for_status()
    payload = resp.json()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    _log.info("osm_ttc_stations: cached %d elements to %s",
              len(payload.get("elements", [])), cache_path)
    return payload


def _node_lonlat(elem) -> tuple[float, float] | None:
    if elem.get("type") != "node":
        return None
    return (elem.get("lon"), elem.get("lat"))


def _way_polygon(elem):
    """Build a shapely Polygon from an OSM way's geometry list."""
    if elem.get("type") != "way":
        return None
    geom = elem.get("geometry") or []
    if len(geom) < 3:
        return None
    coords = [(g["lon"], g["lat"]) for g in geom]
    if coords[0] != coords[-1]:
        coords.append(coords[0])  # close the ring
    try:
        poly = Polygon(coords)
        if not poly.is_valid:
            poly = poly.buffer(0)  # fix self-intersections
        return poly if poly.is_valid and not poly.is_empty else None
    except Exception:
        return None


def compute_station_exclusion_index(cache_dir: Path) -> STRtree:
    """Return an STRtree of TTC station + transit-infrastructure polygons in
    WGS84 to be excluded from the wire.

    Construction:
      1. Pull subway station nodes (conceptual points) and candidate building
         polygons (ways) from Overpass.
      2. Keep building polygons that lie within `BUILDING_TO_STATION_MAX_M` of
         any subway node — drops GO Train stations and unrelated buildings.
      3. For subway nodes whose location is NOT covered by any kept polygon,
         add a `NODE_FALLBACK_BUFFER_M`-radius buffered point (in metres,
         reprojected to WGS84). Catches stations that have no building polygon
         mapped in OSM yet.
    """
    cache = Path(cache_dir)
    payload = _fetch_overpass(cache / CACHE_FILENAME)

    nodes = []  # list of (name, lon, lat) for subway-station nodes
    entrance_nodes = []  # list of (name, lon, lat) for subway_entrance nodes
    candidate_polys = []  # list of (name, poly, station_tag)
    for elem in payload.get("elements", []):
        tags = elem.get("tags") or {}
        name = tags.get("name", "")
        # 2026-05-06 user direction: exclude transit-infrastructure parcels
        # of every flavor from the listings (we keep using their *values*
        # for transit-distance scoring, but the lots themselves shouldn't
        # surface as multiplex picks). Previously we dropped GO and
        # light_rail here — those exclusions are removed so GO stations
        # (Mimico, Langstaff, Agincourt, Cooksville, Finch GO terminal)
        # and Eglinton Crosstown LRT stations are caught by the gate.

        if elem["type"] == "node" and tags.get("station") in ("subway", "light_rail"):
            ll = _node_lonlat(elem)
            if ll:
                nodes.append((name, ll[0], ll[1]))
            continue

        if elem["type"] == "node" and tags.get("railway") == "subway_entrance":
            ll = _node_lonlat(elem)
            if ll:
                entrance_nodes.append((name, ll[0], ll[1]))
            continue

        if elem["type"] == "way":
            building = tags.get("building")
            station = tags.get("station")
            amenity = tags.get("amenity")
            railway = tags.get("railway")
            public_transport = tags.get("public_transport")
            network = (tags.get("network") or "").upper()
            operator = (tags.get("operator") or "").upper()
            is_metrolinx = (
                "GO TRANSIT" in network or "METROLINX" in operator
                or "GO TRANSIT" in operator or "METROLINX" in network
            )
            keep = (
                station in ("subway", "light_rail")
                or building == "train_station"  # subway boxes + GO stations
                or amenity == "bus_station"     # bus terminals (incl. GO)
                or railway in ("subway_entrance", "tram_stop")
                or (public_transport == "platform" and network == "TTC")
                or (public_transport == "station" and is_metrolinx)
            )
            if not keep:
                continue
            poly = _way_polygon(elem)
            if poly is None:
                continue
            candidate_polys.append((name, poly, station))

    _log.info(
        "osm_ttc_stations: parsed %d subway nodes, %d entrance nodes, "
        "%d candidate polygons",
        len(nodes), len(entrance_nodes), len(candidate_polys),
    )

    # Filter polygons to those within BUILDING_TO_STATION_MAX_M of a subway node.
    # `station=subway`-tagged polygons skip the proximity test (already specific).
    kept_polys = []
    for (name, poly, station) in candidate_polys:
        if station == "subway":
            kept_polys.append(poly)
            continue
        # Use polygon centroid for distance test — cheap, sufficient for filtering.
        c = poly.centroid
        nearest_m = min(
            _haversine_m(c.y, c.x, n_lat, n_lon)
            for (_, n_lon, n_lat) in nodes
        ) if nodes else float("inf")
        if nearest_m <= BUILDING_TO_STATION_MAX_M:
            kept_polys.append(poly)

    _log.info(
        "osm_ttc_stations: kept %d polygons after proximity filter",
        len(kept_polys),
    )

    # For each subway NODE not already covered by a kept polygon, add a buffered
    # point (in metres, reprojected to WGS84).
    polys_tree = STRtree(kept_polys) if kept_polys else None
    fallback_buffers = []
    for (name, lon, lat) in nodes:
        node_pt = Point(lon, lat)
        covered = False
        if polys_tree is not None:
            for idx in polys_tree.query(node_pt):
                if kept_polys[idx].contains(node_pt):
                    covered = True
                    break
        if covered:
            continue
        # Buffer in projected metres, then convert ring coords back to WGS84.
        x_m, y_m = _LONLAT_TO_M.transform(lon, lat)
        buf_m = Point(x_m, y_m).buffer(NODE_FALLBACK_BUFFER_M)
        coords_lonlat = [
            _M_TO_LONLAT.transform(x, y) for x, y in buf_m.exterior.coords
        ]
        fallback_buffers.append(Polygon(coords_lonlat))

    _log.info(
        "osm_ttc_stations: added %d fallback %.0fm buffers for unmapped stations",
        len(fallback_buffers), NODE_FALLBACK_BUFFER_M,
    )

    # Buffer subway-entrance nodes uniformly — these are precise headhouse
    # locations and almost never have a building polygon mapped, so we always
    # add the buffered point (no proximity-skip).
    entrance_buffers = []
    for (name, lon, lat) in entrance_nodes:
        x_m, y_m = _LONLAT_TO_M.transform(lon, lat)
        buf_m = Point(x_m, y_m).buffer(ENTRANCE_FALLBACK_BUFFER_M)
        coords_lonlat = [
            _M_TO_LONLAT.transform(x, y) for x, y in buf_m.exterior.coords
        ]
        entrance_buffers.append(Polygon(coords_lonlat))
    _log.info(
        "osm_ttc_stations: added %d entrance %.0fm buffers",
        len(entrance_buffers), ENTRANCE_FALLBACK_BUFFER_M,
    )

    all_polys: list[BaseGeometry] = kept_polys + fallback_buffers + entrance_buffers
    _log.info(
        "osm_ttc_stations: %d total exclusion polygons in STRtree",
        len(all_polys),
    )
    return STRtree(all_polys)


def is_ttc_station(parcel_geom: BaseGeometry, station_index: STRtree) -> bool:
    """True iff the parcel intersects any TTC station / transit-infra polygon.

    Uses geometric intersection, not point-in-polygon — the prior
    representative_point test missed plaza-style station parcels where the
    headhouse sits at one edge of a wide lot (9 Bedford Rd: 70m-wide plaza,
    headhouse at the corner; representative point is mid-plaza, well outside
    a 50m headhouse buffer). Intersection correctly catches any overlap.
    """
    if station_index is None or len(station_index.geometries) == 0:
        return False
    for idx in station_index.query(parcel_geom):
        if station_index.geometries[idx].intersects(parcel_geom):
            return True
    return False
