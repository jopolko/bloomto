"""Stratified diversity sample of parcels for manual audit.

Reads data/parcels-top.json + data/parcels-broader.json and writes:
  - audit/manual_sample.html  printable per-parcel cards with deep-links
  - audit/manual_sample.csv   one row per audited parcel for tracking

Sampling strategy: stratified by score band, then greedy diversification on
neighborhood + zoneClass + edge-case flags so the 10 picks span the gate
surface, not just the top scorers.

Usage:
    python3 tools/sample_manual_audit.py
    python3 tools/sample_manual_audit.py --n 12 --seed 42
"""

import argparse
import csv
import html
import json
import logging
import random
import sys
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP_PATH = ROOT / "data" / "parcels-top.json"
BROADER_PATH = ROOT / "data" / "parcels-broader.json"
OUT_DIR = ROOT / "audit"
HTML_OUT = OUT_DIR / "manual_sample.html"
CSV_OUT = OUT_DIR / "manual_sample.csv"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def _bands(top_rows, broader_rows):
    """Return list of (band_label, [rows]) for stratified sampling.

    Top is already sorted by score desc inside each file; we slice by rank.
    Broader-only excludes any parcelId already in top so we don't double-pick.
    """
    top_ids = {r["parcelId"] for r in top_rows}
    broader_only = [r for r in broader_rows if r["parcelId"] not in top_ids]
    n = len(top_rows)
    return [
        ("top-elite (rank 1-50)", top_rows[:50]),
        ("mid-elite (rank 200-1500)", top_rows[200:1500]),
        ("bottom-elite (last 250)", top_rows[max(0, n - 250):]),
        ("broader-mid", broader_only[len(broader_only) // 4: 3 * len(broader_only) // 4]),
        ("broader-low (lowest 500)", broader_only[-500:]),
    ]


def _diversity_key(row):
    """Tuple a sample's diversity is measured against — prefer not to repeat."""
    return (row.get("neighborhood"), row.get("zoneClass"))


def _required_flags(sample):
    """Return dict of required-edge-case-coverage flags currently satisfied."""
    return {
        "cornerLot": any(r.get("cornerLot") for r in sample),
        "abutsLaneway": any(r.get("abutsLaneway") for r in sample),
        "sixplex_no": any(r.get("sixplexEligible") is False for r in sample),
        "postwar": any(r.get("postwarNeighborhood") for r in sample),
        "prewar": any(r.get("postwarNeighborhood") is False for r in sample),
    }


def _row_satisfies_unmet_flag(row, sample):
    flags = _required_flags(sample)
    if not flags["cornerLot"] and row.get("cornerLot"):
        return True
    if not flags["abutsLaneway"] and row.get("abutsLaneway"):
        return True
    if not flags["sixplex_no"] and row.get("sixplexEligible") is False:
        return True
    if not flags["postwar"] and row.get("postwarNeighborhood"):
        return True
    if not flags["prewar"] and row.get("postwarNeighborhood") is False:
        return True
    return False


def pick_sample(top_rows, broader_rows, n=10, seed=1729):
    """Greedy diversity pick. Each iteration:
       1. Rotate through bands so each contributes roughly evenly.
       2. Within a band, pick the candidate that EITHER fills an unmet
          edge-case flag OR minimizes (neighborhood, zoneClass) repetition.
    """
    rng = random.Random(seed)
    bands = _bands(top_rows, broader_rows)

    # Shuffle each band so re-runs with different seeds give different picks.
    for _, rows in bands:
        rng.shuffle(rows)

    sample = []
    used_ids = set()
    band_order = [b[0] for b in bands]
    band_rows_map = dict(bands)
    while len(sample) < n:
        band_label = band_order[len(sample) % len(band_order)]
        band_rows = band_rows_map[band_label]
        # Window = up to 30 still-unused candidates from this band. We pick the
        # best one and only mark THAT one used; the rest stay available for the
        # next time this band's turn comes around.
        window = []
        for cand in band_rows:
            if cand["parcelId"] in used_ids:
                continue
            window.append(cand)
            if len(window) >= 30:
                break
        if not window:
            log.warning("band %s exhausted before sample reached %d", band_label, n)
            break
        # Score each candidate: prefer (1) fills unmet flag, (2) minimizes diversity-key repetition.
        used_keys = Counter(_diversity_key(r) for r in sample)
        max_per_neighborhood = 2
        nbhd_counts = Counter(r.get("neighborhood") for r in sample)

        def cand_score(c):
            fills_flag = _row_satisfies_unmet_flag(c, sample)
            nbhd_overflow = nbhd_counts[c.get("neighborhood")] >= max_per_neighborhood
            div_repeat = used_keys[_diversity_key(c)]
            # Lower is better. Ordering: avoid nbhd overflow, then favor flag-fillers, then minimize repeat.
            return (nbhd_overflow, -int(fills_flag), div_repeat)

        window.sort(key=cand_score)
        chosen = window[0]
        chosen = {**chosen, "_band": band_label}
        sample.append(chosen)
        used_ids.add(chosen["parcelId"])
    return sample


# --- Output ---------------------------------------------------------------

VERIFIER_LINKS_NOTE = (
    "Most City lookups don't accept deep-link by address — open the page, "
    "paste the address from the row above."
)


def _maps_url(lat, lng):
    return f"https://www.google.com/maps/?q={lat},{lng}"


def _streetview_url(lat, lng):
    return f"https://www.google.com/maps?q=&layer=c&cbll={lat},{lng}"


def _google_search_url(addr):
    return "https://www.google.com/search?q=" + urllib.parse.quote(f"{addr} Toronto")


def _bing_birdseye_url(lat, lng):
    return f"https://www.bing.com/maps?cp={lat}~{lng}&style=o&lvl=20"


CLAIMS_TO_AUDIT = [
    # (column, label, what to check, suggested verifier)
    ("address", "Address", "matches the lat/lng on Google Maps", "Google Maps + reverse geocode"),
    ("neighborhood", "Neighborhood", "official City neighbourhood", "Toronto Neighbourhoods Map"),
    ("zoneClass", "Zone class", "current zoning category (R, RD, RM, CR, …)", "Zoning By-law lookup"),
    ("maxUnits", "Max units", "as-of-right unit cap from current bylaw", "Zoning By-law lookup"),
    ("sixplexEligible", "Sixplex eligible", "T&EY citywide + Ward 23 expansion (June 2025)", "City Planning + Council motion"),
    ("heritageStatus", "Heritage status", "Listed / Designated / None on the Heritage Register", "Heritage Register search"),
    ("distSubwayM", "Distance to subway (m)", "walking distance to nearest TTC subway entrance", "Google Maps walking"),
    ("distStreetcarM", "Distance to streetcar (m)", "walking distance to nearest streetcar stop", "TTC route map"),
    ("outsideTransitBuffer", "Outside transit buffer", "true if >500m from subway+streetcar", "Implied by distances above"),
    ("lotAreaM2", "Lot area (m²)", "lot size from property polygon", "MapIT property layer"),
    ("cornerLot", "Corner lot", "visual: parcel touches two streets", "Google Maps satellite"),
    ("abutsLaneway", "Abuts laneway", "visual: parcel rear touches a public laneway", "Google Maps satellite"),
    ("inFloodingStudyArea", "Basement flooding study area", "Toronto Water study area inclusion", "Toronto Water flood map"),
    ("inRegulatedArea", "TRCA Reg 41/24 area", "regulated by TRCA (riverine, wetlands)", "TRCA online mapping"),
]


def _claim_value_str(v):
    if v is None:
        return "<em>(null)</em>"
    if isinstance(v, bool):
        return "✓ true" if v else "✗ false"
    if isinstance(v, float):
        return f"{v:.1f}"
    return html.escape(str(v))


def _wire_audit_findings(top_rows):
    """Pre-flight findings about the wire itself, before the user audits anything."""
    findings = []
    n = len(top_rows)
    hs_null = sum(1 for r in top_rows if r.get("heritageStatus") is None)
    if hs_null == n:
        findings.append(
            "<strong>heritageStatus is None on all "
            f"{n:,} elite rows</strong> — either the gate hard-excludes any "
            "Listed/Designated parcel (expected behaviour, but should still appear "
            "on the wire so the page can <em>say</em> &ldquo;not on the Heritage "
            "Register&rdquo; affirmatively) or the column is being dropped before "
            "wire serialization. Verify in the ETL."
        )
    flood_true = sum(1 for r in top_rows if r.get("inFloodingStudyArea") is True)
    if flood_true == n:
        findings.append(
            "<strong>inFloodingStudyArea is True on all "
            f"{n:,} elite rows</strong> — the basement-flooding-study-areas "
            "dataset hits 100% wire-wide; carries no signal. TRCA Reg 41/24 "
            "riverine was the discriminating layer (per project memory)."
        )
    reg_false = sum(1 for r in top_rows if r.get("inRegulatedArea") is False)
    if reg_false == n:
        findings.append(
            "<strong>inRegulatedArea is False on all "
            f"{n:,} elite rows</strong> — either the gate excludes regulated "
            "parcels (then this column shouldn&rsquo;t be on the wire) or the "
            "TRCA layer never wired through. Same root cause as the flood column."
        )
    return findings


def write_csv(sample, path):
    cols = [
        "rank_in_sample", "band", "parcelId", "address", "neighborhood",
        "lat", "lng", "score", "bloom",
        "zoneClass", "maxUnits", "sixplexEligible", "heritageStatus",
        "distSubwayM", "distStreetcarM", "outsideTransitBuffer",
        "lotAreaM2", "cornerLot", "abutsLaneway",
        "inFloodingStudyArea", "inRegulatedArea",
        "manual_result", "manual_notes",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i, r in enumerate(sample, 1):
            w.writerow([
                i, r["_band"], r["parcelId"], r.get("address", ""),
                r.get("neighborhood", ""), r["lat"], r["lng"],
                r.get("score"), r.get("bloom"),
                r.get("zoneClass"), r.get("maxUnits"), r.get("sixplexEligible"),
                r.get("heritageStatus"),
                r.get("distSubwayM"), r.get("distStreetcarM"),
                r.get("outsideTransitBuffer"),
                r.get("lotAreaM2"), r.get("cornerLot"), r.get("abutsLaneway"),
                r.get("inFloodingStudyArea"), r.get("inRegulatedArea"),
                "",  # manual_result: agree / disagree / unclear
                "",  # manual_notes
            ])


def render_html(sample, wire_findings, top_rows, broader_rows, seed):
    parts = []
    parts.append("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<title>BloomTO manual-audit sample</title>")
    parts.append("""<style>
:root { --line: #e5e7eb; --muted: #6b7280; --accent: #2f6a4f; --warn: #b45309; --bad: #991b1b; }
* { box-sizing: border-box; }
body { font: 14px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; max-width: 920px;
       margin: 24px auto; padding: 0 20px; color: #1f2937; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--muted); margin-bottom: 24px; }
.findings { background: #fef3c7; border: 1px solid #fcd34d; border-radius: 6px;
            padding: 12px 16px; margin-bottom: 24px; }
.findings h2 { font-size: 15px; margin: 0 0 8px; color: var(--warn); }
.findings ul { margin: 4px 0 0 18px; padding: 0; }
.findings li { margin-bottom: 6px; }
.parcel { border: 1px solid var(--line); border-radius: 8px;
          padding: 16px 18px; margin-bottom: 18px; page-break-inside: avoid; }
.parcel header { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.parcel h2 { font-size: 17px; margin: 0; }
.parcel .meta { color: var(--muted); font-size: 12px; }
.parcel .badges { margin: 6px 0 10px; }
.badge { display: inline-block; font-size: 11px; padding: 2px 8px;
         border-radius: 999px; margin-right: 4px; background: #f3f4f6; color: #374151; }
.badge.score { background: #ecfdf5; color: var(--accent); font-weight: 600; }
.badge.warn { background: #fef3c7; color: var(--warn); }
.links a { display: inline-block; font-size: 12px; padding: 4px 10px;
           border: 1px solid var(--line); border-radius: 4px; text-decoration: none;
           color: var(--accent); margin: 2px 4px 2px 0; }
.links a:hover { background: #f9fafb; }
table.claims { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
table.claims th, table.claims td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left;
                                    vertical-align: top; }
table.claims th { font-weight: 600; color: var(--muted); font-size: 11px; text-transform: uppercase; }
table.claims td.value { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
table.claims td.check { white-space: nowrap; color: var(--muted); }
.notes { margin-top: 12px; }
.notes textarea { width: 100%; min-height: 60px; font: 13px/1.4 system-ui; padding: 6px;
                  border: 1px solid var(--line); border-radius: 4px; }
@media print {
  .parcel { break-inside: avoid; }
  textarea { border: none; }
}
</style></head><body>""")
    parts.append("<h1>BloomTO &mdash; manual-audit sample</h1>")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts.append(
        f"<div class='sub'>Generated {now} &middot; seed {seed} &middot; "
        f"{len(sample)} parcels &middot; "
        f"sampled from {len(top_rows):,} elite + "
        f"{len(broader_rows) - len(top_rows):,} broader-only rows</div>"
    )
    if wire_findings:
        parts.append("<div class='findings'><h2>Wire-level findings (no field work needed)</h2><ul>")
        for f in wire_findings:
            parts.append(f"<li>{f}</li>")
        parts.append("</ul><p style='font-size:12px;color:var(--muted);margin:6px 0 0'>"
                     "These three columns short-circuit a developer&rsquo;s confidence "
                     "in the regulatory gates &mdash; fix before relying on the audit results below.</p></div>")
    for i, r in enumerate(sample, 1):
        addr = r.get("address") or "(no address)"
        lat, lng = r["lat"], r["lng"]
        score = r.get("score")
        bloom = r.get("bloom")
        nbhd = r.get("neighborhood", "")
        parts.append("<section class='parcel'>")
        parts.append("<header>")
        parts.append(
            f"<h2>#{i}. {html.escape(addr)}</h2>"
            f"<div class='meta'>{html.escape(nbhd)} &middot; parcelId <code>{html.escape(str(r['parcelId']))}</code></div>"
        )
        parts.append("</header>")
        parts.append("<div class='badges'>")
        parts.append(f"<span class='badge score'>score {score}</span>")
        if bloom:
            parts.append("<span class='badge warn'>visual bloom</span>")
        parts.append(f"<span class='badge'>{html.escape(r['_band'])}</span>")
        if r.get("sixplexEligible"):
            parts.append("<span class='badge'>sixplex eligible</span>")
        if r.get("cornerLot"):
            parts.append("<span class='badge'>corner</span>")
        if r.get("abutsLaneway"):
            parts.append("<span class='badge'>laneway</span>")
        if r.get("postwarNeighborhood"):
            parts.append("<span class='badge'>postwar</span>")
        parts.append("</div>")
        parts.append("<div class='links'>")
        parts.append(f"<a target='_blank' href='{_maps_url(lat, lng)}'>📍 Map</a>")
        parts.append(f"<a target='_blank' href='{_streetview_url(lat, lng)}'>👀 Street View</a>")
        parts.append(f"<a target='_blank' href='{_bing_birdseye_url(lat, lng)}'>🦅 Bing bird&rsquo;s-eye</a>")
        if addr != "(no address)":
            parts.append(f"<a target='_blank' href='{_google_search_url(addr)}'>🔎 Google: {html.escape(addr)}</a>")
        parts.append("<a target='_blank' href='https://map.toronto.ca/maps/map.jsp?app=ZBL_CONSULT'>🏛️ Zoning By-law</a>")
        parts.append("<a target='_blank' href='https://secure.toronto.ca/HeritageInventory/searchProperty.do'>🛡️ Heritage Register</a>")
        parts.append("<a target='_blank' href='https://map.toronto.ca/maps/map.jsp?app=PropertyInfo'>🏘️ Property Info</a>")
        parts.append("<a target='_blank' href='https://trcaca.maps.arcgis.com/apps/webappviewer/index.html?id=1d23f54fda174d27ad5fd2782a2cb43f'>🌊 TRCA mapping</a>")
        parts.append("</div>")
        parts.append("<table class='claims'>")
        parts.append("<thead><tr><th>Claim</th><th>Wire value</th><th>What to verify</th><th>Verifier</th><th>Agree?</th></tr></thead><tbody>")
        for col, label, what, verifier in CLAIMS_TO_AUDIT:
            v = _claim_value_str(r.get(col))
            parts.append(
                f"<tr><td>{html.escape(label)}</td>"
                f"<td class='value'>{v}</td>"
                f"<td>{html.escape(what)}</td>"
                f"<td>{html.escape(verifier)}</td>"
                f"<td class='check'>☐ agree<br>☐ disagree<br>☐ unclear</td></tr>"
            )
        parts.append("</tbody></table>")
        parts.append("<div class='notes'><label style='font-size:12px;color:var(--muted)'>Notes</label>"
                     "<textarea placeholder='Free-text observations &mdash; especially anything counter to the wire claims.'></textarea></div>")
        parts.append("</section>")
    parts.append(f"<p style='color:var(--muted);font-size:12px;margin-top:24px'>{VERIFIER_LINKS_NOTE}</p>")
    parts.append("</body></html>")
    return "".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=10, help="sample size (default 10)")
    ap.add_argument("--seed", type=int, default=1729, help="random seed for reproducibility")
    ap.add_argument("--top", type=Path, default=TOP_PATH)
    ap.add_argument("--broader", type=Path, default=BROADER_PATH)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)

    top_payload = json.loads(args.top.read_text())
    broader_payload = json.loads(args.broader.read_text())
    top_rows = top_payload["rows"]
    broader_rows = broader_payload["rows"]
    log.info("loaded %d top + %d broader rows", len(top_rows), len(broader_rows))

    sample = pick_sample(top_rows, broader_rows, n=args.n, seed=args.seed)
    findings = _wire_audit_findings(top_rows)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "manual_sample.csv"
    html_path = args.out_dir / "manual_sample.html"
    write_csv(sample, csv_path)
    html_path.write_text(render_html(sample, findings, top_rows, broader_rows, args.seed))

    log.info("wrote %s (%d rows)", csv_path.relative_to(ROOT), len(sample))
    log.info("wrote %s", html_path.relative_to(ROOT))
    log.info("\nSample summary (band / score / address / neighborhood):")
    for i, r in enumerate(sample, 1):
        log.info(
            "  %2d. [%s] score=%s %s — %s",
            i, r["_band"], r.get("score"),
            r.get("address") or "(no address)",
            r.get("neighborhood") or "?",
        )


if __name__ == "__main__":
    main()
