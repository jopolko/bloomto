"""Automated internal-consistency audit for the parcels wire.

Runs every cheap check we can do without external network. Reads
data/parcels-top.json and data/parcels-broader.json, dumps findings to
audit/wire_consistency.md grouped by severity:

  CRITICAL  claim is logically impossible / mutually contradictory
  HIGH      column is degenerate (no signal) or rule violated by many rows
  MEDIUM    distribution is suspicious — investigate
  LOW       documentation / sanity flag

Usage:
    python3 tools/audit_wire_consistency.py
    python3 tools/audit_wire_consistency.py --top data/parcels-top.json
"""

import argparse
import json
import logging
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
TOP_PATH = ROOT / "data" / "parcels-top.json"
BROADER_PATH = ROOT / "data" / "parcels-broader.json"
OUT_PATH = ROOT / "audit" / "wire_consistency.md"

# Toronto rough bounding box (a bit generous so legitimate edge parcels pass).
TO_LAT_MIN, TO_LAT_MAX = 43.58, 43.86
TO_LNG_MIN, TO_LNG_MAX = -79.64, -79.11

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


@dataclass
class Finding:
    severity: str
    title: str
    body: str
    examples: list = field(default_factory=list)  # human-readable lines


def _fmt_examples(examples, max_n=5):
    if not examples:
        return ""
    head = examples[:max_n]
    suffix = f"\n  …and {len(examples) - max_n} more" if len(examples) > max_n else ""
    return "\n".join(f"  - {e}" for e in head) + suffix


# Fields that are constant-by-design in the elite/broader projection because
# `tools/build_parcels_top.py:_passes_shared` (and the score>0 gate) filter
# them upstream. The audit was previously firing HIGH on each of these every
# run; we now record them as LOW with the reason so a real new degeneracy
# stands out instead of getting buried.
EXPECTED_GATE_CONSTANTS = {
    "heritageStatus": (None, "_passes_shared excludes any heritage tier"),
    "inRegulatedArea": (False, "_passes_shared excludes TRCA-regulated parcels"),
    "residential": (True, "elite gate requires residential zoning"),
    "solarShadowQuality": ("measured",
        "elite parcels generally have measured shadow quality (synthetic gate)"),
    "inFloodingStudyArea": (True,
        "basement-flooding-study-areas covers ~all pre-1990 residential Toronto; "
        "this dataset is non-discriminating (see memory: project_flood_dataset_choice). "
        "Replace with TRCA Reg 41/24 riverine when endpoint is confirmed."),
}


# ---------- Universal degeneracy ---------------------------------------------

def check_degenerate_columns(rows, file_label):
    findings = []
    if not rows:
        return findings
    n = len(rows)
    cols = sorted(rows[0].keys())
    for col in cols:
        values = [r.get(col) for r in rows]
        nulls = sum(1 for v in values if v is None)
        non_null = [v for v in values if v is not None]
        # All-null
        if nulls == n:
            if col in EXPECTED_GATE_CONSTANTS:
                exp_v, reason = EXPECTED_GATE_CONSTANTS[col]
                if exp_v is None:
                    findings.append(Finding(
                        "LOW",
                        f"`{col}` is null on all {n:,} rows in {file_label} (expected — gate-filtered)",
                        f"Reason: {reason}.",
                    ))
                    continue
            findings.append(Finding(
                "HIGH",
                f"`{col}` is null on all {n:,} rows in {file_label}",
                "Column carries no signal — drop from wire or fix ETL.",
            ))
            continue
        # All-same value
        try:
            uniq = set(non_null) if all(isinstance(v, (str, int, bool, float)) for v in non_null) else None
        except TypeError:
            uniq = None
        if uniq is not None and len(uniq) == 1:
            v = next(iter(uniq))
            if col in EXPECTED_GATE_CONSTANTS:
                exp_v, reason = EXPECTED_GATE_CONSTANTS[col]
                if v == exp_v:
                    findings.append(Finding(
                        "LOW",
                        f"`{col}` is constant `{v!r}` on {n - nulls:,} rows in {file_label} (expected — gate-filtered)",
                        f"Reason: {reason}.",
                    ))
                    continue
            sev = "HIGH" if nulls == 0 else "MEDIUM"
            findings.append(Finding(
                sev,
                f"`{col}` is the constant `{v!r}` on all {n - nulls:,} non-null rows in {file_label}",
                "Either the gate already filters by this value (then drop from wire), "
                "or the column never wired through (then fix ETL).",
            ))
        # Boolean nearly-constant (>99.5% one value)
        elif uniq is not None and uniq <= {True, False} and uniq:
            true_count = sum(1 for v in non_null if v is True)
            frac = true_count / len(non_null)
            if frac >= 0.995 or frac <= 0.005:
                findings.append(Finding(
                    "MEDIUM",
                    f"`{col}` is {frac:.1%} `{frac >= 0.5}` in {file_label}",
                    f"{true_count:,}/{len(non_null):,} rows are `{frac >= 0.5}`. "
                    "Near-constant boolean — verify the gate isn't already excluding the minority.",
                ))
    return findings


# ---------- Logical invariants ---------------------------------------------

def check_logical_invariants(rows, file_label):
    findings = []

    def collect(predicate, why_bad):
        bad = [r for r in rows if predicate(r)]
        return bad

    # outsideTransitBuffer / score / softScore checks dropped 2026-05-07
    # — those wire fields no longer exist (synthesised composites stripped).

    # distSubwayStreetcarM == min(distSubwayM, distStreetcarM)
    mismatches = []
    for r in rows:
        d_sub = r.get("distSubwayM")
        d_str = r.get("distStreetcarM")
        d_comb = r.get("distSubwayStreetcarM")
        if d_sub is None or d_str is None or d_comb is None:
            continue
        expected = min(d_sub, d_str)
        if abs(expected - d_comb) > 0.5:
            mismatches.append(
                f"parcelId={r['parcelId']}  subway={d_sub} streetcar={d_str} combined={d_comb} (expected {expected})"
            )
    if mismatches:
        findings.append(Finding(
            "CRITICAL",
            f"`distSubwayStreetcarM` ≠ min(`distSubwayM`,`distStreetcarM`) on {len(mismatches):,} rows in {file_label}",
            "Combined-distance field is meant to be the row-wise minimum.",
            mismatches,
        ))

    # Numeric range checks
    range_findings = []

    def range_check(field, min_v, max_v, severity="HIGH"):
        bad = []
        for r in rows:
            v = r.get(field)
            if v is None or isinstance(v, bool):
                continue
            if not isinstance(v, (int, float)):
                continue
            if not (min_v <= v <= max_v):
                bad.append(f"parcelId={r['parcelId']}  {field}={v}")
        if bad:
            range_findings.append(Finding(
                severity,
                f"`{field}` outside [{min_v}, {max_v}] on {len(bad):,} rows in {file_label}",
                f"Expected range is [{min_v}, {max_v}].",
                bad,
            ))

    range_check("lat", TO_LAT_MIN, TO_LAT_MAX, "CRITICAL")
    range_check("lng", TO_LNG_MIN, TO_LNG_MAX, "CRITICAL")
    range_check("solarScore", 0, 100)
    range_check("builtYear", 1820, 2026)
    range_check("neighborhoodCanopyPct", 0, 100)
    range_check("buildingCoverageRatio", 0, 1, "MEDIUM")  # sanity — might be 0-100
    range_check("lotAreaM2", 50, 20000, "MEDIUM")  # weird outliers worth flagging
    range_check("distSubwayM", 0, 50000, "MEDIUM")
    range_check("distStreetcarM", 0, 50000, "MEDIUM")
    range_check("distBusM", 0, 50000, "MEDIUM")
    range_check("distBikeLaneM", 0, 50000, "MEDIUM")
    range_check("permitsRecentCount", 0, 1000, "MEDIUM")
    range_check("permitsRecentValueTotal", 0, 1e9, "MEDIUM")
    range_check("nbPermitSampleSize", 0, 100000, "LOW")

    findings.extend(range_findings)

    # lotAspectRatio convention: should be >= 1 (long/short)
    bad = []
    for r in rows:
        v = r.get("lotAspectRatio")
        if isinstance(v, (int, float)) and v < 1:
            bad.append(f"parcelId={r['parcelId']}  lotAspectRatio={v}")
    if bad:
        findings.append(Finding(
            "MEDIUM",
            f"`lotAspectRatio` < 1 on {len(bad):,} rows in {file_label}",
            "Convention is long-axis ÷ short-axis; values < 1 mean the convention flipped or "
            "the field is short ÷ long.",
            bad,
        ))

    # Permits self-consistency: zero count + non-zero value or non-null date
    bad = []
    for r in rows:
        c = r.get("permitsRecentCount")
        v = r.get("permitsRecentValueTotal")
        d = r.get("permitsRecentMostRecentDate")
        if c == 0 and (v not in (None, 0, 0.0)):
            bad.append(f"parcelId={r['parcelId']}  count=0  valueTotal={v}")
        if c == 0 and d not in (None, ""):
            bad.append(f"parcelId={r['parcelId']}  count=0  mostRecentDate={d}")
        if isinstance(c, int) and c > 0 and d in (None, ""):
            bad.append(f"parcelId={r['parcelId']}  count={c} but mostRecentDate is null")
    if bad:
        findings.append(Finding(
            "HIGH",
            f"`permitsRecent*` self-inconsistency on {len(bad):,} rows in {file_label}",
            "Count, valueTotal, and mostRecentDate must be coherent.",
            bad,
        ))

    # Address sanity
    bad = []
    for r in rows:
        a = r.get("address")
        if a in (None, "", "None None", "None"):
            bad.append(f"parcelId={r['parcelId']}  address={a!r}")
    if bad:
        findings.append(Finding(
            "MEDIUM",
            f"`address` is missing/placeholder on {len(bad):,} rows in {file_label}",
            "Frontend falls back to lat/lng but a developer expects a real address; "
            "consider reverse-geocoding in the ETL for these.",
            bad,
        ))

    # builtYear plausibility: many parcels have null OR a year of last-rebuild
    nulls = sum(1 for r in rows if r.get("builtYear") in (None, 0))
    if nulls / len(rows) > 0.3:
        findings.append(Finding(
            "MEDIUM",
            f"`builtYear` is null on {nulls:,}/{len(rows):,} rows ({nulls/len(rows):.1%}) in {file_label}",
            "If `postwarNeighborhood` is computed downstream from `builtYear`, "
            "those rows fall back to neighborhood-level inference — confirm that's intentional.",
        ))

    # Zero-frontage / zero-area parcels
    bad = []
    for r in rows:
        a = r.get("lotAreaM2")
        if a is not None and a < 100:
            bad.append(f"parcelId={r['parcelId']}  lotAreaM2={a}")
    if bad:
        findings.append(Finding(
            "MEDIUM",
            f"`lotAreaM2` < 100 m² on {len(bad):,} rows in {file_label}",
            "Sub-100m² lots are likely artifacts (residual polygons, ROW slivers, "
            "or condo parcels). Should usually be excluded by the gate.",
            bad,
        ))

    return findings


# ---------- Domain coupling -------------------------------------------------

RESIDENTIAL_ZONES = {"R", "RD", "RM", "RA", "RS", "RT", "CRE"}


def check_zoning_coupling(rows, file_label):
    findings = []

    # sixplexEligible should only be True if zoneClass is residential
    bad = []
    for r in rows:
        if r.get("sixplexEligible") and r.get("zoneClass") not in RESIDENTIAL_ZONES and r.get("zoneClass") != "CR":
            bad.append(f"parcelId={r['parcelId']}  zoneClass={r.get('zoneClass')}  sixplexEligible=True")
    if bad:
        findings.append(Finding(
            "HIGH",
            f"`sixplexEligible=True` on non-residential zone in {file_label}: {len(bad):,} rows",
            "Sixplex as-of-right (June 2025) is for residential zones; CR is a "
            "mixed-use carve-out that may be allowed depending on the wording.",
            bad,
        ))

    # sixplexEligible=True should imply maxUnits >= 6
    bad = []
    for r in rows:
        if r.get("sixplexEligible") and isinstance(r.get("maxUnits"), int) and r["maxUnits"] < 6:
            bad.append(
                f"parcelId={r['parcelId']}  sixplexEligible=True  maxUnits={r['maxUnits']}  zoneClass={r.get('zoneClass')}"
            )
    if bad:
        findings.append(Finding(
            "CRITICAL",
            f"`sixplexEligible=True` but `maxUnits<6` on {len(bad):,} rows in {file_label}",
            "Internal contradiction: a sixplex-eligible parcel cannot have a unit cap below 6.",
            bad,
        ))

    # sixplexEligible=False but maxUnits >= 6 — may be fine (e.g. maxUnits is multiplex cap)
    # but still useful to surface. LOW.
    bad = []
    for r in rows:
        if r.get("sixplexEligible") is False and isinstance(r.get("maxUnits"), int) and r["maxUnits"] >= 6:
            bad.append(
                f"parcelId={r['parcelId']}  sixplexEligible=False  maxUnits={r['maxUnits']}  zoneClass={r.get('zoneClass')}"
            )
    if bad and len(bad) > 50:
        findings.append(Finding(
            "LOW",
            f"`sixplexEligible=False` but `maxUnits>=6` on {len(bad):,} rows in {file_label}",
            "Likely fine if `maxUnits` represents another threshold (e.g. fourplex+laneway, "
            "or CR mixed-use cap). Worth confirming the field's documented meaning.",
            bad,
        ))

    # residential=False but zoneClass purely residential
    bad = []
    for r in rows:
        if r.get("residential") is False and r.get("zoneClass") in {"R", "RD", "RM", "RT"}:
            bad.append(f"parcelId={r['parcelId']}  residential=False  zoneClass={r.get('zoneClass')}")
    if bad:
        findings.append(Finding(
            "HIGH",
            f"`residential=False` but residential zoneClass on {len(bad):,} rows in {file_label}",
            "Zoning class and residential flag disagree.",
            bad,
        ))

    # zoneClass=R or RD with maxUnits > 6 (low-density single-family-ish)
    bad = []
    for r in rows:
        if r.get("zoneClass") in {"R", "RD"} and isinstance(r.get("maxUnits"), int) and r["maxUnits"] > 6:
            bad.append(f"parcelId={r['parcelId']}  zoneClass={r['zoneClass']}  maxUnits={r['maxUnits']}")
    if bad and len(bad) > 50:
        findings.append(Finding(
            "MEDIUM",
            f"Low-density zone (R/RD) with `maxUnits>6` on {len(bad):,} rows in {file_label}",
            "R/RD are conventionally low-density. >6 units suggests either Bill-185 multiplex "
            "expansion is being applied here, or the field is mis-derived.",
            bad,
        ))

    return findings


# ---------- Geographic plausibility ---------------------------------------

def check_geographic(rows, file_label):
    findings = []

    # Neighborhood concentration — top 5 should not exceed 50% of rows
    nbhds = Counter(r.get("neighborhood") for r in rows if r.get("neighborhood"))
    top5 = nbhds.most_common(5)
    top5_share = sum(c for _, c in top5) / len(rows) if rows else 0
    if top5_share > 0.5:
        examples = [f"{n}: {c:,} ({c/len(rows):.1%})" for n, c in top5]
        findings.append(Finding(
            "MEDIUM",
            f"Top-5 neighborhoods are {top5_share:.0%} of {file_label}",
            "Healthy citywide ranking should diversify across neighborhoods; "
            "concentration suggests a bias in the gate (zoning multiplier, transit overlay, etc.).",
            examples,
        ))

    # Distinct neighborhood count — Toronto has ~158 (new) or ~140 (old) neighborhoods
    distinct = len(nbhds)
    if distinct < 50:
        findings.append(Finding(
            "MEDIUM",
            f"Only {distinct} distinct neighborhoods represented in {file_label}",
            "Toronto has 140-158 official neighborhoods; <50 covered means many are excluded entirely.",
        ))

    return findings


# ---------- Score-distribution sanity --------------------------------------

def check_score_distribution(rows, file_label):
    findings = []
    scores = [r.get("score") for r in rows if isinstance(r.get("score"), (int, float))]
    if not scores:
        return findings

    # Score quantization — if scores are nearly-discrete (e.g. 30 unique values for 15K rows)
    uniq = len(set(scores))
    if uniq < 15:
        findings.append(Finding(
            "MEDIUM",
            f"`score` has only {uniq} distinct values across {len(scores):,} rows in {file_label}",
            "Heavy quantization usually means downstream sort tiebreaks are deterministic by ETL order. "
            "Consider adding a tiebreaker (e.g. parcelId) to break ties stably.",
        ))

    # Bloom field expected to be boolean — check
    bloom_vals = Counter(type(r.get("bloom")).__name__ for r in rows if r.get("bloom") is not None)
    if len(bloom_vals) > 1:
        findings.append(Finding(
            "MEDIUM",
            f"`bloom` field has mixed types in {file_label}: {dict(bloom_vals)}",
            "Should be a single type (bool or numeric).",
        ))

    return findings


# ---------- Cross-file consistency -----------------------------------------

def check_cross_file(top_rows, broader_rows):
    findings = []
    top_by_id = {r["parcelId"]: r for r in top_rows}
    broader_by_id = {r["parcelId"]: r for r in broader_rows}

    overlap = set(top_by_id) & set(broader_by_id)
    only_top = set(top_by_id) - set(broader_by_id)
    only_broader = set(broader_by_id) - set(top_by_id)

    # Two valid designs:
    #   A) top is a strict subset of broader (top ⊆ broader)
    #   B) top and broader are disjoint (broader = next-tier-only)
    is_subset = (not only_top) and overlap
    is_disjoint = not overlap

    if is_subset:
        # A: scores must agree on overlap
        score_mismatch = []
        for pid in overlap:
            t = top_by_id[pid].get("score")
            b = broader_by_id[pid].get("score")
            if t != b:
                score_mismatch.append(f"parcelId={pid}  top.score={t}  broader.score={b}")
        if score_mismatch:
            findings.append(Finding(
                "CRITICAL",
                f"Score disagreement on {len(score_mismatch):,} parcels in both files",
                "When the same parcel appears in both files, its score must be identical.",
                score_mismatch,
            ))
    elif is_disjoint:
        findings.append(Finding(
            "LOW",
            "top and broader files are disjoint",
            "Design seems to be A=elite, B=next-tier-only. Confirm that's intentional and "
            "that broader.json doesn't accidentally exclude any elite parcel.",
        ))
    else:
        # Mixed — partial overlap. Likely a bug.
        findings.append(Finding(
            "HIGH",
            f"top and broader have partial overlap: "
            f"{len(overlap):,} shared, {len(only_top):,} only-top, {len(only_broader):,} only-broader",
            "Two-tier design is ambiguous. Either top should be a strict subset of broader "
            "(magazine cover + back-of-book) or fully disjoint. Partial overlap suggests an "
            "ETL ordering bug.",
        ))

    # Duplicate parcelIds within each file
    for label, rows in [("top", top_rows), ("broader", broader_rows)]:
        dups = [pid for pid, c in Counter(r["parcelId"] for r in rows).items() if c > 1]
        if dups:
            findings.append(Finding(
                "CRITICAL",
                f"Duplicate parcelIds in {label} file: {len(dups):,} ids appear more than once",
                "Each parcel must appear at most once per file.",
                [f"parcelId={pid}" for pid in dups[:10]],
            ))

    return findings


# ---------- Render ----------------------------------------------------------

def render_md(findings, top_rows, broader_rows):
    by_sev = defaultdict(list)
    for f in findings:
        by_sev[f.severity].append(f)
    counts = {s: len(by_sev[s]) for s in SEVERITIES}

    lines = []
    lines.append("# BloomTO &mdash; wire consistency audit\n")
    from datetime import datetime, timezone
    lines.append(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} &middot; "
                 f"{len(top_rows):,} elite + {len(broader_rows):,} broader rows_\n")
    lines.append("## Summary\n")
    lines.append("| Severity | Count |")
    lines.append("|---|---:|")
    for s in SEVERITIES:
        lines.append(f"| {s} | {counts[s]} |")
    lines.append("")
    if not findings:
        lines.append("No issues found by the internal-consistency checks. Time to step up to "
                     "external cross-checks (zoning point-in-polygon, heritage register diff).")
        return "\n".join(lines)
    for s in SEVERITIES:
        if not by_sev[s]:
            continue
        lines.append(f"\n## {s}\n")
        for i, f in enumerate(by_sev[s], 1):
            lines.append(f"### {i}. {f.title}\n")
            lines.append(f.body)
            if f.examples:
                lines.append("\n**Examples:**\n```")
                for ex in f.examples[:8]:
                    lines.append(ex)
                if len(f.examples) > 8:
                    lines.append(f"…and {len(f.examples) - 8:,} more")
                lines.append("```")
            lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=Path, default=TOP_PATH)
    ap.add_argument("--broader", type=Path, default=BROADER_PATH)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args(argv)

    top_rows = json.loads(args.top.read_text())["rows"]
    broader_rows = json.loads(args.broader.read_text())["rows"]
    log.info("loaded %d top + %d broader rows", len(top_rows), len(broader_rows))

    findings = []
    findings += check_degenerate_columns(top_rows, "top")
    findings += check_degenerate_columns(broader_rows, "broader")
    findings += check_logical_invariants(top_rows, "top")
    findings += check_logical_invariants(broader_rows, "broader")
    findings += check_zoning_coupling(top_rows, "top")
    findings += check_zoning_coupling(broader_rows, "broader")
    findings += check_geographic(top_rows, "top")
    findings += check_score_distribution(top_rows, "top")
    findings += check_score_distribution(broader_rows, "broader")
    findings += check_cross_file(top_rows, broader_rows)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_md(findings, top_rows, broader_rows))

    by_sev = Counter(f.severity for f in findings)
    log.info("\nFindings: %s", {s: by_sev[s] for s in SEVERITIES})
    log.info("Wrote %s", args.out.relative_to(ROOT))
    if any(f.severity == "CRITICAL" for f in findings):
        sys.exit(2)


if __name__ == "__main__":
    main()
