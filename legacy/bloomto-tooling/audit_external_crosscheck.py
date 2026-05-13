"""Cross-check wire claims against canonical source datasets.

Recomputes per-parcel claims from the original ETL inputs (already cached in
tools/cache/) and diffs against the wire. No network calls.

Checks:
  - zoneClass        via zoning_area.geojson point-in-polygon
  - sixplexEligible  via T&EY District polygon + Ward 23 bbox
  - inRegulatedArea  via TRCA Reg 41/24 polygons
  - inFloodingStudyArea via basement-flooding-study-areas polygons
  - distSubwayM      via TTC GTFS subway stops + haversine
  - distStreetcarM   via TTC GTFS streetcar stops + haversine
  - distSubwayStreetcarM  derived = min(subway, streetcar)

Sample size: stratified across both files (default 200 parcels). Big enough
to catch systematic drift, small enough to run in a few seconds.

Usage:
    python3 tools/audit_external_crosscheck.py
    python3 tools/audit_external_crosscheck.py --n 500 --seed 42
"""

import argparse
import json
import logging
import math
import random
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shapely.geometry import Point  # noqa: E402

from tools.sources import sixplex_district, flood, trca_floodplain, ttc, zoning  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOP_PATH = ROOT / "data" / "parcels-top.json"
BROADER_PATH = ROOT / "data" / "parcels-broader.json"
CACHE_DIR = ROOT / "tools" / "cache"
OUT_PATH = ROOT / "audit" / "external_crosscheck.md"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ---------- helpers ---------------------------------------------------------

EARTH_R_M = 6_371_008.8  # mean radius


def haversine_m(lat1, lng1, lat2, lng2):
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def nearest_stop_distance_m(lat, lng, stops):
    best = float("inf")
    for stop in stops:
        d = haversine_m(lat, lng, stop.y, stop.x)
        if d < best:
            best = d
    return best


@dataclass
class CheckOutcome:
    label: str
    n_checked: int
    n_agree: int
    n_disagree: int
    n_skipped: int
    examples: list  # (parcelId, wire_value, derived_value)

    @property
    def disagree_pct(self):
        if self.n_checked == 0:
            return 0.0
        return self.n_disagree / self.n_checked


# ---------- sampling -------------------------------------------------------

def stratified_sample(top_rows, broader_rows, n, seed):
    """Mix elite + broader-only with a 50/50 split."""
    rng = random.Random(seed)
    top_ids = {r["parcelId"] for r in top_rows}
    broader_only = [r for r in broader_rows if r["parcelId"] not in top_ids]
    n_top = n // 2
    n_broader = n - n_top
    top_pick = rng.sample(top_rows, min(n_top, len(top_rows)))
    broader_pick = rng.sample(broader_only, min(n_broader, len(broader_only)))
    out = []
    for r in top_pick:
        out.append({**r, "_band": "top"})
    for r in broader_pick:
        out.append({**r, "_band": "broader-only"})
    return out


# ---------- checks ---------------------------------------------------------

def check_zone_class(sample, tree, zone_classes):
    examples = []
    n_agree = n_disagree = 0
    for r in sample:
        pt = Point(r["lng"], r["lat"])
        wire = r.get("zoneClass")
        derived = None
        for i in tree.query(pt):
            if zone_classes[i] and tree.geometries[i].contains(pt):
                derived = zone_classes[i]
                break
        if derived is None:
            # No polygon — counts as a disagreement (wire claims SOMETHING,
            # source has no polygon at this lat/lng → suspicious).
            if wire is not None:
                n_disagree += 1
                examples.append((r["parcelId"], wire, "(no polygon)"))
            continue
        if derived == wire:
            n_agree += 1
        else:
            n_disagree += 1
            examples.append((r["parcelId"], wire, derived))
    return CheckOutcome("zoneClass", n_agree + n_disagree, n_agree, n_disagree, 0, examples)


def check_sixplex(sample, sixplex_idx):
    examples = []
    n_agree = n_disagree = 0
    for r in sample:
        wire = r.get("sixplexEligible")
        if wire is None:
            continue
        pt = Point(r["lng"], r["lat"])
        derived = sixplex_district.is_sixplex_eligible(pt, sixplex_idx)
        if derived == wire:
            n_agree += 1
        else:
            n_disagree += 1
            examples.append((r["parcelId"], wire, derived))
    return CheckOutcome("sixplexEligible", n_agree + n_disagree, n_agree, n_disagree, 0, examples)


def check_trca(sample, trca_idx):
    examples = []
    n_agree = n_disagree = 0
    for r in sample:
        wire = r.get("inRegulatedArea")
        if wire is None:
            continue
        pt = Point(r["lng"], r["lat"])
        derived = trca_floodplain.is_in_regulated_area(pt, trca_idx)
        if derived == wire:
            n_agree += 1
        else:
            n_disagree += 1
            examples.append((r["parcelId"], wire, derived))
    return CheckOutcome("inRegulatedArea", n_agree + n_disagree, n_agree, n_disagree, 0, examples)


def check_flood(sample, flood_idx):
    examples = []
    n_agree = n_disagree = 0
    for r in sample:
        wire = r.get("inFloodingStudyArea")
        if wire is None:
            continue
        pt = Point(r["lng"], r["lat"])
        derived = flood.is_in_flooding_area(pt, flood_idx)
        if derived == wire:
            n_agree += 1
        else:
            n_disagree += 1
            examples.append((r["parcelId"], wire, derived))
    return CheckOutcome("inFloodingStudyArea", n_agree + n_disagree, n_agree, n_disagree, 0, examples)


def check_distance(sample, stops, wire_field, tolerance_m=20):
    """Recompute haversine distance to nearest stop and diff against wire.

    Tolerance: 20m to absorb haversine-vs-geodesic differences (the ETL uses
    pyproj.Geod which is more accurate). The ETL also caps at 5000m, so any
    wire >=5000 just needs derived to be >5000 too.
    """
    examples = []
    n_agree = n_disagree = 0
    for r in sample:
        wire = r.get(wire_field)
        if wire is None:
            continue
        derived = nearest_stop_distance_m(r["lat"], r["lng"], stops)
        if wire >= 5000:
            agrees = derived >= 4900
        else:
            agrees = abs(derived - wire) <= tolerance_m
        if agrees:
            n_agree += 1
        else:
            n_disagree += 1
            examples.append((r["parcelId"], wire, round(derived)))
    return CheckOutcome(wire_field, n_agree + n_disagree, n_agree, n_disagree, 0, examples)


def check_dist_combined(sample):
    """Pure-internal check: distSubwayStreetcarM should be min of the two."""
    examples = []
    n_agree = n_disagree = 0
    for r in sample:
        ds = r.get("distSubwayM")
        dt = r.get("distStreetcarM")
        dc = r.get("distSubwayStreetcarM")
        if None in (ds, dt, dc):
            continue
        expected = min(ds, dt)
        if abs(expected - dc) <= 0.5:
            n_agree += 1
        else:
            n_disagree += 1
            examples.append((r["parcelId"], dc, expected))
    return CheckOutcome("distSubwayStreetcarM == min(subway,streetcar)",
                        n_agree + n_disagree, n_agree, n_disagree, 0, examples)


# ---------- render ---------------------------------------------------------

def fmt_pct(p):
    return f"{p * 100:.1f}%"


def render_md(outcomes, sample, top_rows, broader_rows, seed):
    lines = []
    lines.append("# BloomTO &mdash; external cross-check audit\n")
    lines.append(
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} &middot; "
        f"sample size {len(sample)} (seed {seed}) from {len(top_rows):,} elite + "
        f"{len(broader_rows) - len(top_rows):,} broader-only rows_\n"
    )
    lines.append("## Summary\n")
    lines.append("| Check | Checked | Agree | Disagree | % disagree |")
    lines.append("|---|---:|---:|---:|---:|")
    for o in outcomes:
        marker = "❌ " if o.disagree_pct > 0.05 else ("⚠️ " if o.disagree_pct > 0 else "✅ ")
        lines.append(
            f"| {marker}{o.label} | {o.n_checked:,} | {o.n_agree:,} | "
            f"{o.n_disagree:,} | {fmt_pct(o.disagree_pct)} |"
        )
    lines.append("")
    for o in outcomes:
        if o.n_disagree == 0:
            continue
        lines.append(f"\n## {o.label} &mdash; {o.n_disagree:,} disagreements out of {o.n_checked:,}\n")
        lines.append("| parcelId | wire | derived from source |")
        lines.append("|---|---|---|")
        for pid, wire, derived in o.examples[:15]:
            lines.append(f"| {pid} | `{wire}` | `{derived}` |")
        if len(o.examples) > 15:
            lines.append(f"\n_…and {len(o.examples) - 15:,} more not shown_")
        lines.append("")
    if all(o.n_disagree == 0 for o in outcomes):
        lines.append("\nAll cross-checks pass on the sample. Either the wire is faithful to "
                     "its source datasets, or the bugs are too rare for a sample to catch &mdash; "
                     "raise `--n` to push harder.\n")
    return "\n".join(lines)


# ---------- main -----------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1729)
    ap.add_argument("--top", type=Path, default=TOP_PATH)
    ap.add_argument("--broader", type=Path, default=BROADER_PATH)
    ap.add_argument("--cache", type=Path, default=CACHE_DIR)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args(argv)

    log.info("loading wire files…")
    top_rows = json.loads(args.top.read_text())["rows"]
    broader_rows = json.loads(args.broader.read_text())["rows"]

    log.info("loading source datasets from %s…", args.cache.relative_to(ROOT))
    log.info("  zoning index (50 MB)…")
    zone_tree, zone_classes = zoning.load_zone_index(args.cache)
    log.info("  sixplex (T&EY+Ward23) index…")
    sixplex_idx = sixplex_district.compute_sixplex_index(args.cache)
    log.info("  TRCA Reg 41/24 index (89 MB)…")
    trca_idx = trca_floodplain.compute_trca_index(args.cache)
    log.info("  basement-flooding-study-areas index…")
    flood_idx = flood.compute_flood_index(args.cache)
    log.info("  TTC subway + streetcar stops…")
    subway_stops = ttc.compute_subway_stops(args.cache)
    streetcar_stops = ttc.compute_streetcar_stops(args.cache)
    log.info("    %d subway stops, %d streetcar stops", len(subway_stops), len(streetcar_stops))

    sample = stratified_sample(top_rows, broader_rows, args.n, args.seed)
    log.info("stratified sample: %d parcels (%d top, %d broader-only)",
             len(sample),
             sum(1 for r in sample if r["_band"] == "top"),
             sum(1 for r in sample if r["_band"] == "broader-only"))

    outcomes = []
    log.info("running checks…")
    log.info("  zoneClass…")
    outcomes.append(check_zone_class(sample, zone_tree, zone_classes))
    log.info("  sixplexEligible…")
    outcomes.append(check_sixplex(sample, sixplex_idx))
    log.info("  inRegulatedArea (TRCA)…")
    outcomes.append(check_trca(sample, trca_idx))
    log.info("  inFloodingStudyArea…")
    outcomes.append(check_flood(sample, flood_idx))
    log.info("  distSubwayM (haversine vs wire)…")
    outcomes.append(check_distance(sample, subway_stops, "distSubwayM"))
    log.info("  distStreetcarM (haversine vs wire)…")
    outcomes.append(check_distance(sample, streetcar_stops, "distStreetcarM"))
    log.info("  distSubwayStreetcarM == min(subway,streetcar)…")
    outcomes.append(check_dist_combined(sample))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_md(outcomes, sample, top_rows, broader_rows, args.seed))
    log.info("\nresults:")
    for o in outcomes:
        marker = "FAIL" if o.disagree_pct > 0.05 else ("WARN" if o.n_disagree > 0 else "OK  ")
        log.info("  %s  %-50s  %d/%d disagree (%s)",
                 marker, o.label, o.n_disagree, o.n_checked, fmt_pct(o.disagree_pct))
    log.info("\nwrote %s", args.out.relative_to(ROOT))


if __name__ == "__main__":
    main()
