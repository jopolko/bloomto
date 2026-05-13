"""Demolition permits — daily-refreshed CKAN feed.

Filtered subset of `building-permits-active-permits` where the work is a
demolition. Two paths into the same set:

- `WORK == 'Demolition'` (the work-type label)
- `PERMIT_TYPE == 'Demolition Folder (DM)'` (Toronto's permit-type code)

Either is sufficient — we OR them so a typo or upstream schema drift in
one field doesn't lose rows.

A demolition permit on a multiplex-eligible parcel is the strongest
"owner-is-moving" signal we surface:
- Owner pulled the permit, paid the fee, posted public notice
- They're either selling the cleared site (offer them land cost!) or
  DIY-redeveloping (call them anyway — they're a real-estate-active person)
- Filed-but-not-issued status means it's pending — even earlier signal

CKAN refresh cadence: **daily**. Resource has 230K active permits citywide;
filtering to demolition + last 90 days drops to a handful per week of
genuinely-fresh signal.
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from tools.sources._address import normalize_address

CKAN_BASE = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action"
PACKAGE_ID = "building-permits-active-permits"
RESOURCE_NAME = "building-permits-active-permits"
CACHE_FILENAME = "demo_permits.json"
CACHE_TTL_S = 24 * 3600  # daily

# CKAN datastore_search caps at 32000 rows per request — page through.
_PAGE_SIZE = 32000

_log = logging.getLogger(__name__)


@dataclass
class DemoPermit:
    permit_num: str
    address_norm: str
    raw_address: str
    application_date: str   # yyyy-mm-dd
    issued_date: str | None
    status: str
    structure_type: str | None
    description: str
    current_use: str | None
    proposed_use: str | None
    dwelling_units_lost: int | None
    est_const_cost: str | None  # often non-numeric "DO NOT UPDATE…", keep raw
    builder_name: str | None


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
        if r.get("name") == RESOURCE_NAME and r.get("datastore_active"):
            return r["id"]
    raise RuntimeError(f"could not find datastore-active {RESOURCE_NAME!r} in {PACKAGE_ID}")


def _fetch_demo_permits_paged(cache_path: Path) -> list[dict]:
    """Page through the active permits resource with a server-side filter
    on PERMIT_TYPE = 'Demolition Folder (DM)'.

    We can't easily AND-OR filter against `WORK` server-side via CKAN, so
    we make two passes (one per filter) and dedupe by `_id`.
    """
    if _is_cache_fresh(cache_path):
        _log.info("demo_permits: using cached %s", cache_path)
        with cache_path.open(encoding="utf-8") as fp:
            return json.load(fp)

    res_id = _resolve_resource_id()

    def _page_with_filter(filters: dict) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            resp = requests.get(
                f"{CKAN_BASE}/datastore_search",
                params={
                    "resource_id": res_id,
                    "limit": _PAGE_SIZE,
                    "offset": offset,
                    "filters": json.dumps(filters),
                },
                timeout=120,
            )
            resp.raise_for_status()
            records = resp.json().get("result", {}).get("records", []) or []
            out.extend(records)
            if len(records) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
        return out

    _log.info("demo_permits: fetching by PERMIT_TYPE='Demolition Folder (DM)'…")
    a = _page_with_filter({"PERMIT_TYPE": "Demolition Folder (DM)"})
    _log.info("demo_permits: fetching by WORK='Demolition'…")
    b = _page_with_filter({"WORK": "Demolition"})

    # Dedupe by `_id`
    seen = set()
    merged: list[dict] = []
    for rec in a + b:
        rid = rec.get("_id")
        if rid in seen:
            continue
        seen.add(rid)
        merged.append(rec)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(merged), encoding="utf-8")
    _log.info(
        "demo_permits: cached %d unique demo permits (a=%d, b=%d, dedup=%d)",
        len(merged), len(a), len(b), (len(a) + len(b)) - len(merged),
    )
    return merged


def _format_address(rec: dict) -> tuple[str, str]:
    num = (rec.get("STREET_NUM") or "").strip()
    name = (rec.get("STREET_NAME") or "").strip()
    typ = (rec.get("STREET_TYPE") or "").strip()
    direction = (rec.get("STREET_DIRECTION") or "").strip()
    parts = [p for p in (num, name, typ, direction) if p]
    raw = " ".join(parts)
    return raw, normalize_address(raw)


def _coerce_int(v) -> int | None:
    if v is None or v == "" or v == "None":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_demo_permits(
    cache_dir: Path,
    *,
    since_iso: str | None = None,
) -> list[DemoPermit]:
    """Return the list of active demolition permits.

    `since_iso`: optional yyyy-mm-dd filter on the *later* of APPLICATION_DATE
    and ISSUED_DATE (most recent activity). Defaults to no filter.
    """
    cache = Path(cache_dir)
    raw = _fetch_demo_permits_paged(cache / CACHE_FILENAME)

    out: list[DemoPermit] = []
    for rec in raw:
        app_date = (rec.get("APPLICATION_DATE") or "")[:10]
        iss_date = (rec.get("ISSUED_DATE") or "")[:10]
        latest = max(app_date, iss_date)  # lex compare on yyyy-mm-dd is correct
        if since_iso and latest < since_iso:
            continue
        raw_addr, norm_addr = _format_address(rec)
        if not norm_addr:
            continue
        out.append(DemoPermit(
            permit_num=str(rec.get("PERMIT_NUM") or ""),
            address_norm=norm_addr,
            raw_address=raw_addr,
            application_date=app_date,
            issued_date=iss_date or None,
            status=(rec.get("STATUS") or "").strip(),
            structure_type=(rec.get("STRUCTURE_TYPE") or "").strip() or None,
            description=(rec.get("DESCRIPTION") or "").strip(),
            current_use=(rec.get("CURRENT_USE") or "").strip() or None,
            proposed_use=(rec.get("PROPOSED_USE") or "").strip() or None,
            dwelling_units_lost=_coerce_int(rec.get("DWELLING_UNITS_LOST")),
            est_const_cost=(rec.get("EST_CONST_COST") or "").strip() or None,
            builder_name=(rec.get("BUILDER_NAME") or "").strip() or None,
        ))
    _log.info(
        "demo_permits: %d demolition permits%s",
        len(out),
        f" since {since_iso}" if since_iso else "",
    )
    return out
