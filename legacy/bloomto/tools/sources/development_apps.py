"""Development Applications — daily-refreshed CKAN feed.

Toronto's `development-applications` dataset lists every Community Planning
+ Committee of Adjustment application filed since 2008-01-01. ~26K records
total; daily-refreshed.

The dev-pipeline-activity signal: a parcel with an active or recently-
approved Site Plan / rezoning application means *somebody is already trying
to build here*. Two-way utility for our small-builder/infill-developer
audience:

- **Negative**: parcel is already taken — strike from active hunting
- **Positive**: comparable activity 200m away validates underwriting

Slots into `signals.json` alongside severance / demo permit / violation /
preliminary zoning review. Same address join, same daily refresh cadence.

Filtering scope:
- `APPLICATION_TYPE in {"SA", "OZ"}` — Site Plan + Zoning Bylaw Amendment.
  These are the multiplex-relevant tracks. CD/SB/PL skew toward
  large-format development that's not our audience.
- Optional `since_iso` filter on `DATE_SUBMITTED` for last-N-day windows.

Status grouping (used downstream in build_signals + UI):
- ACTIVE_STATUSES: in motion, the dev hasn't won/lost yet
- APPROVED_STATUSES: greenlit but possibly not yet built
- CLOSED_STATUSES: dead (refused or formally closed)

CKAN refresh cadence: **daily**. Cached fetch via 32K-row pagination.
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from tools.sources._address import normalize_address

CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action"
PACKAGE_ID = "development-applications"
RESOURCE_NAME = "Development Applications"
CACHE_FILENAME = "development_applications.json"
CACHE_TTL_S = 24 * 3600

# CKAN datastore_search caps at ~32000 rows per request.
_PAGE_SIZE = 32000

# Multiplex-relevant application tracks.
#   SA = Site Plan Application
#   OZ = Official Plan / Zoning Bylaw Amendment (rezoning)
# Excluded:
#   CD = Condominium (large-format)
#   SB / PL = Subdivision / Plan of Subdivision (large-format)
RELEVANT_TYPES = frozenset({"SA", "OZ"})

# Status enum (as it appears in the CSV, raw whitespace tolerated).
# Empirical from sampling 1500 rows on 2026-05-09:
ACTIVE_STATUSES = frozenset({
    "Application Received",
    "Under Review",
    "Appeal Received",
    "OMB Appeal",
})
APPROVED_STATUSES = frozenset({
    "Council Approved",
    "NOAC Issued",
    "OMB Approved",
    "Draft Plan Approved",
    "Final Approval Completed",
    "Approved",
})
CLOSED_STATUSES = frozenset({
    "Closed",
    "Refused",
})

_log = logging.getLogger(__name__)


@dataclass
class DevelopmentApp:
    application_number: str       # "24 216579 STE 13 SA"
    application_type: str         # "SA" | "OZ"
    address_norm: str
    raw_address: str
    date_submitted: str           # yyyy-mm-dd
    status: str                   # raw status string (whitespace-tolerated)
    status_group: str             # "active" | "approved" | "closed" | "unknown"
    description: str              # free text — sometimes hints at structure
    application_url: str | None
    contact_name: str | None
    contact_email: str | None
    ward_number: str | None


def _classify_status(status: str) -> str:
    s = (status or "").strip()
    if s in ACTIVE_STATUSES:
        return "active"
    if s in APPROVED_STATUSES:
        return "approved"
    if s in CLOSED_STATUSES:
        return "closed"
    return "unknown"


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_TTL_S


def _resolve_resource_id() -> str:
    pkg = requests.get(
        f"{CKAN_BASE}/package_show",
        params={"id": PACKAGE_ID},
        timeout=30,
    ).json()["result"]
    for r in pkg.get("resources", []):
        if (r.get("name") == RESOURCE_NAME
                and r.get("format", "").upper() == "CSV"
                and r.get("datastore_active")):
            return r["id"]
    raise RuntimeError(
        f"could not find datastore-active {RESOURCE_NAME!r} in {PACKAGE_ID}"
    )


def _fetch_all(cache_path: Path) -> list[dict]:
    """Page through the resource and cache only multiplex-relevant rows.

    Full dataset is ~26K rows. Filtering to SA/OZ at fetch time drops it
    to ~12K and keeps the on-disk cache tight.
    """
    if _is_cache_fresh(cache_path):
        _log.info("development_apps: using cached %s", cache_path)
        with cache_path.open(encoding="utf-8") as fp:
            return json.load(fp)

    res_id = _resolve_resource_id()
    _log.info("development_apps: fetching from CKAN (paged)…")
    out: list[dict] = []
    offset = 0
    seen_total = 0
    while True:
        resp = requests.get(
            f"{CKAN_BASE}/datastore_search",
            params={"resource_id": res_id, "limit": _PAGE_SIZE, "offset": offset},
            timeout=180,
        )
        resp.raise_for_status()
        recs = resp.json().get("result", {}).get("records", []) or []
        seen_total += len(recs)
        for r in recs:
            if (r.get("APPLICATION_TYPE") or "").strip() in RELEVANT_TYPES:
                out.append(r)
        if len(recs) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(out), encoding="utf-8")
    _log.info(
        "development_apps: scanned %d rows, cached %d SA/OZ",
        seen_total, len(out),
    )
    return out


def _format_address(rec: dict) -> tuple[str, str]:
    num = (rec.get("STREET_NUM") or "").strip()
    name = (rec.get("STREET_NAME") or "").strip()
    typ = (rec.get("STREET_TYPE") or "").strip()
    direction = (rec.get("STREET_DIRECTION") or "").strip()
    parts = [p for p in (num, name, typ, direction) if p]
    raw = " ".join(parts)
    return raw, normalize_address(raw)


def fetch_development_applications(
    cache_dir: Path,
    *,
    since_iso: str | None = None,
) -> list[DevelopmentApp]:
    """Return the list of multiplex-relevant DA rows.

    `since_iso`: optional yyyy-mm-dd filter on `DATE_SUBMITTED`.
    """
    cache = Path(cache_dir)
    raw = _fetch_all(cache / CACHE_FILENAME)

    out: list[DevelopmentApp] = []
    for rec in raw:
        date_sub = (rec.get("DATE_SUBMITTED") or "")[:10]
        if since_iso and date_sub < since_iso:
            continue
        raw_addr, norm_addr = _format_address(rec)
        if not norm_addr:
            continue
        status = (rec.get("STATUS") or "").strip()
        # Truncate description aggressively — the wire-format upper bound
        # for free text is 200 chars (matches our pattern for severance
        # description). Full text is one click away via APPLICATION_URL.
        desc = (rec.get("DESCRIPTION") or "").strip()[:200]
        out.append(DevelopmentApp(
            application_number=str(rec.get("APPLICATION#") or "").strip(),
            application_type=(rec.get("APPLICATION_TYPE") or "").strip(),
            address_norm=norm_addr,
            raw_address=raw_addr,
            date_submitted=date_sub,
            status=status,
            status_group=_classify_status(status),
            description=desc,
            application_url=(rec.get("APPLICATION_URL") or "").strip() or None,
            contact_name=(rec.get("CONTACT_NAME") or "").strip() or None,
            contact_email=(rec.get("CONTACT_EMAIL") or "").strip() or None,
            ward_number=(rec.get("WARD_NUMBER") or "").strip() or None,
        ))
    _log.info(
        "development_apps: %d SA/OZ rows%s",
        len(out),
        f" since {since_iso}" if since_iso else "",
    )
    return out


def index_by_address(apps: list[DevelopmentApp]) -> dict[str, DevelopmentApp]:
    """Most-recent-by-DATE_SUBMITTED wins per normalized address.

    Status priority is *not* applied — the latest filing is the most
    actionable signal even if it's a status downgrade ("Closed" beats
    a stale "Under Review" from 5 years ago).
    """
    out: dict[str, DevelopmentApp] = {}
    for app in apps:
        prev = out.get(app.address_norm)
        if prev is None or app.date_submitted > prev.date_submitted:
            out[app.address_norm] = app
    return out
