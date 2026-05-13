"""Permits backtest — recall of BloomTO's gates against approved multiplex permits.

For every approved 3-6 unit residential permit since `--since` (default
2024-01-01), check whether BloomTO would have surfaced the parcel as elite
(parcels-top.json) or broader (parcels-broader.json). The fraction caught
is the recall — how predictive BloomTO is of where developers actually
build multiplexes.

Address-join: both sides are normalized via `tools.sources._address.normalize_address`.
The permits CSV has no lat/lng so spatial join isn't possible without first
geocoding — out of scope for this script.

What this DOES NOT measure (yet):
  - Whether a "missed" parcel was excluded by gate vs simply scored too low
    (would need parcels.geojson cross-reference; future enhancement).
  - Whether the *score* is well-calibrated (just whether the parcel showed up).
  - Pre-Bill-185 baseline (gates encode current bylaw; can't usefully backtest
    against permits issued before the bylaw existed).

Usage:
    python3 tools/audit_permits_backtest.py
    python3 tools/audit_permits_backtest.py --since 2024-01-01 --units-min 3 --units-max 6
"""

import argparse
import csv
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.sources import building_permits as permits_src  # noqa: E402
from tools.sources._address import normalize_address  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TOP_PATH = ROOT / "data" / "parcels-top.json"
BROADER_PATH = ROOT / "data" / "parcels-broader.json"
PERMITS_PATH = ROOT / "tools" / "cache" / "building_permits.csv"
OUT_PATH = ROOT / "audit" / "permits_backtest.md"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ---------- permit loading ------------------------------------------------

def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_int(s):
    if not s:
        return None
    try:
        return int(s.replace(",", ""))
    except (ValueError, TypeError):
        return None


def load_relevant_permits(path, since_date, units_min, units_max):
    """Stream permits CSV and yield rows matching the multiplex window.

    Filters: kept permit category, units_created in [units_min, units_max],
    issued_date >= since_date.
    """
    out = []
    drift = []  # unrecognized PERMIT_TYPE values
    seen_types = Counter()
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ptype = (row.get("PERMIT_TYPE") or "").strip()
            seen_types[ptype] += 1
            cat = permits_src.classify(ptype)
            if cat is None and ptype:
                drift.append(ptype)
            if cat not in permits_src.KEPT_CATEGORIES:
                continue
            issued = _parse_date(row.get("ISSUED_DATE"))
            if issued is None or issued < since_date:
                continue
            units = _parse_int(row.get("DWELLING_UNITS_CREATED"))
            if units is None or not (units_min <= units <= units_max):
                continue
            addr_raw = permits_src._build_address(row)
            if not addr_raw:
                continue
            out.append({
                "permit_id": row.get("PERMIT_NUM", ""),
                "address_raw": addr_raw,
                "address_norm": normalize_address(addr_raw),
                "issued_date": issued,
                "application_date": _parse_date(row.get("APPLICATION_DATE")),
                "units_created": units,
                "category": cat,
                "permit_type": ptype,
                "ward_grid": row.get("WARD_GRID", ""),
                "est_cost": _parse_int(row.get("EST_CONST_COST")),
                "current_use": row.get("CURRENT_USE", ""),
                "proposed_use": row.get("PROPOSED_USE", ""),
            })
    return out, drift


# ---------- wire indexing -------------------------------------------------

def build_address_index(rows):
    """Multi-map normalized address → list of (rank, row).

    Multi-map because two parcels can share an address (e.g. semi-detached
    pair sharing a single street number, or a strata complex). Lookup
    callers pick the best match by other criteria.
    """
    idx = defaultdict(list)
    for rank, r in enumerate(rows, 1):
        addr = r.get("address") or ""
        if not addr:
            continue
        idx[normalize_address(addr)].append((rank, r))
    return idx


# ---------- backtest ------------------------------------------------------

def backtest(permits, top_idx, broader_idx, top_count, broader_count):
    """Categorize each permit by best feed match. Returns list of result dicts."""
    results = []
    for p in permits:
        addr = p["address_norm"]
        in_top = top_idx.get(addr) or []
        in_broader = broader_idx.get(addr) or []
        if in_top:
            best = in_top[0]
            tier = "elite"
            score = best[1].get("score")
            rank = best[0]
            neighborhood = best[1].get("neighborhood")
        elif in_broader:
            best = in_broader[0]
            tier = "broader"
            score = best[1].get("score")
            rank = best[0]
            neighborhood = best[1].get("neighborhood")
        else:
            tier = "missed"
            score = None
            rank = None
            neighborhood = None
        results.append({**p, "tier": tier, "score": score, "rank": rank, "neighborhood": neighborhood})
    return results


# ---------- render --------------------------------------------------------

def render_md(results, top_count, broader_count, since_date, units_min, units_max, drift):
    n = len(results)
    by_tier = Counter(r["tier"] for r in results)
    elite = by_tier.get("elite", 0)
    broader = by_tier.get("broader", 0)
    missed = by_tier.get("missed", 0)
    recall_elite = elite / n if n else 0
    recall_either = (elite + broader) / n if n else 0

    lines = []
    lines.append("# BloomTO &mdash; permits backtest\n")
    lines.append(
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} &middot; "
        f"window: ISSUED_DATE ≥ {since_date.isoformat()}, units in [{units_min}, {units_max}]_\n"
    )
    lines.append("## Summary\n")
    lines.append(f"- **{n:,}** approved permits matched the multiplex window")
    lines.append(f"- **{elite:,}** ({recall_elite:.0%}) parcels appear in `parcels-top.json` (elite, top {top_count:,})")
    lines.append(f"- **{broader:,}** ({broader/n if n else 0:.0%}) parcels appear in `parcels-broader.json` only")
    lines.append(f"- **{missed:,}** ({missed/n if n else 0:.0%}) parcels are missing from both feeds &mdash; *recall gap*")
    lines.append("")
    lines.append(f"**Combined recall** (elite ∪ broader): **{recall_either:.0%}**")
    lines.append("")
    lines.append("Recall is the fraction of approved multiplex permits that BloomTO would have ranked. "
                 "It's not expected to be 100% &mdash; many permits land on parcels with poor "
                 "transit, heritage encumbrances, or low scores by design. But it's the most direct "
                 "test of whether the gates are *too* strict.\n")

    # Year breakdown
    years = sorted({r["issued_date"].year for r in results})
    if len(years) > 1:
        lines.append("## Recall by year\n")
        lines.append("| Year | Permits | Elite | Broader | Missed | Combined recall |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for y in years:
            ys = [r for r in results if r["issued_date"].year == y]
            yn = len(ys)
            ye = sum(1 for r in ys if r["tier"] == "elite")
            yb = sum(1 for r in ys if r["tier"] == "broader")
            ym = sum(1 for r in ys if r["tier"] == "missed")
            rec = (ye + yb) / yn if yn else 0
            lines.append(f"| {y} | {yn:,} | {ye:,} | {yb:,} | {ym:,} | {rec:.0%} |")
        lines.append("")

    # Units breakdown — does sixplex range have weaker recall than 4plex?
    lines.append("## Recall by units_created\n")
    lines.append("| Units | Permits | Elite | Broader | Missed | Combined recall |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for u in range(units_min, units_max + 1):
        us = [r for r in results if r["units_created"] == u]
        un = len(us)
        if not un:
            continue
        ue = sum(1 for r in us if r["tier"] == "elite")
        ub = sum(1 for r in us if r["tier"] == "broader")
        um = sum(1 for r in us if r["tier"] == "missed")
        rec = (ue + ub) / un if un else 0
        lines.append(f"| {u} | {un:,} | {ue:,} | {ub:,} | {um:,} | {rec:.0%} |")
    lines.append("")

    # Top-N missed permits — these are the recall failures most worth investigating.
    missed_rows = [r for r in results if r["tier"] == "missed"]
    if missed_rows:
        lines.append(f"## Sample of missed permits ({len(missed_rows):,} total)\n")
        lines.append("These are parcels where a developer was approved to build "
                     f"{units_min}-{units_max} units, but BloomTO didn&rsquo;t rank "
                     "the parcel in either feed. Each is a candidate for a gate that&rsquo;s too strict.\n")
        lines.append("| permit | address | units | issued | category | est cost |")
        lines.append("|---|---|---:|---|---|---:|")
        # Sort by recency, take top 25
        missed_rows.sort(key=lambda r: r["issued_date"], reverse=True)
        for r in missed_rows[:25]:
            cost = f"${r['est_cost']:,}" if r["est_cost"] else ""
            lines.append(
                f"| `{r['permit_id']}` | {r['address_raw']} | {r['units_created']} | "
                f"{r['issued_date']} | {r['category']} | {cost} |"
            )
        if len(missed_rows) > 25:
            lines.append(f"\n_…and {len(missed_rows) - 25:,} more not shown_")
        lines.append("")

    # Hits — useful for sanity-checking that we joined correctly
    hit_rows = [r for r in results if r["tier"] != "missed"]
    if hit_rows:
        lines.append(f"## Sample of caught permits ({len(hit_rows):,} total)\n")
        lines.append("Sanity check &mdash; these matched a parcel in the feed. Confirm the addresses look right.\n")
        lines.append("| permit | address | units | tier | score | neighborhood |")
        lines.append("|---|---|---:|---|---:|---|")
        hit_rows.sort(key=lambda r: r["score"] or 0, reverse=True)
        for r in hit_rows[:15]:
            lines.append(
                f"| `{r['permit_id']}` | {r['address_raw']} | {r['units_created']} | "
                f"{r['tier']} | {r['score']} | {r['neighborhood'] or ''} |"
            )
        lines.append("")

    if drift:
        drift_counts = Counter(drift)
        lines.append("## Classifier drift\n")
        lines.append("Unrecognized `PERMIT_TYPE` values that the classifier dropped silently. "
                     "Update `tools/sources/building_permits.py:PERMIT_CATEGORY_TABLE` if any "
                     "of these should be kept.\n")
        for ptype, c in drift_counts.most_common(10):
            lines.append(f"- `{ptype}`: {c:,} permits")
        lines.append("")

    return "\n".join(lines)


# ---------- main ----------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2024-01-01",
                    help="ISSUED_DATE >= this (YYYY-MM-DD); default 2024-01-01")
    ap.add_argument("--units-min", type=int, default=3)
    ap.add_argument("--units-max", type=int, default=6)
    ap.add_argument("--top", type=Path, default=TOP_PATH)
    ap.add_argument("--broader", type=Path, default=BROADER_PATH)
    ap.add_argument("--permits", type=Path, default=PERMITS_PATH)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args(argv)

    since_date = datetime.strptime(args.since, "%Y-%m-%d").date()

    log.info("loading permits CSV…")
    permits, drift = load_relevant_permits(args.permits, since_date, args.units_min, args.units_max)
    log.info("found %d approved %d-%d unit residential permits since %s",
             len(permits), args.units_min, args.units_max, since_date)
    if drift:
        log.warning("%d permits had unrecognized PERMIT_TYPE values (see report)", len(drift))

    log.info("loading wire feeds…")
    top_payload = json.loads(args.top.read_text())
    broader_payload = json.loads(args.broader.read_text())
    top_rows = top_payload["rows"]
    broader_rows = broader_payload["rows"]
    log.info("  top: %d rows, broader: %d rows", len(top_rows), len(broader_rows))

    top_idx = build_address_index(top_rows)
    broader_idx = build_address_index(broader_rows)
    log.info("indexed %d unique elite addresses, %d unique broader addresses",
             len(top_idx), len(broader_idx))

    results = backtest(permits, top_idx, broader_idx, len(top_rows), len(broader_rows))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_md(results, len(top_rows), len(broader_rows),
                                  since_date, args.units_min, args.units_max, drift))

    by_tier = Counter(r["tier"] for r in results)
    log.info("\nresults:")
    log.info("  elite:    %d (%.0f%%)", by_tier.get("elite", 0),
             100 * by_tier.get("elite", 0) / max(1, len(results)))
    log.info("  broader:  %d (%.0f%%)", by_tier.get("broader", 0),
             100 * by_tier.get("broader", 0) / max(1, len(results)))
    log.info("  missed:   %d (%.0f%%)", by_tier.get("missed", 0),
             100 * by_tier.get("missed", 0) / max(1, len(results)))
    combined = (by_tier.get("elite", 0) + by_tier.get("broader", 0)) / max(1, len(results))
    log.info("  combined recall: %.0f%%", 100 * combined)
    log.info("\nwrote %s", args.out.relative_to(ROOT))


if __name__ == "__main__":
    main()
