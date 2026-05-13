"""Offline ETL orchestrator: fetch all Toronto Open Data sources, score every
neighborhood, and write `data/neighborhoods.json` atomically.

This is the v1.1 "all-real" pipeline — every numeric field is grounded in a
CKAN-published dataset. The script is read-only against the host (only writes
to `--cache-dir` and `--out`); intended to run on a developer workstation,
not on the VPS.

Run:
    python3 tools/build_neighborhoods.py
    python3 tools/build_neighborhoods.py --out data/neighborhoods.json
    python3 tools/build_neighborhoods.py --weights '{"energy":1.0,"canopy":0,"walk":0,"transit":0,"bike":0,"mm":0}'
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    # Direct invocation: `python3 tools/build_neighborhoods.py`. Put the project
    # root on sys.path so `from tools.* import ...` resolves the same as `-m`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import io as bloomto_io
from tools import scoring
from tools.sources import (
    canopy as canopy_src,
    census as census_src,
    cycling as cycling_src,
    neighborhoods as neighborhoods_src,
    solar_to as solar_src,
    streets as streets_src,
    ttc as ttc_src,
    zoning as zoning_src,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "data" / "neighborhoods.json"
DEFAULT_CACHE = PROJECT_ROOT / "tools" / "cache"

SOURCE_VERSIONS = {
    "neighborhoods": neighborhoods_src.RESOURCE_URL,
    "solar_to": solar_src.RESOURCE_URL,
    "canopy": canopy_src.RESOURCE_URL,
    "census": census_src.RESOURCE_URL,
    "ttc": ttc_src.RESOURCE_URL,
    "cycling": cycling_src.RESOURCE_URL,
    "streets": streets_src.RESOURCE_URL,
    "zoning": zoning_src.ZONING_RESOURCE_URL,
    "property": zoning_src.PROPERTY_RESOURCE_URL,
}

FALLBACK_SOURCE_ORDER = (
    "solar_to", "canopy", "census", "ttc", "cycling", "streets", "zoning",
)

_log = logging.getLogger("bloomto.build")


def _stage(label: str):
    _log.info("→ %s", label)
    return time.monotonic()


def _done(label: str, started_at: float) -> None:
    _log.info("  %s done in %.1fs", label, time.monotonic() - started_at)


def _aggregate_permits_by_neighborhood(parcels_path: Path) -> dict[str, int]:
    """Sum unit-creating residential permits per neighborhood from
    `data/parcels.geojson`. Each parcel feature carries a `permits` object
    whose `recentCount` is the count of last-5-yr permits with `unitsCreated
    >= 1` (per `tools/build_parcels.py` filter). Aggregating gives a per-
    neighborhood "redevelopment activity" signal for the frontend's sort
    dimension and detail-panel readout. Returns `{}` when parcels.geojson is
    absent (first-ever build, or when `build_neighborhoods.py` is run alone).
    """
    if not parcels_path.exists():
        _log.warning("permits aggregate: %s not found — emitting zeros", parcels_path)
        return {}
    from collections import defaultdict
    counts: dict[str, int] = defaultdict(int)
    with parcels_path.open(encoding="utf-8") as fp:
        data = json.load(fp)
    for feat in data.get("features", []):
        p = feat.get("properties") or {}
        nb = p.get("neighborhood")
        if not nb:
            continue
        recent = (p.get("permits") or {}).get("recentCount", 0) or 0
        if recent > 0:
            counts[nb] += recent
    _log.info("permits aggregate: %d permits across %d neighborhoods",
              sum(counts.values()), len(counts))
    return dict(counts)


def assemble_payload(
    neighborhoods,
    heat_pump,
    canopy,
    built_year,
    existing,
    potential,
    transit,
    bike,
    walk,
    *,
    fallbacks_by_source,
    weights=None,
    income_med=None,
    income_avg=None,
    dwelling_med=None,
    dwelling_avg=None,
    permits_recent=None,
):
    """Build the `{meta, neighborhoods}` payload (no I/O).

    Exposed as a module function so the e2e test can build payloads against
    fixtures with a smaller `expected_count` than the production 158.
    """
    entries = []
    for n in neighborhoods:
        e_existing = existing.get(n.name, 0)
        e_potential = potential.get(n.name, e_existing)
        s = scoring.score(
            heat_pump=heat_pump.get(n.name, 0),
            canopy_pct=canopy.get(n.name, 0),
            walk=walk.get(n.name, 0),
            transit=transit.get(n.name, 0),
            bike=bike.get(n.name, 0),
            existing=e_existing,
            potential=e_potential,
            weights=weights,
        )
        entry = {
            "name": n.name,
            "lat": n.centroid_lat,
            "lng": n.centroid_lng,
            "score": s,
            "heatPump": heat_pump.get(n.name, 0),
            "canopy": canopy.get(n.name, 0),
            "walk": walk.get(n.name, 0),
            "transit": transit.get(n.name, 0),
            "bike": bike.get(n.name, 0),
            "builtYear": built_year.get(n.name, 0),
            "existing": e_existing,
            "potential": e_potential,
        }
        # 2026-05-07 evening — household income, dwelling value, and unit-creating
        # permit rate per 1k dwellings. Optional — only present when the caller
        # passes the dicts. Wire fields keyed `nb*` so the frontend lookup map
        # follows the same convention as `nbHeatPump` / `nbPermitMedianCostPerUnit`.
        if income_med is not None:
            entry["medHouseholdIncome"] = income_med.get(n.name, 0)
        if income_avg is not None:
            entry["avgHouseholdIncome"] = income_avg.get(n.name, 0)
        if dwelling_med is not None:
            entry["medDwellingValue"] = dwelling_med.get(n.name, 0)
        if dwelling_avg is not None:
            entry["avgDwellingValue"] = dwelling_avg.get(n.name, 0)
        if permits_recent is not None:
            recent = permits_recent.get(n.name, 0)
            entry["permitsRecentCount"] = recent
            # Per-1000-dwellings rate. `existing` is the NPP 2021 occupied-
            # private-dwelling count (drawn into `existing` upstream).
            entry["permitsPer1kDwellings"] = (
                round(recent / e_existing * 1000, 2) if e_existing else 0.0
            )
        entries.append(entry)

    entries.sort(key=lambda e: e["score"], reverse=True)

    meta = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sourceVersions": dict(SOURCE_VERSIONS),
        "scoreFormula": scoring.FORMULA_TEXT,
        "fallbacks": {k: list(fallbacks_by_source.get(k, [])) for k in FALLBACK_SOURCE_ORDER},
    }
    return {"meta": meta, "neighborhoods": entries}


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="Build BloomTO data/neighborhoods.json from Toronto Open Data.",
    )
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help=f"output JSON path (default: {DEFAULT_OUT.relative_to(PROJECT_ROOT)})")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE,
                   help=f"download cache dir (default: {DEFAULT_CACHE.relative_to(PROJECT_ROOT)})")
    p.add_argument("--weights", type=str, default=None,
                   help='JSON object overriding score weights, e.g. \'{"energy":0.30,"canopy":0.20,"walk":0.15,"transit":0.15,"bike":0.10,"mm":0.10}\'')
    p.add_argument("--quiet", action="store_true",
                   help="suppress progress logs (errors still printed)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    weights = json.loads(args.weights) if args.weights else None
    cache = args.cache_dir
    started = time.monotonic()

    t = _stage("fetch neighborhoods")
    neighborhoods = neighborhoods_src.fetch_neighborhoods(cache)
    _done("fetch neighborhoods", t)

    fallbacks = {}

    t = _stage("compute heat_pump (SolarTO)")
    heat_pump, fallbacks["solar_to"] = solar_src.compute_heat_pump(neighborhoods, cache)
    _done("heat_pump", t)

    t = _stage("compute canopy (Forest/Land Cover)")
    canopy, fallbacks["canopy"] = canopy_src.compute_canopy(neighborhoods, cache)
    _done("canopy", t)

    t = _stage("compute census (NPP 2021)")
    built_year, existing, fallbacks["census"] = census_src.compute_census(neighborhoods, cache)
    _done("census", t)

    t = _stage("compute household income (NPP 2021)")
    income_med, income_avg, fallbacks["census_income"] = census_src.compute_household_income(neighborhoods, cache)
    _done("household income", t)

    t = _stage("compute dwelling value (NPP 2021)")
    dwelling_med, dwelling_avg, fallbacks["census_dwelling"] = census_src.compute_dwelling_value(neighborhoods, cache)
    _done("dwelling value", t)

    t = _stage("aggregate unit-creating permits per neighborhood (parcels.geojson)")
    permits_recent = _aggregate_permits_by_neighborhood(PROJECT_ROOT / "data" / "parcels.geojson")
    _done("permits aggregate", t)

    t = _stage("compute transit (TTC GTFS)")
    transit, fallbacks["ttc"] = ttc_src.compute_transit(neighborhoods, cache)
    _done("transit", t)

    t = _stage("compute bike (Cycling Network)")
    bike, fallbacks["cycling"] = cycling_src.compute_bike(neighborhoods, cache)
    _done("bike", t)

    t = _stage("compute walk (Centreline)")
    walk, fallbacks["streets"] = streets_src.compute_walk(neighborhoods, cache)
    _done("walk", t)

    t = _stage("compute potential (Zoning + Property)")
    potential, fallbacks["zoning"] = zoning_src.compute_potential(neighborhoods, existing, cache)
    _done("potential", t)

    t = _stage("assemble + score")
    payload = assemble_payload(
        neighborhoods,
        heat_pump=heat_pump,
        canopy=canopy,
        built_year=built_year,
        existing=existing,
        potential=potential,
        transit=transit,
        bike=bike,
        walk=walk,
        fallbacks_by_source=fallbacks,
        weights=weights,
        income_med=income_med,
        income_avg=income_avg,
        dwelling_med=dwelling_med,
        dwelling_avg=dwelling_avg,
        permits_recent=permits_recent,
    )
    _done("assemble", t)

    t = _stage(f"write {args.out}")
    bloomto_io.write_atomic(payload, args.out)
    _done("write", t)

    fallback_summary = " ".join(
        f"{k}={len(fallbacks.get(k, []))}" for k in FALLBACK_SOURCE_ORDER
    )
    elapsed = time.monotonic() - started
    _log.info(
        "DONE: %d neighborhoods → %s | fallbacks: %s | %.1fs",
        len(payload["neighborhoods"]), args.out, fallback_summary, elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
