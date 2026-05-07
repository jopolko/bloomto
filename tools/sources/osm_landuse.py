"""OSM landuse / amenity / non-residential building exclusion gate.

Replaces the dead Address Points USE_CODE path (Toronto Open Data nullified
GENERALUSE / GENERALUSE_CODE on 2021-07-29). Scope: parcels that are
*physically* parking lots, light industrial, active construction, or
brownfields — none of which are valid multiplex teardown candidates.

## What we exclude

| OSM tag                                | Reason |
|----------------------------------------|--------|
| `amenity=parking` / `parking_space`    | Surface lot, not a teardown |
| `amenity=fuel` / `car_wash` / `car_rental` / `truck_rental` | Auto-service plot |
| `amenity=bus_garage` / `taxi`          | Transit infra (bus_station handled in osm_ttc_stations.py) |
| `landuse=industrial` / `building=industrial` / `warehouse` | Light industrial |
| `landuse=brownfield`                   | Environmental remediation, special case |
| `landuse=construction` / `building=construction` | Already developing — too late |

## What we KEEP IN (deliberate carve-out)

`retail` and `commercial` polygons stay in the elite set — a CR-zoned
single-storey storefront with `building=retail` covering 100% of the lot is
the prime multiplex teardown target (tear down for a 6-plex above ground-floor
retail). Excluding those would kill the high-value pipeline. Validated against
a 15-parcel manual sample on 2026-05-06 where 7/15 were genuine mainstreet
storefronts.

## Threshold

`COVERAGE_THRESHOLD = 0.50` — the parcel's polygon must have ≥50% of its
area covered by a single excluded category before the gate fires.

Calibrated against the full 3,737-elite set:
- ≥30%: 133 hard excludes, includes boundary cases
- **≥50%: 99 hard excludes** ← chosen
- ≥80%: 73 (overly tight; misses 9 Bedford with parking 66%)

## Caching + freshness

Overpass response cached to `tools/cache/osm_landuse.json` (~44 MB,
63K polygons). Re-fetched only when stale (>7 days). Manual cache bust:
delete the file before next rebuild.

## Wire impact

Hard-exclusion gate, no new wire field. `meta.stats.skippedOsmLanduse`
counter recorded by the build pipeline.
"""

import json
import logging
import time
from pathlib import Path

import requests
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform
from shapely.strtree import STRtree
from pyproj import Transformer

CACHE_FILENAME = "osm_landuse.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
TORONTO_BBOX = (43.58, -79.65, 43.86, -79.10)  # (south, west, north, east)

OVERPASS_QUERY = f"""
[out:json][timeout:300];
(
  way["amenity"="parking"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="parking_space"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="bus_garage"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="taxi"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="fuel"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="car_wash"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="car_rental"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="truck_rental"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="construction"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["landuse"="construction"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["landuse"="industrial"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["landuse"="brownfield"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="industrial"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="warehouse"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // 2026-05-07 expansion: institutional building polygons missed by the
  // city institutions feeds. The Ontario Court of Justice at 10 Armoury
  // St (opened 2023) is `building=government` in OSM but isn't in any
  // Toronto Open Data institutional dataset, so it surfaced as elite.
  way["building"="government"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="public"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="hospital"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="university"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="college"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="courthouse"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="townhall"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="prison"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="university"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="college"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
);
out body geom tags;
""".strip()

# Coverage threshold — parcel area covered by any one excluded category
# must reach this fraction before the gate fires. See module docstring.
COVERAGE_THRESHOLD = 0.50

CACHE_TTL_S = 7 * 24 * 3600  # weekly refresh cadence

_log = logging.getLogger(__name__)

# WGS84 → EPSG:26917 for accurate metre-area calculations.
_LONLAT_TO_M = Transformer.from_crs("EPSG:4326", "EPSG:26917", always_xy=True)
_to_utm = _LONLAT_TO_M.transform


def _classify(tags: dict) -> str | None:
    """Return the exclusion category for a polygon, or None to skip."""
    a = tags.get("amenity", "")
    b = tags.get("building", "")
    l = tags.get("landuse", "")
    if a in ("parking", "parking_space"):
        return "parking"
    if a in ("bus_garage", "taxi"):
        return "transit"
    if a in ("fuel", "car_wash", "car_rental", "truck_rental"):
        return "auto"
    if l == "construction" or b == "construction":
        return "construction"
    if l == "industrial" or b == "industrial" or b == "warehouse":
        return "industrial"
    if l == "brownfield":
        return "brownfield"
    if (b in ("government", "public", "hospital", "university", "college")
            or a in ("courthouse", "townhall", "prison", "university", "college")):
        return "institutional"
    return None  # retail / commercial / office — kept for mainstreet teardowns


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_TTL_S


def _fetch_overpass(cache_path: Path) -> dict:
    if _is_cache_fresh(cache_path):
        _log.info("osm_landuse: using cached %s", cache_path)
        with cache_path.open(encoding="utf-8") as fp:
            return json.load(fp)
    _log.info("osm_landuse: fetching from Overpass API…")
    resp = requests.get(
        OVERPASS_URL,
        params={"data": OVERPASS_QUERY},
        headers={"User-Agent": "BloomTO/1.2 (https://joshuaopolko.com/bloomto)"},
        timeout=300,
    )
    resp.raise_for_status()
    payload = resp.json()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    _log.info("osm_landuse: cached %d elements to %s",
              len(payload.get("elements", [])), cache_path)
    return payload


def _way_polygon_metres(elem) -> Polygon | None:
    """Build a shapely Polygon (in EPSG:26917 metres) from an OSM way."""
    if elem.get("type") != "way":
        return None
    geom = elem.get("geometry") or []
    if len(geom) < 3:
        return None
    coords = [(g["lon"], g["lat"]) for g in geom]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    try:
        poly_ll = Polygon(coords)
        if not poly_ll.is_valid:
            poly_ll = poly_ll.buffer(0)
        if not poly_ll.is_valid or poly_ll.is_empty:
            return None
        return transform(_to_utm, poly_ll)
    except Exception:
        return None


def compute_landuse_exclusion_index(cache_dir: Path) -> tuple[STRtree, list[Polygon]]:
    """Return an (STRtree, polygon list) of exclusion polygons in EPSG:26917
    metres. Both are needed at lookup time — STRtree returns indices into the
    polygon list (matching the prevailing pattern in osm_ttc_stations.py).
    """
    cache = Path(cache_dir)
    payload = _fetch_overpass(cache / CACHE_FILENAME)

    polys: list[Polygon] = []
    for elem in payload.get("elements", []):
        cat = _classify(elem.get("tags") or {})
        if cat is None:
            continue
        poly = _way_polygon_metres(elem)
        if poly is None:
            continue
        polys.append(poly)

    _log.info("osm_landuse: %d exclusion polygons in EPSG:26917", len(polys))
    return STRtree(polys), polys


def is_landuse_excluded(
    parcel_geom: BaseGeometry,
    landuse_index: tuple[STRtree, list[Polygon]],
    threshold: float = COVERAGE_THRESHOLD,
) -> bool:
    """True iff ≥`threshold` of the parcel's area is covered by exclusion polygons.

    `parcel_geom` may be in WGS84 (lon/lat) or EPSG:26917. We re-project to
    EPSG:26917 if it looks like lon/lat (bounds within Toronto's WGS84 box).
    """
    tree, polys = landuse_index
    if not polys:
        return False
    # Detect lon/lat input by bounds (Toronto's WGS84 longitude is ~-79).
    minx, miny, maxx, maxy = parcel_geom.bounds
    if minx > -180 and maxx < 180 and miny > -90 and maxy < 90:
        parcel_m = transform(_to_utm, parcel_geom)
    else:
        parcel_m = parcel_geom
    if not parcel_m.is_valid:
        parcel_m = parcel_m.buffer(0)
    area = parcel_m.area
    if area < 1:
        return False
    overlap = 0.0
    for idx in tree.query(parcel_m):
        try:
            overlap += parcel_m.intersection(polys[idx]).area
        except Exception:
            pass
        # Short-circuit: once we exceed the threshold there's no need to
        # accumulate further (saves work on parcels with many overlapping
        # parking polygons, e.g. multi-tenant lots).
        if overlap / area >= threshold:
            return True
    return overlap / area >= threshold
