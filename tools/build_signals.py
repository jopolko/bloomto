"""Build data/signals.json — the daily fast-signals overlay.

Three CKAN-fresh signals (refreshed daily by Toronto Open Data):
  - Severance applications (Committee of Adjustment, Consent type)
  - Demolition permits (active permits filtered to Demolition)
  - Property violations (Property Standards orders, Stop Work, etc.)

This script:
  1. Fetches the three CKAN feeds (with daily-TTL local caches).
  2. Address-normalizes each record.
  3. Loads the *latest* `data/parcels-top.json` + `data/parcels-broader.json`
     and builds a {normalized_address → parcelId} index. We deliberately
     restrict the join to parcels we actually surface in the UI — joining
     against the full 528K-parcel master would be noise.
  4. Writes `data/signals.json` keyed by parcelId.

The output is small (~50–200 KB), the run is ~30 seconds — runs nightly
on the VPS via cron. No full ETL rebuild required to refresh signals.

CLI:
    python3 tools/build_signals.py
    python3 tools/build_signals.py --since-days 90
    python3 tools/build_signals.py --in data/parcels-top.json --out data/signals.json
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.sources._address import normalize_address
from tools.sources import (
    coa_applications as coa_src,
    demo_permits as demo_src,
    property_violations as viol_src,
    preliminary_zoning_reviews as pzr_src,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_log = logging.getLogger("bloomto.build_signals")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = PROJECT_ROOT / "tools" / "cache"
DEFAULT_OUT = PROJECT_ROOT / "data" / "signals.json"
DEFAULT_TOP = PROJECT_ROOT / "data" / "parcels-top.json"
DEFAULT_BROADER = PROJECT_ROOT / "data" / "parcels-broader.json"

# Default lookback window. All three signals filtered to the same 12-month
# scope so the frontend pill can honestly say "owners moving in the last 12
# months" without mixing time horizons.
#
# Why 365 for severance even though City keeps stale "active" applications
# alive for years: a Consent application filed in 2015 that's still
# technically open is overwhelmingly likely to be a zombie (planner stopped
# pushing, never formally withdrew). Real dev signal is the recent filings.
DEFAULT_SINCE_DAYS_PERMITS = 365
DEFAULT_SINCE_DAYS_VIOLATIONS = 365
DEFAULT_SINCE_DAYS_SEVERANCE = 365
# ZPR (preliminary zoning review) is the earliest pre-application
# signal — owner / dev pings the City "what can I build at X?". The
# hottest of the four signals decays quickly: after 365 days, whoever
# pulled the ZPR has either moved (formal app, deal flow) or moved
# on. Keep the same window for consistency.
DEFAULT_SINCE_DAYS_PZR = 365


def _load_parcel_index(*paths: Path) -> tuple[dict, dict]:
    """Build {normalized_address → parcelId} + {parcelId → row} from the
    given projection JSONs. Later files in the list override earlier ones
    for any address conflict — but elite/broader use the same parcelIds,
    so this is effectively a union.
    """
    addr_to_id: dict[str, str] = {}
    id_to_row: dict[str, dict] = {}
    for path in paths:
        if not path.exists():
            _log.warning("missing %s — skipping", path)
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("rows", []) or []
        for r in rows:
            pid = str(r.get("parcelId") or "")
            if not pid:
                continue
            id_to_row[pid] = r
            addr = (r.get("address") or "").strip()
            if not addr:
                continue
            norm = normalize_address(addr)
            if norm and norm not in addr_to_id:
                addr_to_id[norm] = pid
        _log.info("loaded %d rows from %s", len(rows), path.name)
    _log.info(
        "parcel index: %d unique parcels, %d unique normalized addresses",
        len(id_to_row), len(addr_to_id),
    )
    return addr_to_id, id_to_row


def _join_to_parcels(records, address_index: dict) -> tuple[dict, int, int]:
    """Group records by parcelId. Returns (by_pid, matched, unmatched)."""
    by_pid: dict[str, list] = defaultdict(list)
    matched = 0
    unmatched = 0
    for r in records:
        pid = address_index.get(r.address_norm)
        if pid:
            by_pid[pid].append(r)
            matched += 1
        else:
            unmatched += 1
    return by_pid, matched, unmatched


def _severance_payload(apps) -> dict:
    """Per-parcel severance payload — at most one active application per
    parcel is realistic; if several, take the most recent."""
    apps = sorted(apps, key=lambda a: a.in_date or "", reverse=True)
    a = apps[0]
    return {
        "filedDate": a.in_date or None,
        "hearingDate": a.hearing_date,
        "lotsCreated": a.lots_created,
        "subType": a.sub_type or None,
        "description": a.description or None,
        "applicationUrl": a.application_url,
        "status": a.status,
        "ward": a.ward_name,
        "extraCount": len(apps) - 1,  # additional active CO apps on same lot
    }


def _demo_payload(permits) -> dict:
    """Per-parcel demo-permit payload — surface the most recent + count."""
    permits = sorted(
        permits,
        key=lambda p: max(p.application_date or "", p.issued_date or ""),
        reverse=True,
    )
    p = permits[0]
    return {
        "permitNum": p.permit_num,
        "applicationDate": p.application_date or None,
        "issuedDate": p.issued_date,
        "status": p.status,
        "structureType": p.structure_type,
        "description": p.description or None,
        "currentUse": p.current_use,
        "proposedUse": p.proposed_use,
        "dwellingUnitsLost": p.dwelling_units_lost,
        "builderName": p.builder_name,
        "extraCount": len(permits) - 1,
    }


def _pzr_payload(pzrs) -> dict:
    """Per-parcel preliminary-zoning-review payload — pick most recent."""
    pzrs = sorted(pzrs, key=lambda p: p.application_date or "", reverse=True)
    p = pzrs[0]
    return {
        "permitNum": p.permit_num,
        "applicationDate": p.application_date or None,
        "completedDate": p.completed_date,
        "status": p.status,
        "extraCount": len(pzrs) - 1,
    }


def _violation_payload(viols) -> dict:
    """Per-parcel violation payload — surface the most-severe + most-recent."""
    # Sort by (severity desc, in_date desc).
    viols = sorted(viols, key=lambda v: (-v.severity, v.in_date or ""), reverse=False)
    # Re-sort: severity DESC, then in_date DESC
    viols = sorted(viols, key=lambda v: (v.severity, v.in_date or ""), reverse=True)
    v = viols[0]
    return {
        "filedDate": v.in_date or None,
        "issueDate": v.issue_date,
        "status": v.status,
        "severity": v.severity,
        "subType": v.subtype or None,
        "work": v.work,
        "description": v.description or None,
        "folderNumber": v.folder_number,
        "extraCount": len(viols) - 1,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Build data/signals.json — daily fast-signals overlay.",
    )
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--top", type=Path, default=DEFAULT_TOP)
    parser.add_argument("--broader", type=Path, default=DEFAULT_BROADER)
    parser.add_argument(
        "--since-days-permits", type=int, default=DEFAULT_SINCE_DAYS_PERMITS,
        help="filter demolition permits to last N days (default 365)",
    )
    parser.add_argument(
        "--since-days-violations", type=int, default=DEFAULT_SINCE_DAYS_VIOLATIONS,
        help="filter violations to last N days (default 365)",
    )
    parser.add_argument(
        "--since-days-severance", type=int, default=DEFAULT_SINCE_DAYS_SEVERANCE,
        help="filter severance applications by filing date — last N days (default 365)",
    )
    parser.add_argument(
        "--since-days-pzr", type=int, default=DEFAULT_SINCE_DAYS_PZR,
        help="filter preliminary zoning reviews by application date — last N days (default 365)",
    )
    args = parser.parse_args()

    today = date.today()
    permits_since = (today - timedelta(days=args.since_days_permits)).isoformat()
    violations_since = (today - timedelta(days=args.since_days_violations)).isoformat()
    severance_since = (today - timedelta(days=args.since_days_severance)).isoformat()
    pzr_since = (today - timedelta(days=args.since_days_pzr)).isoformat()

    addr_index, _id_to_row = _load_parcel_index(args.top, args.broader)
    if not addr_index:
        _log.error("empty parcel index — run build_parcels_top.py first")
        return 1

    # Fetch the four CKAN feeds
    severances = coa_src.fetch_severance_applications(args.cache, since_iso=severance_since)
    demos = demo_src.fetch_demo_permits(args.cache, since_iso=permits_since)
    violations = viol_src.fetch_property_violations(args.cache, since_iso=violations_since)
    pzrs = pzr_src.fetch_preliminary_zoning_reviews(args.cache, since_iso=pzr_since)

    # Address-join each
    sev_by_pid, sev_m, sev_u = _join_to_parcels(severances, addr_index)
    demo_by_pid, demo_m, demo_u = _join_to_parcels(demos, addr_index)
    viol_by_pid, viol_m, viol_u = _join_to_parcels(violations, addr_index)
    pzr_by_pid, pzr_m, pzr_u = _join_to_parcels(pzrs, addr_index)

    _log.info(
        "joined: severance %d/%d  demo %d/%d  violations %d/%d  ZPR %d/%d  (matched/total)",
        sev_m, sev_m + sev_u,
        demo_m, demo_m + demo_u,
        viol_m, viol_m + viol_u,
        pzr_m, pzr_m + pzr_u,
    )

    # Build per-parcel payloads
    by_parcel: dict[str, dict] = {}
    for pid, apps in sev_by_pid.items():
        by_parcel.setdefault(pid, {})["severance"] = _severance_payload(apps)
    for pid, permits in demo_by_pid.items():
        by_parcel.setdefault(pid, {})["demoPermit"] = _demo_payload(permits)
    for pid, viols in viol_by_pid.items():
        by_parcel.setdefault(pid, {})["violation"] = _violation_payload(viols)
    for pid, pzrs in pzr_by_pid.items():
        by_parcel.setdefault(pid, {})["prelimZoning"] = _pzr_payload(pzrs)

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "windowDays": {
            "demoPermits": args.since_days_permits,
            "violations": args.since_days_violations,
            "severances": args.since_days_severance,
            "prelimZoning": args.since_days_pzr,
        },
        "stats": {
            "severance": {
                "total": len(severances),
                "matched": sev_m, "unmatched": sev_u,
                "parcels": len(sev_by_pid),
            },
            "demoPermit": {
                "total": len(demos),
                "matched": demo_m, "unmatched": demo_u,
                "parcels": len(demo_by_pid),
            },
            "violation": {
                "total": len(violations),
                "matched": viol_m, "unmatched": viol_u,
                "parcels": len(viol_by_pid),
            },
            "prelimZoning": {
                "total": len(pzrs),
                "matched": pzr_m, "unmatched": pzr_u,
                "parcels": len(pzr_by_pid),
            },
        },
        "byParcelId": by_parcel,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(args.out)
    out_kb = args.out.stat().st_size / 1024
    _log.info(
        "DONE: %d parcels with at least one signal → %s | %.1f KB",
        len(by_parcel), args.out, out_kb,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
