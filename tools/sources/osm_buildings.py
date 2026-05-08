"""OSM building-tag index: address → enum_structure_type from OpenStreetMap.

Volunteer-mapped per-building tags from OpenStreetMap's `building=*` schema.
We pull buildings with `addr:housenumber + addr:street` tags inside Toronto's
bbox via the Overpass API, then map specific tag values to our 5-class enum:

    'house', 'detached', 'bungalow', 'cabin' → "detached"
    'semidetached_house', 'semi-detached'    → "semi"
    'terrace', 'townhouse', 'apartments'     → "row"

OSM coverage on Toronto (verified 2026-05-08): 88,806 buildings with addresses,
72,110 with mappable structure types — adds ~25 % coverage on top of the
permit-derived index, and where the two overlap they agree on 96 % of cases.
Combined permit + OSM coverage on curated cohort = ~58 % (vs 43 % permits-only).

Trust level: secondary to permit data (city-record vs volunteer-mapped). Wire
field `existingStructureSource` distinguishes "permit" / "osm" / "classifier"
so the frontend can mark each parcel's confidence.

Cache: `tools/cache/osm_buildings_typed.json` (~24 MB). Refreshed manually via
`tools/sources/osm_buildings.py refetch` — no auto-update on every build.
"""

import json
import logging
import sys
import time
from pathlib import Path

import requests

CACHE_FILENAME = "osm_buildings_typed.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "BloomTO/0.1 (multiplex parcel finder; contact via project repo)"

# Toronto bbox: 43.58–43.85 lat, -79.65 to -79.12 lng
OVERPASS_QUERY = (
    '[out:json][timeout:300];'
    '('
    '  way["building"]["addr:housenumber"](43.58,-79.65,43.85,-79.12);'
    '  relation["building"]["addr:housenumber"](43.58,-79.65,43.85,-79.12);'
    ');'
    'out tags center;'
)

# OSM `building=*` value → our enum. None = ambiguous, skip.
OSM_BUILDING_TO_ENUM: dict[str, str] = {
    "house":              "detached",
    "detached":           "detached",
    "bungalow":           "detached",
    "cabin":              "detached",
    "semidetached_house": "semi",
    "semi-detached":      "semi",
    "semi_detached":      "semi",
    "terrace":            "row",
    "townhouse":          "row",
    "apartments":         "row",
    # Skipped values: 'yes' (no info), 'residential' (ambiguous mid-rise vs
    # detached), all non-residential (retail/industrial/etc.)
}

# Toronto Open Data parcel addresses use abbreviated street types
# ("Coxwell Ave"); OSM addr:street is full ("Coxwell Avenue"). Map abbrev →
# full when comparing.
_STREET_TYPE_FULL = {
    "AVE": "AVENUE", "AV": "AVENUE",
    "ST": "STREET", "RD": "ROAD", "DR": "DRIVE",
    "BLVD": "BOULEVARD", "CRES": "CRESCENT", "CRT": "COURT",
    "PL": "PLACE", "TER": "TERRACE", "TR": "TRAIL",
    "PKWY": "PARKWAY", "CIR": "CIRCLE", "HTS": "HEIGHTS",
    "HWY": "HIGHWAY", "LANE": "LANE", "LN": "LANE",
    "WAY": "WAY", "SQ": "SQUARE", "GDNS": "GARDENS",
    "GR": "GROVE", "GRV": "GROVE", "PT": "POINT",
    "PROM": "PROMENADE", "MEWS": "MEWS",
}

_log = logging.getLogger(__name__)


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 1000:
        _log.info("using cached %s", cached)
        return cached
    _log.info("fetching OSM building tags from Overpass (~24 MB)...")
    t = time.monotonic()
    r = requests.post(
        OVERPASS_URL,
        data={"data": OVERPASS_QUERY},
        headers={"User-Agent": USER_AGENT},
        timeout=600,
    )
    r.raise_for_status()
    data = r.json()
    cached.write_text(json.dumps(data))
    _log.info(
        "OSM building tags fetched in %.1fs, %d elements → %s",
        time.monotonic() - t, len(data.get("elements", [])), cached,
    )
    return cached


def _parcel_addr_to_osm_form(addr: str) -> str | None:
    """Translate Toronto Open Data parcel-address abbreviations to OSM-style."""
    if not addr:
        return None
    parts = addr.upper().split()
    if len(parts) < 2:
        return None
    if parts[-1] in _STREET_TYPE_FULL:
        parts[-1] = _STREET_TYPE_FULL[parts[-1]]
    return " ".join(parts)


def build_osm_structure_type_index(cache_dir: Path) -> dict[str, str]:
    """Return `{normalized_address: enum_structure_type}` from OSM `building=*`.

    Address normalization: OSM uses full-word street types (Avenue / Street /
    Road) while Toronto's parcel data uses abbreviations (Ave / St / Rd). We
    normalize parcel addresses to OSM form for the lookup. Returns the
    OSM-form key, so callers must apply `_parcel_addr_to_osm_form()` before
    querying.
    """
    cached = _ensure_cached(Path(cache_dir))
    with cached.open() as fp:
        data = json.load(fp)

    out: dict[str, str] = {}
    skipped = 0
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        bv = (tags.get("building") or "").lower()
        enum = OSM_BUILDING_TO_ENUM.get(bv)
        if enum is None:
            skipped += 1
            continue
        num = (tags.get("addr:housenumber") or "").strip().upper()
        street = (tags.get("addr:street") or "").strip().upper()
        if not num or not street:
            continue
        out[f"{num} {street}"] = enum

    _log.info(
        "osm/structure_type: %d unique OSM addresses indexed (~25%% Toronto "
        "coverage); %d elements skipped (no mappable building tag)",
        len(out), skipped,
    )
    return out


def lookup_osm_structure(addr: str, index: dict[str, str]) -> str | None:
    """Convenience: look up a Toronto-Open-Data-form address in the OSM index."""
    osm_form = _parcel_addr_to_osm_form(addr)
    if osm_form is None:
        return None
    return index.get(osm_form)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tools/cache")
    if "refetch" in sys.argv:
        path = cache / CACHE_FILENAME
        if path.exists():
            path.unlink()
            _log.info("removed cache; will re-fetch on next call")
    idx = build_osm_structure_type_index(cache)
    print(f"index size: {len(idx)}")
    from collections import Counter
    print(f"by enum: {dict(Counter(idx.values()))}")
