"""Regression audit — known false-positive parcels must NOT appear in elite.

Accumulates the full list of parcels we've identified through manual spot-
checking as "structurally not multiplex teardowns" — schools, station
infrastructure, government buildings, parking lots, parkettes, etc. The
audit fails (exit non-zero) if any of them re-enters `data/parcels-top.json`.

Run after every projection. Wire into the cron / rebuild pipeline so a
regression in the curation logic surfaces loudly instead of slipping into
the live data.

Each entry pairs `parcelId` (the authoritative key — never changes even if
the address typography drifts) with a brief reason. Address fallback is
provided for parcels where we don't have a stable parcelId on hand.

Usage:
    .venv/bin/python tools/audit_known_false_positives.py
    .venv/bin/python tools/audit_known_false_positives.py --in data/parcels-top.json

Add a new false positive:
    1. Find the parcelId from `data/parcels-top.json` (or the address row).
    2. Append a row to `KNOWN_FALSE_POSITIVES` below with one-line reason.
    3. Run the script — it should pass.
    4. Commit. Future regressions on that parcel will fail the audit.
"""

import argparse
import json
import sys
from pathlib import Path


# Each entry: (parcelId, address, reason). Either parcelId or address can
# be None when only one is known. Both fields are matched (OR) — match either
# one and the audit fails.
KNOWN_FALSE_POSITIVES: list[tuple[str | None, str | None, str]] = [
    # ── Institutional / school complexes (multi-parcel sites where the
    # ── city's institutional point dataset only flags the main building) ──
    ("5491079", "185 Close Ave",          "Parkdale Collegiate / Holy Family School playing field"),
    (None,      "20 Kintyre Ave",         "South Riverdale — large vacant institutional-pattern lot"),
    (None,      "1536 St Clair Ave W",    "Corso Italia-Davenport — large vacant institutional-pattern lot"),
    (None,      "1070 Eastern Ave",       "Greenwood-Coxwell — large vacant institutional-pattern lot"),

    # ── Government / institutional buildings missed by city dataset ──
    (None,      "10 Armoury St",          "Ontario Court of Justice (2023 build, pre-dates Building Outlines refresh)"),

    # ── TTC subway-station infrastructure ──
    (None,      "11 Bedford Rd",          "St. George station entrance"),
    (None,      "9 Bedford Rd",           "St. George station plaza"),
    (None,      "15 Wellesley St E",      "Wellesley station kiosk / parking"),
    (None,      "716 Pape Ave",           "Pape station block"),
    (None,      "30 Alvin Ave",           "Yonge-Eglinton station parking lot"),

    # Add new false positives below this line, with parcelId where available.
    # Format: (pid, address, reason)
]


def audit(top_path: Path) -> int:
    if not top_path.exists():
        print(f"ERROR: input file not found: {top_path}")
        return 1
    payload = json.loads(top_path.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    by_pid = {str(r.get("parcelId") or ""): r for r in rows}
    by_addr = {(r.get("address") or "").strip(): r for r in rows}

    failures: list[tuple[str, str]] = []
    for pid, addr, reason in KNOWN_FALSE_POSITIVES:
        match = None
        if pid and pid in by_pid:
            match = by_pid[pid]
        elif addr and addr in by_addr:
            match = by_addr[addr]
        if match is not None:
            failures.append((
                f"{match.get('address', '?')} (pid {match.get('parcelId', '?')})",
                reason,
            ))

    print(f"audit_known_false_positives — checked {len(KNOWN_FALSE_POSITIVES)} known bad parcels against {top_path} ({len(rows):,} rows)")
    if failures:
        print()
        print(f"FAIL — {len(failures)} known false-positive(s) re-entered the curated set:")
        for desc, reason in failures:
            print(f"  ✗ {desc}")
            print(f"      reason: {reason}")
        print()
        print("The curation gates regressed. Add a stricter rule (positive-residential, "
              "wealthy-enclave filter, OSM landuse, etc.) before shipping.")
        return 1
    print(f"PASS — no known false-positives in elite.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--in", dest="in_path", type=Path,
                   default=Path("data/parcels-top.json"),
                   help="curated parcels file (default data/parcels-top.json)")
    args = p.parse_args(argv)
    return audit(args.in_path)


if __name__ == "__main__":
    sys.exit(main())
