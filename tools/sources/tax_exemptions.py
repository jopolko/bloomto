"""Toronto tax-rebate / exemption institutional-address source.

Loads Toronto's "Tax Rebates - Tax Exemptions" XLS (a retired CKAN dataset
that's still available for download, last updated 2012) and returns a set of
normalized addresses where the property is tax-exempt or tax-rebated under
one of seven categories:

    Municipal Capital Facility           — city-owned community facilities
    Charity Rebate                       — registered charities (schools,
                                            social services, religious orgs)
    Veteran Rebate                       — Royal Canadian Legion halls,
                                            Army/Navy/Air Force veterans clubs
    Exemption under Private Legislation  — universities, hospitals, etc.
    Ethno-Cultural Rebate                — cultural community organizations
    Exemption for Exhibition Buildings   — Exhibition Place / CNE / similar

677 unique addresses citywide (verified 2026-05-08 against parcels-top.json:
4 curated parcels matched, all institutional/non-residential — Royal Canadian
Legion Branch #1 at 243 Coxwell Ave being the headline catch).

Used as a HARD exclusion gate in `tools/build_parcels.py` (parallel to the
institutional / OSM landuse / TTC station gates). Catches institutional
buildings whose zoning permits residential redevelopment but whose actual
use makes them non-multiplex teardown candidates regardless.

Stable-but-stale: the city retired the dataset in ~2012, but the property
types it covers (Legion halls, registered charities, city facilities,
universities) rarely change ownership. Address-join is durable for the
overwhelming majority. New additions since 2012 are missed — we'd need a
manual blacklist or a successor city dataset to catch those.
"""

import logging
import sys
from pathlib import Path

import requests
import xlrd

from . import _http
from ._address import normalize_address

CACHE_FILENAME = "tax_exemptions.xls"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "d0d6cc2b-c429-4389-8daa-6c7f79a39bea/resource/"
    "127ce7ff-f64d-4446-98cb-7df38b05dce1/download/tax-rebates-tax-exemptions.xls"
)

# All seven categories are excluded — every one represents an institutional
# / non-residential / community-tenant use that's not a multiplex play.
EXCLUDED_CATEGORIES = frozenset({
    "Municipal Capital Facility",
    "Charity Rebate",
    "Veteran Rebate",
    "Exemption under Private Legislation",
    "Ethno-Cultural Rebate",
    "Exemption for Exhibition Buildings",
})

_log = logging.getLogger(__name__)


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading Toronto tax-exemptions XLS → %s", cached)
    _http.download_with_retries(RESOURCE_URL, cached)
    return cached


def build_exempt_address_set(cache_dir: Path) -> set[str]:
    """Return a set of normalized addresses where the parcel is tax-
    exempt / tax-rebated under one of the EXCLUDED_CATEGORIES.

    The CSV has no header row; columns 0–7 are:
      0: tax year
      1: roll number
      2: site address (raw, e.g., "243 Coxwell Ave")
      3: rebate / exemption category
      4: organization name
      5: dollar amount
      6: bylaw / citation reference
      7: misc

    We dedupe by normalized address — multiple rows for the same address
    (different years, different programs) collapse to one entry.
    """
    cached = _ensure_cached(Path(cache_dir))
    wb = xlrd.open_workbook(cached)
    out: set[str] = set()
    by_category: dict[str, int] = {}

    for ws_name in wb.sheet_names():
        ws = wb.sheet_by_name(ws_name)
        for i in range(ws.nrows):
            try:
                addr_raw = str(ws.cell_value(i, 2)).strip()
                category = str(ws.cell_value(i, 3)).strip()
            except IndexError:
                continue
            if not addr_raw or category not in EXCLUDED_CATEGORIES:
                continue
            normalized = normalize_address(addr_raw)
            if normalized:
                out.add(normalized)
                by_category[category] = by_category.get(category, 0) + 1

    _log.info(
        "tax_exemptions: %d unique exempt/rebate addresses across %d categories. "
        "Per-category row counts (pre-dedup): %s",
        len(out), len(by_category), dict(by_category),
    )
    return out


def is_tax_exempt(address: str | None, exempt_set: set[str]) -> bool:
    """Return True iff the parcel's address normalizes to one in the
    tax-exempt set. Empty/missing addresses always return False (the
    address-required gate elsewhere catches those).
    """
    if not address:
        return False
    norm = normalize_address(address)
    return norm in exempt_set if norm else False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tools/cache")
    s = build_exempt_address_set(cache)
    print(f"exempt set size: {len(s)}")
    print(f"sample (5): {list(s)[:5]}")
