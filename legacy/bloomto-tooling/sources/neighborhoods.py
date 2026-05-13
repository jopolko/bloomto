"""Fetch Toronto's 158 official neighborhood polygons from CKAN.

Output is a list of `Neighborhood` dataclasses with:
  - name: the verbatim AREA_NAME (e.g., "Bay Street Corridor")
  - polygon: shapely WGS84 geometry
  - centroid_lat/centroid_lng: WGS84 centroid
  - area_km2: geodesic area via pyproj.Geod
"""

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import requests
from pyproj import Geod
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

PACKAGE_ID = "neighbourhoods"
RESOURCE_ID = "0719053b-28b7-48ea-b863-068823a93aaa"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "fc443770-ef0a-4025-9c2c-2cb558bfab00/resource/"
    f"{RESOURCE_ID}/download/neighbourhoods-4326.geojson"
)
CACHE_FILENAME = "neighbourhoods.geojson"
EXPECTED_COUNT = 158
NAME_FIELD = "AREA_NAME"

_GEOD = Geod(ellps="WGS84")
_log = logging.getLogger(__name__)


@dataclass
class Neighborhood:
    name: str
    polygon: BaseGeometry
    centroid_lat: float
    centroid_lng: float
    area_km2: float


def _download_with_retries(url: str, dest: Path) -> None:
    backoffs = (0.5, 1.0, 2.0)
    for attempt in range(len(backoffs) + 1):
        try:
            with requests.get(url, stream=True, timeout=60) as r:
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
            import time
            time.sleep(wait)


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading neighborhoods GeoJSON → %s", cached)
    _download_with_retries(RESOURCE_URL, cached)
    return cached


def fetch_neighborhoods(cache_dir: Path,
                        *, expected_count: int = EXPECTED_COUNT) -> list[Neighborhood]:
    cached = _ensure_cached(Path(cache_dir))
    with cached.open(encoding="utf-8") as fp:
        data = json.load(fp)

    features = data.get("features") or []
    if len(features) != expected_count:
        raise RuntimeError(
            f"neighbourhoods.geojson has {len(features)} features, expected {expected_count}"
        )

    results: list[Neighborhood] = []
    for i, feat in enumerate(features):
        props = feat.get("properties") or {}
        name = props.get(NAME_FIELD)
        if not name:
            raise RuntimeError(f"feature {i} missing {NAME_FIELD}")
        poly = shape(feat["geometry"])
        centroid = poly.centroid
        area_m2, _perim = _GEOD.geometry_area_perimeter(poly)
        results.append(
            Neighborhood(
                name=name,
                polygon=poly,
                centroid_lat=round(centroid.y, 6),
                centroid_lng=round(centroid.x, 6),
                area_km2=abs(area_m2) / 1e6,
            )
        )

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tools/cache")
    ns = fetch_neighborhoods(cache)
    print(f"{len(ns)} neighborhoods")
    for n in ns[:3]:
        print(f"  {n.name!r:40s} area={n.area_km2:6.2f} km² centroid=({n.centroid_lat:.4f},{n.centroid_lng:.4f})")
