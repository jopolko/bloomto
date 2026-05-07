"""Building Construction / Demolition Violations — daily-refreshed CKAN feed.

The ML&S (Municipal Licensing & Standards) division issues these against
property owners for unpermitted construction, unsafe structures, stop-work
violations, and similar enforcement actions. A multiplex dev reading this
feed sees owners under City pressure — strongest "motivated seller" signal
publicly available.

Filtering scope:
- `STATUS` is one of the *active* enforcement states:
  - `Order Issued` (the bulk of distress)
  - `Stop Work Order Issued` (active construction halted)
  - `Unsafe Order Issued` (building deemed unsafe)
  - `Notice Issued` (formal notice)
  - `Prosecution Initiated` (escalated to court)
- Drops `Closed`, `Order Complied`, `Cancelled`, `Rescheduled`, etc.
- Optional date filter via `since_iso` to scope to recent filings.

CKAN refresh cadence: **daily**. Dataset has ~48K rows total — historic +
active. Filtering trims to ~3K active distress signals citywide.
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from tools.sources._address import normalize_address
from tools.sources._http import download_with_retries

CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action"
PACKAGE_ID = "building-construction-demolition-violations"
CACHE_FILENAME = "property_violations.json"
CACHE_TTL_S = 24 * 3600  # daily

# Status values that signal an owner is currently under City pressure.
ACTIVE_DISTRESS_STATUSES = frozenset({
    "Order Issued",
    "Stop Work Order Issued",
    "Unsafe Order Issued",
    "Emergency Order Issued",
    "Notice Issued",
    "Prosecution Initiated",
})

# WORK substrings that flag a violation as irrelevant to multiplex prospecting.
# Sign violations are commercial signage on retail / restaurant frontage —
# nothing to do with the residential / mixed-use teardown thesis. Drop.
IGNORED_WORK_SUBSTRINGS = (
    "sign no permit",
    "sign complaint",
    "sign other",
    "notice issued signs",
)

# Severity ranking for sorting — higher = more pressure on the owner.
STATUS_SEVERITY = {
    "Emergency Order Issued": 5,
    "Stop Work Order Issued": 4,
    "Unsafe Order Issued": 3,
    "Prosecution Initiated": 3,
    "Order Issued": 2,
    "Notice Issued": 1,
}

_log = logging.getLogger(__name__)


@dataclass
class PropertyViolation:
    folder_rsn: str
    address_norm: str
    raw_address: str
    in_date: str         # yyyy-mm-dd, when filed
    issue_date: str | None
    status: str
    severity: int        # 1 (Notice) … 5 (Emergency Order)
    subtype: str
    work: str | None
    description: str
    folder_number: str | None


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_TTL_S


def _resolve_json_url() -> str:
    """The full JSON resource is the cleanest fetch — small enough to grab
    in one shot, no datastore_search paging required."""
    pkg = requests.get(
        f"{CKAN_BASE}/package_show",
        params={"id": PACKAGE_ID},
        timeout=30,
    ).json()["result"]
    for r in pkg.get("resources", []):
        if r.get("format", "").upper() == "JSON" and not r.get("name", "").endswith(".csv"):
            return r["url"]
    raise RuntimeError(f"could not find JSON resource on {PACKAGE_ID}")


def _fetch_violations(cache_path: Path) -> list[dict]:
    if _is_cache_fresh(cache_path):
        _log.info("property_violations: using cached %s", cache_path)
        with cache_path.open(encoding="utf-8") as fp:
            return json.load(fp)
    url = _resolve_json_url()
    _log.info("property_violations: fetching JSON resource…")
    download_with_retries(url, cache_path, timeout=120)
    with cache_path.open(encoding="utf-8") as fp:
        records = json.load(fp)
    _log.info("property_violations: cached %d violation rows", len(records))
    return records


def fetch_property_violations(
    cache_dir: Path,
    *,
    since_iso: str | None = None,
) -> list[PropertyViolation]:
    """Return the list of currently-active enforcement actions.

    `since_iso`: optional yyyy-mm-dd filter on `BRINDATE` (when filed).
    Useful for surfacing only fresh distress signals (e.g. last 90 days).
    """
    cache = Path(cache_dir)
    raw = _fetch_violations(cache / CACHE_FILENAME)

    out: list[PropertyViolation] = []
    for rec in raw:
        status = (rec.get("STATUS") or "").strip()
        if status not in ACTIVE_DISTRESS_STATUSES:
            continue
        work_lc = (rec.get("WORK") or "").lower()
        if any(s in work_lc for s in IGNORED_WORK_SUBSTRINGS):
            continue
        in_date = (rec.get("BRINDATE") or rec.get("INDATE") or "")[:10]
        if since_iso and in_date < since_iso:
            continue
        addr_raw = (rec.get("SITEADDRESS") or "").strip()
        if not addr_raw:
            continue
        norm = normalize_address(addr_raw)
        if not norm:
            continue
        out.append(PropertyViolation(
            folder_rsn=str(rec.get("BRFOLDERRSN") or rec.get("FOLDERRSN") or ""),
            address_norm=norm,
            raw_address=addr_raw,
            in_date=in_date,
            issue_date=(rec.get("ISSUEDATE") or "")[:10] or None,
            status=status,
            severity=STATUS_SEVERITY.get(status, 0),
            subtype=(rec.get("SUBTYPE") or "").strip(),
            work=(rec.get("WORK") or "").strip() or None,
            description=(rec.get("DESCRIPTION") or "").strip(),
            folder_number=(rec.get("FOLDERNUMBER") or "").strip() or None,
        ))
    _log.info(
        "property_violations: %d active enforcement actions%s",
        len(out),
        f" since {since_iso}" if since_iso else "",
    )
    return out
