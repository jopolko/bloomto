"""Committee of Adjustment Applications — daily-refreshed CKAN feed.

Surfaces active **Consent (severance)** applications — owners formally
asking the City to subdivide their lot. This is the strongest pre-listing
signal a multiplex dev can act on: the owner has hired a planner, paid
the City filing fee (~$1,200), and is on record at a public hearing.
Almost always pre-sale or pre-development.

Filtering scope:
- `APPLICATION_TYPE == 'CO'` (Consent / severance only — drops Minor
  Variance noise which is 92% of the dataset volume)
- Active applications resource (drops Closed since 2017)
- Optional date filter via `since_iso` to scope to last 90/180/365 days

CKAN refresh cadence: **daily**. Runs in seconds; cached fetch only
re-runs if the resource is new.

Wire-side this is consumed by `tools/build_signals.py`, which address-joins
each record to a parcel and writes the per-parcel `severance` payload to
`data/signals.json`. The frontend reads that file alongside parcels-top.json.
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from tools.sources._address import normalize_address

CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action"
PACKAGE_ID = "committee-of-adjustment-applications"
ACTIVE_RESOURCE_NAME = "Active Applications"
CACHE_FILENAME = "coa_active.json"
CACHE_TTL_S = 24 * 3600  # daily

_log = logging.getLogger(__name__)


@dataclass
class SeveranceApp:
    """One active Committee of Adjustment Consent (severance) application."""
    sys_id: str
    address_norm: str       # canonical "STREET_NUM STREET_NAME STREET_TYPE"
    raw_address: str        # human-readable form for the UI
    in_date: str            # ISO yyyy-mm-dd, when filed
    hearing_date: str | None
    lots_created: int | None
    sub_type: str
    work_type: str
    description: str
    application_url: str | None
    status: str | None
    ward_name: str | None


def _is_cache_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < CACHE_TTL_S


def _resolve_active_resource_id() -> str:
    """Locate the resource_id for the Active Applications datastore."""
    pkg = requests.get(
        f"{CKAN_BASE}/package_show",
        params={"id": PACKAGE_ID},
        timeout=30,
    ).json()["result"]
    for r in pkg.get("resources", []):
        if r.get("name") == ACTIVE_RESOURCE_NAME and r.get("datastore_active"):
            return r["id"]
    raise RuntimeError(f"could not find datastore-active {ACTIVE_RESOURCE_NAME!r} in {PACKAGE_ID}")


def _fetch_active(cache_path: Path) -> list[dict]:
    """Fetch the full Active Applications datastore via CKAN datastore_search.
    The dataset is small (~3K rows), no paging needed beyond limit=10000.
    """
    if _is_cache_fresh(cache_path):
        _log.info("coa_applications: using cached %s", cache_path)
        with cache_path.open(encoding="utf-8") as fp:
            return json.load(fp)

    res_id = _resolve_active_resource_id()
    _log.info("coa_applications: fetching active applications via datastore_search…")
    resp = requests.get(
        f"{CKAN_BASE}/datastore_search",
        params={"resource_id": res_id, "limit": 10000},
        timeout=120,
    )
    resp.raise_for_status()
    records = resp.json().get("result", {}).get("records", []) or []
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(records), encoding="utf-8")
    _log.info("coa_applications: cached %d active applications", len(records))
    return records


def _format_address(rec: dict) -> tuple[str, str]:
    """Build (raw_address, normalized_address) from CoA record fields."""
    num = (rec.get("STREET_NUM") or "").strip()
    name = (rec.get("STREET_NAME") or "").strip()
    typ = (rec.get("STREET_TYPE") or "").strip()
    direction = (rec.get("STREET_DIRECTION") or "").strip()
    parts = [p for p in (num, name, typ, direction) if p]
    raw = " ".join(parts)
    return raw, normalize_address(raw)


def _coerce_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_severance_applications(
    cache_dir: Path,
    *,
    since_iso: str | None = None,
) -> list[SeveranceApp]:
    """Return the list of active Consent (severance) applications.

    `since_iso`: optional yyyy-mm-dd filter on `IN_DATE` (when filed).
    Defaults to no filter (all open consents regardless of age).
    """
    cache = Path(cache_dir)
    raw = _fetch_active(cache / CACHE_FILENAME)

    out: list[SeveranceApp] = []
    for rec in raw:
        if rec.get("APPLICATION_TYPE") != "CO":
            continue
        in_date = (rec.get("IN_DATE") or "")[:10]
        if since_iso and in_date < since_iso:
            continue
        raw_addr, norm_addr = _format_address(rec)
        if not norm_addr:
            continue
        out.append(SeveranceApp(
            sys_id=str(rec.get("SYS_ID") or rec.get("_id") or ""),
            address_norm=norm_addr,
            raw_address=raw_addr,
            in_date=in_date,
            hearing_date=(rec.get("HEARING_DATE") or "")[:10] or None,
            lots_created=_coerce_int(rec.get("NUMBER_OF_LOTS_CREATED")),
            sub_type=(rec.get("SUB_TYPE") or "").strip(),
            work_type=(rec.get("WORK_TYPE") or "").strip(),
            description=(rec.get("DESCRIPTION") or "").strip(),
            application_url=(rec.get("APPLICATION_URL") or "").strip() or None,
            status=(rec.get("STATUSDESC") or "").strip() or None,
            ward_name=(rec.get("WARD_NAME") or "").strip() or None,
        ))
    _log.info(
        "coa_applications: %d active CO (severance) applications%s",
        len(out),
        f" since {since_iso}" if since_iso else "",
    )
    return out
