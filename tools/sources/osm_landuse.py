"""OSM landuse / amenity / non-residential building exclusion gate.

Replaces the dead Address Points USE_CODE path (Toronto Open Data nullified
GENERALUSE / GENERALUSE_CODE on 2021-07-29). Scope: parcels that are
*physically* parking lots, light industrial, active construction, or
brownfields — none of which are valid multiplex teardown candidates.

## Commercial-holdover detection (added 2026-05-09)

Separate from the hard-exclusion gate above, this module also tags
commercial-holdover parcels (the 505 Jarvis A&W case): a fast-food /
restaurant / bank / pharmacy on residentially-zoned land. We do NOT
exclude these — the underlying R-zone permits residential, so they're
legitimate teardown candidates once the dev buys out the lease — but we
surface the OSM amenity tag via `osmAmenityType` on the wire so the
listing row can flag "Currently A&W (commercial holdover)" before the dev
clicks through to Street View.


## What we exclude

| OSM tag                                | Reason |
|----------------------------------------|--------|
| `amenity=parking` / `parking_space`    | Surface lot, not a teardown |
| `amenity=fuel` / `car_wash` / `car_rental` / `truck_rental` | Auto-service plot |
| `amenity=bus_garage` / `taxi`          | Transit infra (bus_station handled in osm_ttc_stations.py) |
| `landuse=industrial` / `building=industrial` / `warehouse` | Light industrial |
| `landuse=brownfield`                   | Environmental remediation, special case |
| `landuse=construction` / `building=construction` | Already developing — too late |
| `power=substation` / `power=plant` / `landuse=utility` | Hydro substations & utility yards (added 2026-05-12, 109 Shaw case) |

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
  // 2026-05-07 expansion: recreational + religious + education polygons
  // missed by the city institutions feed. The institutions dataset uses
  // POINT geometries (school center), so multi-parcel complexes
  // (school + playing field + community centre on adjacent lots) only
  // flag the parcel containing the point. The OTHER parcels (playing
  // fields, side yards) appear as "vacant" lots — see 185 Close Ave
  // (Parkdale Collegiate playing field) for a representative case.
  way["amenity"="school"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="kindergarten"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="place_of_worship"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="hospital"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="library"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="community_centre"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="fire_station"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="school"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="kindergarten"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="religious"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="church"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="cathedral"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="chapel"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="mosque"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="synagogue"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["building"="temple"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["leisure"="park"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["leisure"="pitch"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["leisure"="playground"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["leisure"="stadium"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["leisure"="sports_centre"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["leisure"="recreation_ground"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["leisure"="track"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["landuse"="religious"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["landuse"="cemetery"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["landuse"="education"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["landuse"="recreation_ground"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // Commercial holdover (added 2026-05-09): hospitality + retail
  // services on what may be residentially-zoned land. NOT excluded —
  // surfaced on the wire as `osmAmenityType` so the row can flag the
  // commercial reality (505 Jarvis A&W case) before Street View click.
  way["amenity"="fast_food"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="restaurant"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="cafe"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="bar"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="pub"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="bank"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="pharmacy"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["amenity"="post_office"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // Shop tags (added 2026-05-09): liquor stores, convenience, hardware,
  // car dealerships, etc — physical retail on what may be R-zoned land.
  // User direction: hard-exclude all commercial uses, not just amenity-
  // tagged hospitality. Same intersection-ratio gate as fast_food etc.
  way["shop"="alcohol"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["shop"="convenience"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["shop"="supermarket"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["shop"="hardware"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["shop"="car"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["shop"="car_repair"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["shop"="furniture"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["shop"="department_store"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  // Utility infrastructure (added 2026-05-12): Hydro / power substations
  // and utility yards. 109 Shaw St case: parcel is the Toronto Hydro
  // Bellwoods substation but city zoning reads "R (d1.0) (x806)" — the
  // x806 use-exception isn't parsed by our ETL, and the classifier sees
  // the substation building footprint as "detached." OSM tags substations
  // as `power=substation`; folding them into the exclusion query catches
  // the physical-use truth without needing to parse Toronto's full
  // Schedule of Exceptions.
  way["power"="substation"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["power"="plant"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["man_made"="substation"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
  way["landuse"="utility"]({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
);
out body geom tags;
""".strip()

# Amenity tags that flag a parcel as a commercial holdover (i.e., a
# business sitting on land that may be residentially zoned). NOT used
# for hard exclusion — the dev can buy out the lease and redevelop.
# Surfaced via `osm_amenity_type()` for the row's "Currently A&W"
# badge. Mutually exclusive with the exclusion `_classify()` set:
# fuel/car_wash/car_rental/truck_rental are EXCLUDED (auto cluster);
# fast_food/restaurant/etc are HOLDOVER (this set).
HOLDOVER_AMENITIES = {
    'fast_food', 'restaurant', 'cafe', 'bar', 'pub',
    'bank', 'pharmacy', 'post_office',
}

# Shop= tag values that mark a parcel as commercial. Treated the same as
# HOLDOVER_AMENITIES in `_classify_amenity` so they surface in
# `osmAmenityType` and trigger the frontend hide. Added 2026-05-09 per
# user direction (no liquor stores, no gas stations, no commercial uses).
HOLDOVER_SHOPS = {
    'alcohol', 'convenience', 'supermarket', 'hardware',
    'car', 'car_repair', 'furniture', 'department_store',
}

# Holdover overlap threshold — the AMENITY building's footprint must sit
# at least this fraction inside the parcel polygon for the parcel to be
# tagged. Same intersection-ratio principle as EXISTING_BUILDING_OVERLAP_RATIO
# in build_parcels.py: if the amenity building is mostly on this parcel,
# the dev needs to know.
HOLDOVER_OVERLAP_RATIO = 0.50

# Coverage threshold — parcel area covered by exclusion polygons must
# reach this fraction before the gate fires. See module docstring.
COVERAGE_THRESHOLD = 0.50
# Stricter threshold for leisure / institutional categories. Parks,
# playgrounds, pitches, schools, places of worship, etc. are smaller
# polygons that often partially overlap residential parcel polygons
# (sliver lots adjacent to a parkette, school playing-field abutting
# residential lots, etc). 25% catches partial-parkette parcels like
# 107 Indian Rd that the 50% gate misses without nuking legitimate
# residential lots elsewhere. Tightened 2026-05-09 from the unified
# 0.50 after the user surfaced the Indian Rd 100-block cluster.
LEISURE_INSTITUTIONAL_COVERAGE_THRESHOLD = 0.25
LEISURE_INSTITUTIONAL_CATEGORIES = frozenset({"institutional"})

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
    leisure = tags.get("leisure", "")
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
    # Utility (added 2026-05-12 — 109 Shaw St Toronto Hydro Bellwoods
    # substation case). OSM tags Hydro substations as power=substation
    # and increasingly landuse=utility. Catches the physical-use truth
    # without relying on Toronto's Schedule of Exceptions (x806 etc.).
    p = tags.get("power", "")
    m = tags.get("man_made", "")
    if p in ("substation", "plant") or m == "substation" or l == "utility":
        return "utility"
    # Institutional umbrella — government / hospital / university / college /
    # courthouse / townhall / prison / school / kindergarten / place of
    # worship / library / community centre / fire station / religious or
    # education buildings + landuse, plus recreational polygons (parks,
    # pitches, playgrounds, stadiums, sports centres, recreation grounds,
    # tracks, cemeteries) which are non-buildable on the multiplex thesis.
    if (b in (
            "government", "public", "hospital", "university", "college",
            "school", "kindergarten",
            "religious", "church", "cathedral", "chapel",
            "mosque", "synagogue", "temple",
        )
        or a in (
            "courthouse", "townhall", "prison", "university", "college",
            "school", "kindergarten", "place_of_worship", "hospital",
            "library", "community_centre", "fire_station",
        )
        or l in ("religious", "cemetery", "education", "recreation_ground")
        or leisure in (
            "park", "pitch", "playground", "stadium",
            "sports_centre", "recreation_ground", "track",
        )):
        return "institutional"
    return None  # retail / commercial / office — kept for mainstreet teardowns


def _classify_amenity(tags: dict) -> str | None:
    """Return the OSM `amenity` or `shop` tag if it's in HOLDOVER_AMENITIES
    / HOLDOVER_SHOPS, or None otherwise. Verbatim tag passes through (e.g.,
    "fast_food" or "alcohol") so the frontend can render directly.
    Shop tags prefixed with "shop_" to disambiguate from amenity tags
    in downstream display logic.
    """
    a = tags.get('amenity', '')
    if a in HOLDOVER_AMENITIES:
        return a
    s = tags.get('shop', '')
    if s in HOLDOVER_SHOPS:
        return f'shop_{s}'
    return None


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
    # POST not GET — the query string grew past the Overpass GET URI limit
    # after the 2026-05-12 utility-infra additions. Overpass accepts either
    # method with `data=` as form body or URL param.
    resp = requests.post(
        OVERPASS_URL,
        data={"data": OVERPASS_QUERY},
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


def compute_landuse_exclusion_index(
    cache_dir: Path,
) -> tuple[STRtree, list[Polygon], list[str]]:
    """Return (STRtree, polys, cats) for exclusion polygons in EPSG:26917
    metres. `cats[i]` is the coarse `_classify` category for `polys[i]`
    (e.g., "parking", "industrial", "institutional"). Categories enable
    the per-category coverage threshold in `is_landuse_excluded` —
    institutional / leisure polygons fire at a stricter 25% overlap
    (catches partial-parkette residue), other categories stay at 50%.
    """
    cache = Path(cache_dir)
    payload = _fetch_overpass(cache / CACHE_FILENAME)

    polys: list[Polygon] = []
    cats: list[str] = []
    for elem in payload.get("elements", []):
        cat = _classify(elem.get("tags") or {})
        if cat is None:
            continue
        poly = _way_polygon_metres(elem)
        if poly is None:
            continue
        polys.append(poly)
        cats.append(cat)

    _log.info("osm_landuse: %d exclusion polygons in EPSG:26917", len(polys))
    return STRtree(polys), polys, cats


def compute_amenity_holdover_index(
    cache_dir: Path,
) -> tuple[STRtree, list[Polygon], list[str]]:
    """Return (STRtree, polys, types) for commercial-holdover amenity
    polygons in EPSG:26917 metres. `types[i]` is the OSM amenity tag
    string for `polys[i]` (e.g., "fast_food", "restaurant"). Re-uses the
    cached Overpass payload from `_fetch_overpass`, so calling this
    after `compute_landuse_exclusion_index` doesn't double-fetch.
    """
    cache = Path(cache_dir)
    payload = _fetch_overpass(cache / CACHE_FILENAME)

    polys: list[Polygon] = []
    types: list[str] = []
    for elem in payload.get("elements", []):
        amenity = _classify_amenity(elem.get("tags") or {})
        if amenity is None:
            continue
        poly = _way_polygon_metres(elem)
        if poly is None:
            continue
        polys.append(poly)
        types.append(amenity)

    _log.info("osm_landuse: %d holdover-amenity polygons in EPSG:26917", len(polys))
    return STRtree(polys), polys, types


def osm_amenity_type(
    parcel_geom: BaseGeometry,
    amenity_index: tuple[STRtree, list[Polygon], list[str]],
    threshold: float = HOLDOVER_OVERLAP_RATIO,
) -> str | None:
    """Return the OSM amenity tag (e.g., "fast_food") of the holdover
    building that substantially sits on this parcel, or None.

    "Substantially sits on" is the same intersection-ratio principle as
    `EXISTING_BUILDING_OVERLAP_RATIO` in build_parcels.py: the AMENITY
    polygon must have ≥`threshold` of its area inside the parcel, so a
    neighbouring fast-food joint that just clips this lot's boundary
    doesn't trigger the flag.

    `parcel_geom` may be in WGS84 (lon/lat) or EPSG:26917; we re-project
    to UTM if it looks like lon/lat (matches `is_landuse_excluded`).
    Returns the FIRST match by overlap ratio — multi-amenity parcels
    are vanishingly rare.
    """
    tree, polys, types = amenity_index
    if not polys:
        return None
    minx, miny, maxx, maxy = parcel_geom.bounds
    if minx > -180 and maxx < 180 and miny > -90 and maxy < 90:
        parcel_m = transform(_to_utm, parcel_geom)
    else:
        parcel_m = parcel_geom
    if not parcel_m.is_valid:
        parcel_m = parcel_m.buffer(0)
    if parcel_m.is_empty:
        return None
    best_type: str | None = None
    best_overlap = 0.0
    for idx in tree.query(parcel_m):
        try:
            inter = parcel_m.intersection(polys[idx])
            if inter.is_empty:
                continue
            amenity_area = polys[idx].area
            if amenity_area <= 0:
                continue
            ratio = inter.area / amenity_area
            if ratio >= threshold and ratio > best_overlap:
                best_overlap = ratio
                best_type = types[idx]
        except Exception:
            pass
    return best_type


def is_landuse_excluded(
    parcel_geom: BaseGeometry,
    landuse_index: tuple[STRtree, list[Polygon], list[str]],
    threshold: float = COVERAGE_THRESHOLD,
    leisure_threshold: float = LEISURE_INSTITUTIONAL_COVERAGE_THRESHOLD,
) -> bool:
    """True iff parcel overlap with exclusion polygons exceeds threshold.

    Per-category thresholds (as of 2026-05-09):
      - "institutional" (schools, parks, religious, recreation): fires at
        ≥`leisure_threshold` (default 25%) — catches partial-parkette /
        school-edge residue parcels (107 Indian Rd case).
      - All other categories (parking, industrial, brownfield,
        construction, auto, transit): fires at ≥`threshold` (default 50%).
    The two categories are tracked separately and the parcel is excluded
    if EITHER pool reaches its respective threshold.

    `parcel_geom` may be in WGS84 (lon/lat) or EPSG:26917. We re-project to
    EPSG:26917 if it looks like lon/lat (bounds within Toronto's WGS84 box).
    """
    # Backwards-compat: tolerate the legacy 2-tuple shape (no categories)
    # so the function doesn't blow up if a stale caller still passes the
    # old signature. Treats every polygon as the strict-50% bucket.
    if len(landuse_index) == 2:
        tree, polys = landuse_index
        cats = ["other"] * len(polys)
    else:
        tree, polys, cats = landuse_index
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
    overlap_other = 0.0
    overlap_leisure = 0.0
    for idx in tree.query(parcel_m):
        try:
            inter_area = parcel_m.intersection(polys[idx]).area
        except Exception:
            continue
        cat = cats[idx] if idx < len(cats) else "other"
        if cat in LEISURE_INSTITUTIONAL_CATEGORIES:
            overlap_leisure += inter_area
        else:
            overlap_other += inter_area
        # Short-circuit when either pool exceeds its threshold.
        if (overlap_other / area >= threshold
                or overlap_leisure / area >= leisure_threshold):
            return True
    return (overlap_other / area >= threshold
            or overlap_leisure / area >= leisure_threshold)
