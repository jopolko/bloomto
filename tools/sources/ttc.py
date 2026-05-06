"""Compute `transit` (TTC stop density, normalized 0–100) per neighborhood from the
TTC GTFS-static `stops.txt`. See `tools/README.md` § Transit Source for the locked
schema and 200m buffer rationale.
"""

import csv
import io
import logging
import sys
import time
import zipfile
from pathlib import Path

import requests
from shapely.geometry import Point
from shapely.strtree import STRtree

from .neighborhoods import Neighborhood

PACKAGE_ID = "ttc-routes-and-schedules"
RESOURCE_ID = "cfb6b2b8-6191-41e3-bda1-b175c51148cb"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "7795b45e-e65a-4465-81fc-c36b9dfff169/resource/"
    f"{RESOURCE_ID}/download/opendata_ttc_schedules.zip"
)
CACHE_FILENAME = "ttc_gtfs.zip"
STOPS_MEMBER = "stops.txt"

# 200m at 43.7°N: 1° lat ≈ 111 km → 200 m ≈ 0.0018°. Applied uniformly to lat/lon in
# WGS84 — east-west stretch is acceptable for ranking since the same buffer is applied
# to every polygon. See README risk #1 (905-area stops fall outside all buffers).
BUFFER_DEG = 0.0018

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
    _log.info("downloading TTC GTFS ZIP → %s", cached)
    _download_with_retries(RESOURCE_URL, cached)
    return cached


def _load_stops_by_route_types(cache_dir: Path, route_types: set[int]) -> list[Point]:
    """Return shapely Points for every TTC stop served by at least one route
    whose `route_type` is in `route_types`.

    Joins routes → trips → stop_times → stops. The stop_times.txt file is the
    large one (~1 M+ rows); both passes are streamed so memory stays bounded
    by the size of the matched-route_ids / trip_ids / stop_ids sets — at most
    ~10k stops for the full TTC even if all route_types are selected.

    Stops are deduplicated by `stop_id` (a stop served by N trips appears
    once in stops.txt → once in the output).
    """
    cached = _ensure_cached(Path(cache_dir))

    with zipfile.ZipFile(cached) as zf:
        # 1) routes.txt → route_ids whose route_type matches.
        route_ids: set[str] = set()
        with zf.open("routes.txt") as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            for row in reader:
                try:
                    rt = int(row["route_type"])
                except (KeyError, ValueError, TypeError):
                    continue
                if rt in route_types:
                    rid = row.get("route_id")
                    if rid:
                        route_ids.add(rid)

        # 2) trips.txt → trip_ids whose route_id is in route_ids.
        trip_ids: set[str] = set()
        with zf.open("trips.txt") as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            for row in reader:
                if row.get("route_id") in route_ids:
                    tid = row.get("trip_id")
                    if tid:
                        trip_ids.add(tid)

        # 3) stop_times.txt → stop_ids served by any matching trip.
        stop_ids: set[str] = set()
        with zf.open("stop_times.txt") as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            for row in reader:
                if row.get("trip_id") in trip_ids:
                    sid = row.get("stop_id")
                    if sid:
                        stop_ids.add(sid)

        # 4) stops.txt → Points for matched stop_ids (dedup'd by stop_id).
        points: list[Point] = []
        with zf.open("stops.txt") as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            for row in reader:
                if row.get("stop_id") not in stop_ids:
                    continue
                try:
                    lat = float(row["stop_lat"])
                    lon = float(row["stop_lon"])
                except (KeyError, ValueError, TypeError):
                    continue
                points.append(Point(lon, lat))

    _log.info("ttc: route_types=%s → %d routes → %d trips → %d stops",
              sorted(route_types), len(route_ids), len(trip_ids), len(points))
    return points


def compute_major_transit_stops(cache_dir: Path) -> list[Point]:
    """Subway (route_type=1) + streetcar (route_type=0) stops as Points.

    Used for the parcel-level `distSubwayStreetcarM` (Req 5.2) and the score's
    transit_factor (Req 4.1). Excludes buses (route_type=3) — Toronto's parcel
    parking-waiver rules hinge on major-transit proximity, not bus proximity.
    """
    return _load_stops_by_route_types(Path(cache_dir), {0, 1})


def compute_subway_stops(cache_dir: Path) -> list[Point]:
    """Subway-only (route_type=1) stops as Points.

    Used for the parcel-level `distSubwayM` and the bloom flag's tighter
    800 m subway threshold (design.md §137).
    """
    return _load_stops_by_route_types(Path(cache_dir), {1})


def compute_streetcar_stops(cache_dir: Path) -> list[Point]:
    """Streetcar-only (GTFS route_type=0) stops as Points.

    Used for the parcel-level `distStreetcarM` (added 2026-05-03 for the
    per-mode transit split — previously the streetcar distance was only
    derivable from the subway+streetcar union by inference).
    """
    return _load_stops_by_route_types(Path(cache_dir), {0})


def compute_bus_stops(cache_dir: Path) -> list[Point]:
    """Bus-only (GTFS route_type=3) stops as Points.

    Used for the parcel-level `distBusM` (added 2026-05-03). Buses do NOT
    qualify for Toronto's major-transit parking-waiver, so this is purely
    informational — a "is there bus service near this parcel" signal for
    suburban-multiplex screening (which often relies on TTC bus routes
    rather than subway/streetcar).
    """
    return _load_stops_by_route_types(Path(cache_dir), {3})


def compute_transit(neighborhoods: list[Neighborhood], cache_dir: Path
                    ) -> tuple[dict[str, int], list[str]]:
    """Returns `(transit_by_name, fallback_names)` keyed on AREA_NAME. Per neighborhood:
    count of TTC stops inside the 200m-buffered polygon, scaled to 0–100 by 95th-
    percentile normalization. Neighborhoods with zero stops get `transit = 0` and
    appear in `fallback_names`.

    Boundary stops legitimately count for both adjacent neighborhoods — the metric is
    "how many TTC stops can someone living here walk to," not a partition of stops.
    """
    cached = _ensure_cached(Path(cache_dir))

    buffered = [n.polygon.buffer(BUFFER_DEG) for n in neighborhoods]
    tree = STRtree(buffered)
    counts = [0] * len(neighborhoods)

    total_stops = 0
    out_of_bounds = 0
    with zipfile.ZipFile(cached) as zf:
        with zf.open(STOPS_MEMBER) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            for row in reader:
                total_stops += 1
                try:
                    lat = float(row["stop_lat"])
                    lng = float(row["stop_lon"])
                except (KeyError, ValueError):
                    continue
                pt = Point(lng, lat)
                hit_any = False
                for idx in tree.query(pt):
                    if buffered[idx].contains(pt):
                        counts[idx] += 1
                        hit_any = True
                if not hit_any:
                    out_of_bounds += 1

    _log.info("ttc: %d total stops; %d outside any buffered polygon (905-area / fringe)",
              total_stops, out_of_bounds)

    sorted_counts = sorted(counts)
    p95_idx = int(0.95 * (len(sorted_counts) - 1))
    p95 = sorted_counts[p95_idx]
    if p95 == 0:
        _log.error("p95 stop count is 0 — every neighborhood falls back to transit=0")
        scale = 1
    else:
        scale = p95

    transit_by_name: dict[str, int] = {}
    fallback_names: list[str] = []
    for nb, c in zip(neighborhoods, counts):
        if c == 0:
            transit_by_name[nb.name] = 0
            fallback_names.append(nb.name)
            continue
        score = round(100 * c / scale)
        transit_by_name[nb.name] = max(0, min(100, score))

    _log.info("ttc: p95 stop count=%d; %d fallback names", p95, len(fallback_names))
    return transit_by_name, fallback_names


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    from .neighborhoods import fetch_neighborhoods
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tools/cache")
    nbs = fetch_neighborhoods(cache)
    tr, fb = compute_transit(nbs, cache)
    top = sorted(tr.items(), key=lambda kv: -kv[1])[:5]
    bottom = sorted(tr.items(), key=lambda kv: kv[1])[:5]
    print(f"transit: {len(tr)} entries")
    print(f"  top 5: {top}")
    print(f"  bottom 5: {bottom}")
    print(f"fallbacks: {len(fb)} {fb[:5]}{'...' if len(fb) > 5 else ''}")
