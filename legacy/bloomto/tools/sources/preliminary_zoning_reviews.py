"""Preliminary Zoning Reviews — daily-refreshed CKAN feed.

The earliest-stage dev signal: someone (owner / broker / developer)
asks City Planning *"what can I build at address X?"* and gets a
preliminary zoning review back. Daily-refreshed. Filed before any
formal application — sometimes weeks before a Committee of Adjustment
filing, sometimes years before a building permit. By the time another
dev sees a demo permit, this signal has long passed.

The Toronto Open Data dataset `preliminary-zoning-reviews` actually
covers five program tracks (business licences, liquor licences, sign
permits, zoning use, zoning preliminary reviews). We filter strictly
to `PERMIT_NUM` ending in ` ZPR` — the residential / development
preliminary-zoning-review track — so the surfaced rows are the
multiplex-relevant subset only. ~127 ZPR rows / year citywide; we
expect ~5% to land on elite parcels.

Filtering scope:
- `PERMIT_NUM` ends in ` ZPR` (preliminary zoning review track)
- Optional date filter via `since_iso` for last-N-day windows.

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
PACKAGE_ID = "preliminary-zoning-reviews"
RESOURCE_NAME = "Preliminary Zoning Reviews"
CACHE_FILENAME = "preliminary_zoning_reviews.json"
CACHE_TTL_S = 24 * 3600

# CKAN datastore_search caps at ~32000 rows per request.
_PAGE_SIZE = 32000

# Target program code at the tail of PERMIT_NUM. Other codes in this
# dataset (LTO, ZAP, LPR, PSP, etc.) cover liquor / business / signs,
# not multiplex-relevant dev intent.
_TARGET_SUFFIX = " ZPR"

_log = logging.getLogger(__name__)


@dataclass
class PrelimZoningReview:
    permit_num: str
    address_norm: str
    raw_address: str
    application_date: str   # yyyy-mm-dd, when filed
    completed_date: str | None
    status: str


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
    raise RuntimeError(f"could not find datastore-active {RESOURCE_NAME!r} in {PACKAGE_ID}")


def _fetch_all(cache_path: Path) -> list[dict]:
    """Page through the resource and cache only ZPR-suffixed rows.

    The full dataset is ~219K rows (~50MB); filtering to ZPR at fetch
    time drops it to ~5MB and keeps the on-disk cache tight.
    """
    if _is_cache_fresh(cache_path):
        _log.info("preliminary_zoning_reviews: using cached %s", cache_path)
        with cache_path.open(encoding="utf-8") as fp:
            return json.load(fp)

    res_id = _resolve_resource_id()
    _log.info("preliminary_zoning_reviews: fetching from CKAN (paged)…")
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
            if (r.get("PERMIT_NUM") or "").endswith(_TARGET_SUFFIX):
                out.append(r)
        if len(recs) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(out), encoding="utf-8")
    _log.info(
        "preliminary_zoning_reviews: scanned %d rows, cached %d ZPR",
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


def fetch_preliminary_zoning_reviews(
    cache_dir: Path,
    *,
    since_iso: str | None = None,
) -> list[PrelimZoningReview]:
    """Return the list of ZPR rows.

    `since_iso`: optional yyyy-mm-dd filter on `APPLICATION_DATE`.
    """
    cache = Path(cache_dir)
    raw = _fetch_all(cache / CACHE_FILENAME)

    out: list[PrelimZoningReview] = []
    for rec in raw:
        app_date = (rec.get("APPLICATION_DATE") or "")[:10]
        if since_iso and app_date < since_iso:
            continue
        raw_addr, norm_addr = _format_address(rec)
        if not norm_addr:
            continue
        out.append(PrelimZoningReview(
            permit_num=str(rec.get("PERMIT_NUM") or ""),
            address_norm=norm_addr,
            raw_address=raw_addr,
            application_date=app_date,
            completed_date=(rec.get("COMPLETED_DATE") or "")[:10] or None,
            status=(rec.get("STATUS") or "").strip(),
        ))
    _log.info(
        "preliminary_zoning_reviews: %d ZPR rows%s",
        len(out),
        f" since {since_iso}" if since_iso else "",
    )
    return out
