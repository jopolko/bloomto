"""Compute `bike` (cycling infrastructure km, normalized 0–100) per neighborhood from
the Cycling Network 4326 GeoJSON. See `tools/README.md` § Cycling Source for the locked
schema and length-aggregation approach.
"""

import json
import logging
import sys
import time
from pathlib import Path

import requests
from pyproj import Geod
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from .neighborhoods import Neighborhood

PACKAGE_ID = "cycling-network"
RESOURCE_ID = "023da9a2-8848-4e10-9cad-e7f9119cd874"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "abbe5ee3-e249-4f86-a219-f0022eaddcc9/resource/"
    f"{RESOURCE_ID}/download/cycling-network-4326.geojson"
)
CACHE_FILENAME = "cycling.geojson"

_GEOD = Geod(ellps="WGS84")
_log = logging.getLogger(__name__)


def _download_with_retries(url: str, dest: Path) -> None:
    backoffs = (0.5, 1.0, 2.0)
    for attempt in range(len(backoffs) + 1):
        try:
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with dest.open("wb") as fp:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
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


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading cycling network GeoJSON → %s", cached)
    _download_with_retries(RESOURCE_URL, cached)
    return cached


def _line_length_m(geom: BaseGeometry) -> float:
    """Geodesic length in metres for the line components of `geom`. Non-line components
    (Points, etc.) contribute 0 — useful when an intersection touches a polygon boundary
    at a single point.
    """
    if geom.is_empty:
        return 0.0
    gt = geom.geom_type
    if gt in ("LineString", "MultiLineString"):
        return _GEOD.geometry_length(geom)
    if gt == "GeometryCollection":
        return sum(_line_length_m(g) for g in geom.geoms)
    return 0.0


def load_cycling_index(cache_dir: Path) -> tuple[STRtree, list[BaseGeometry]]:
    """Load cycling-network line geometries → (STRtree, lines).

    Used by `tools/build_parcels.py` for per-parcel `distBikeLaneM`. The
    same line set powers the v1.1 neighborhood-level `bike` score; loading
    them once at the orchestrator level avoids duplicate I/O.
    """
    cached = _ensure_cached(Path(cache_dir))
    with cached.open(encoding="utf-8") as fp:
        data = json.load(fp)
    line_geoms: list[BaseGeometry] = []
    for feat in data.get("features") or []:
        if not feat.get("geometry"):
            continue
        line_geoms.append(shape(feat["geometry"]))
    _log.info("cycling: %d line geometries indexed for per-parcel distance", len(line_geoms))
    return STRtree(line_geoms), line_geoms


def nearest_bike_lane_distance_m(parcel_geom: BaseGeometry,
                                 tree: STRtree,
                                 lines: list[BaseGeometry],
                                 max_search_deg: float = 0.02) -> float:
    """Return the geodesic distance (m) from `parcel_geom`'s representative
    point to the nearest cycling-network line. Returns a large sentinel
    (99999) when nothing is within `max_search_deg` (~2 km at Toronto's
    latitude).

    Uses STRtree.query against an expanded bounding box so the candidate
    set is small, then computes Euclidean degree-distance from the
    representative point and converts to meters via the rough conversion
    1 deg ≈ 111,320 m at the equator (good enough for nearest-stop ranking;
    not for survey-grade distance).
    """
    pt = parcel_geom.representative_point()
    # Expanded bounding box for the STRtree query — slightly larger than the
    # actual search radius so we don't miss a line that's just beyond the
    # parcel's bbox.
    minx, miny, maxx, maxy = pt.x - max_search_deg, pt.y - max_search_deg, pt.x + max_search_deg, pt.y + max_search_deg
    from shapely.geometry import box as _box
    bbox = _box(minx, miny, maxx, maxy)
    candidates = list(tree.query(bbox))
    if not candidates:
        return 99999.0
    best_deg = float("inf")
    for i in candidates:
        d = lines[i].distance(pt)
        if d < best_deg:
            best_deg = d
    if best_deg == float("inf"):
        return 99999.0
    # Convert degrees → meters (Toronto ~43.7°N: latitude is ~111,320 m/deg,
    # longitude is ~80,400 m/deg). We use an average for simplicity since
    # the result is rounded to the nearest meter anyway.
    return float(best_deg * 95000)


def compute_bike(neighborhoods: list[Neighborhood], cache_dir: Path
                 ) -> tuple[dict[str, int], list[str]]:
    """Returns `(bike_by_name, fallback_names)`. Per neighborhood: km of cycling
    infrastructure inside the polygon (geodesic length of each line × polygon
    intersection), scaled to 0–100 by 95th-percentile normalization. Neighborhoods
    with zero infrastructure get `bike = 0` and appear in `fallback_names`.
    """
    cached = _ensure_cached(Path(cache_dir))
    with cached.open(encoding="utf-8") as fp:
        data = json.load(fp)

    line_geoms: list[BaseGeometry] = []
    for feat in data.get("features") or []:
        if not feat.get("geometry"):
            continue
        line_geoms.append(shape(feat["geometry"]))
    _log.info("cycling: %d line features loaded", len(line_geoms))

    tree = STRtree(line_geoms)
    km_by_idx = [0.0] * len(neighborhoods)
    for nb_idx, nb in enumerate(neighborhoods):
        for line_idx in tree.query(nb.polygon):
            line = line_geoms[line_idx]
            if not nb.polygon.intersects(line):
                continue
            length_m = _line_length_m(nb.polygon.intersection(line))
            km_by_idx[nb_idx] += length_m / 1000.0

    sorted_km = sorted(km_by_idx)
    p95_idx = int(0.95 * (len(sorted_km) - 1))
    p95 = sorted_km[p95_idx]
    if p95 == 0:
        _log.error("p95 cycling km is 0 — every neighborhood falls back to bike=0")
        scale = 1.0
    else:
        scale = p95

    bike_by_name: dict[str, int] = {}
    fallback_names: list[str] = []
    for nb, km in zip(neighborhoods, km_by_idx):
        if km <= 0:
            bike_by_name[nb.name] = 0
            fallback_names.append(nb.name)
            continue
        score = round(100 * km / scale)
        bike_by_name[nb.name] = max(0, min(100, score))

    _log.info("cycling: p95 km=%.2f; %d fallback names", p95, len(fallback_names))
    return bike_by_name, fallback_names


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    from .neighborhoods import fetch_neighborhoods
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tools/cache")
    nbs = fetch_neighborhoods(cache)
    bk, fb = compute_bike(nbs, cache)
    top = sorted(bk.items(), key=lambda kv: -kv[1])[:5]
    bottom = sorted(bk.items(), key=lambda kv: kv[1])[:5]
    print(f"bike: {len(bk)} entries")
    print(f"  top 5: {top}")
    print(f"  bottom 5: {bottom}")
    print(f"fallbacks: {len(fb)} {fb[:5]}{'...' if len(fb) > 5 else ''}")
