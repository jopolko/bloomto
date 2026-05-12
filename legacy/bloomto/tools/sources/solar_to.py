"""Compute `heat_pump` (share of rooftops with above-75th-percentile annual kWh,
expressed 0–100) per neighborhood from the SolarTO city-wide rooftop dataset.

Also exposes the v1.2 building blocks `compute_solar_points` (STRtree + parallel
kWh array, used by parcel scoring) and `kwh_p95` (citywide 95th-percentile kWh,
used to normalize per-parcel solar to 0–100). Note: `kwh_p95` is **not** the
statistic used by `compute_heat_pump` — that uses an internal P75. The two
percentiles serve different scoring layers (P75 for the neighborhood-level
heat-pump share, P95 for the parcel-level raw solar score).

SolarTO upstream rooftop screening (City of Toronto, applied SOURCE-side
before kWh values are published — BloomTO inherits these gates transitively):
  - Roof surface must receive >=800 kWh/m^2/yr incident solar radiation
  - Roof surface must have >=30 m^2 of clear, usable space
  - Slope must be < 45 degrees
  - Aspect must NOT be north-facing
  - Toronto yield factor: 1 kW installed PV -> ~1,150 kWh/yr generated

So every kWh value in the SolarTO CSV represents a roof surface that already
clears all four screening gates. BloomTO does NOT re-apply these filters —
they're respected transitively because the source already filtered. The
methodology is surfaced on the wire as `meta.solarMethodology` for honest
disclosure, and in `goldmines.html` info card so users see what's behind
their `solarScore`.

See `tools/README.md` § SolarTO Source for the locked CSV schema.
"""

import csv
import json
import logging
import sys
import time
from pathlib import Path

import requests
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from .neighborhoods import Neighborhood

PACKAGE_ID = "solarto"
RESOURCE_ID = "f5f37d23-85c9-4af8-b8a5-369523778f93"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "304aaee3-11cb-41c4-96a3-adef31f63ed5/resource/"
    f"{RESOURCE_ID}/download/solarto-map-4326.csv"
)
CACHE_FILENAME = "solar_to.csv"

KWH_FIELD = "annual_electricity_generation_k"
GEOM_FIELD = "geometry"

# Required by csv.field_size_limit when geometry strings exceed the stdlib default
# (some MultiPolygon GeoJSON blobs in this CSV exceed 131 KB per cell).
csv.field_size_limit(sys.maxsize)

_log = logging.getLogger(__name__)


def _download_with_retries(url: str, dest: Path) -> None:
    backoffs = (0.5, 1.0, 2.0)
    for attempt in range(len(backoffs) + 1):
        try:
            with requests.get(url, stream=True, timeout=600) as r:
                r.raise_for_status()
                with dest.open("wb") as fp:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
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
    _log.info("downloading SolarTO CSV (~293 MB) → %s", cached)
    _download_with_retries(RESOURCE_URL, cached)
    return cached


def _iter_solar_rooftops(cache_dir: Path):
    """Stream the SolarTO CSV yielding `(kwh, rep_point)` per valid row.

    Skips rows with missing/invalid kWh or unparseable geometry silently. Logs
    the seen/valid/skipped counts after the underlying iterator is exhausted.
    Used by both `compute_heat_pump` (which would otherwise pay the STRtree-build
    cost it doesn't need) and `compute_solar_points`.
    """
    cached = _ensure_cached(Path(cache_dir))

    seen = 0
    valid = 0
    skipped_no_kwh = 0
    skipped_no_geom = 0
    skipped_bad_geom = 0
    _log.info("streaming SolarTO CSV")
    try:
        with cached.open(encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                seen += 1
                kwh_raw = (row.get(KWH_FIELD) or "").strip()
                if not kwh_raw:
                    skipped_no_kwh += 1
                    continue
                try:
                    kwh = float(kwh_raw)
                except ValueError:
                    skipped_no_kwh += 1
                    continue
                geom_raw = row.get(GEOM_FIELD) or ""
                if not geom_raw:
                    skipped_no_geom += 1
                    continue
                try:
                    geom = shape(json.loads(geom_raw))
                    rep = geom.representative_point()
                except Exception:
                    skipped_bad_geom += 1
                    continue
                valid += 1
                yield kwh, rep
    finally:
        _log.info("solar_to: %d rows seen, %d valid rooftops "
                  "(skipped: no_kwh=%d, no_geom=%d, bad_geom=%d)",
                  seen, valid, skipped_no_kwh, skipped_no_geom, skipped_bad_geom)


def compute_solar_points(cache_dir: Path) -> tuple[STRtree, list[float]]:
    """Stream the SolarTO CSV and return (STRtree of rooftop points, parallel kWh array).

    Each point is the rooftop polygon's `representative_point()` in WGS84. Indices
    into `tree.geometries` align 1:1 with the kWh list. Rows with missing/invalid
    `annual_electricity_generation_k` or unparseable geometry are skipped silently.
    """
    points: list[Point] = []
    kwh_values: list[float] = []
    for kwh, rep in _iter_solar_rooftops(cache_dir):
        points.append(rep)
        kwh_values.append(kwh)
    return STRtree(points), kwh_values


def kwh_p95(values: list[float]) -> float:
    """Citywide 95th-percentile rooftop kWh.

    Used by parcel scoring to normalize per-parcel solar (`max_kwh_in_parcel / p95`)
    to a 0–100 raw score before shadow attenuation. Returns `0.0` for an empty input.
    """
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(0.95 * (len(s) - 1))
    return s[idx]


def compute_solar_per_parcel(parcels, cache_dir: Path) -> dict[str, int]:
    """Per-parcel raw solar score, 0–100.

    For each parcel, finds the SolarTO rooftop point with the highest annual
    kWh **inside the parcel polygon** (representative-point containment, same
    semantics as the v1.1 neighborhood aggregation), divides by the citywide
    P95 kWh, clamps to [0, 100]. Empty parcels (no contained rooftops) get 0.

    The "raw" name distinguishes this from the shadow-attenuated `solarScore`
    on the wire format — `parcel_scoring.py` (or the orchestrator) multiplies
    this by the unshadowed fraction from `shadow_analysis.py` to get the
    final number, and may set it to None per the three-tier rule.

    Reuses `compute_solar_points` + `kwh_p95` (Task 7) — does NOT re-stream
    the CSV. The STRtree built there is exactly what we need here.
    """
    tree, kwh_values = compute_solar_points(Path(cache_dir))

    if not kwh_values:
        _log.warning("solar_to: no rooftops loaded — every parcel will get raw_score = 0")
        return {p.parcel_id: 0 for p in parcels}

    p95 = kwh_p95(kwh_values)
    if p95 <= 0:
        _log.error("solar_to: P95 is %s — every parcel will get raw_score = 0", p95)
        return {p.parcel_id: 0 for p in parcels}

    out: dict[str, int] = {}
    for parcel in parcels:
        max_kwh = 0.0
        for idx in tree.query(parcel.geometry):
            pt = tree.geometries[idx]
            if not parcel.geometry.contains(pt):
                continue
            kwh = kwh_values[idx]
            if kwh > max_kwh:
                max_kwh = kwh
        score = round(100 * max_kwh / p95)
        out[parcel.parcel_id] = max(0, min(100, score))
    return out


def compute_heat_pump(neighborhoods: list[Neighborhood], cache_dir: Path
                      ) -> tuple[dict[str, int], list[str]]:
    """Returns `(heat_pump_by_name, fallback_names)`. Per neighborhood: percentage of
    its rooftops whose `annual_electricity_generation_k` falls at or above the citywide
    75th percentile, rounded to 0–100. Neighborhoods with zero rooftops fall back to
    `heat_pump = 50` (neutral) and appear in `fallback_names`.
    """
    # Pass 1: collect (kwh, point) tuples — same memory shape as v1.1
    # (no STRtree build; we don't need spatial-query access during the
    # neighborhood attribution pass).
    rooftops: list[tuple[float, Point]] = list(_iter_solar_rooftops(Path(cache_dir)))

    if not rooftops:
        _log.error("no valid SolarTO rooftops; every neighborhood falls back to 50")
        return {n.name: 50 for n in neighborhoods}, [n.name for n in neighborhoods]

    sorted_kwh = sorted(r[0] for r in rooftops)
    p75_idx = int(0.75 * (len(sorted_kwh) - 1))
    p75 = sorted_kwh[p75_idx]
    _log.info("solar_to: p75 kWh threshold = %.0f", p75)

    polygons = [n.polygon for n in neighborhoods]
    nb_tree = STRtree(polygons)
    counts_total = [0] * len(neighborhoods)
    counts_above = [0] * len(neighborhoods)

    for kwh, pt in rooftops:
        for idx in nb_tree.query(pt):
            if polygons[idx].contains(pt):
                counts_total[idx] += 1
                if kwh >= p75:
                    counts_above[idx] += 1
                break

    heat_pump_by_name: dict[str, int] = {}
    fallback_names: list[str] = []
    for nb, total, above in zip(neighborhoods, counts_total, counts_above):
        if total == 0:
            heat_pump_by_name[nb.name] = 50
            fallback_names.append(nb.name)
            continue
        score = round(100 * above / total)
        heat_pump_by_name[nb.name] = max(0, min(100, score))

    _log.info("solar_to: %d fallback names (zero-rooftop neighborhoods)",
              len(fallback_names))
    return heat_pump_by_name, fallback_names


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    from .neighborhoods import fetch_neighborhoods
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tools/cache")
    nbs = fetch_neighborhoods(cache)
    hp, fb = compute_heat_pump(nbs, cache)
    top = sorted(hp.items(), key=lambda kv: -kv[1])[:5]
    bottom = sorted(hp.items(), key=lambda kv: kv[1])[:5]
    print(f"heat_pump: {len(hp)} entries")
    print(f"  top 5: {top}")
    print(f"  bottom 5: {bottom}")
    print(f"fallbacks: {len(fb)} {fb[:5]}{'...' if len(fb) > 5 else ''}")
