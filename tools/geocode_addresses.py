#!/usr/bin/env python3
"""
Geocode Toronto street addresses for entries that don't already have coordinates
via Google Places. Uses OpenStreetMap Nominatim — free, no API key, rate-limited
to 1 req/sec by their usage policy.

Reads candidate addresses from corridors.json (newOpenings.recent).
Skips anything already in places_cache OR geocode_cache.
Writes to tools/cache/geocode_cache.json. Idempotent — re-running picks up only
the new delta.

Cron: runs daily after the verification pipeline, before inject_openings.
"""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent
PLACES_CACHE_PATH = ROOT / 'tools' / 'cache' / 'places_cache.json'
GEOCODE_CACHE_PATH = ROOT / 'tools' / 'cache' / 'geocode_cache.json'
WIRE_PATH = ROOT / 'data' / 'corridors.json'

# Nominatim usage policy:
#   - max 1 req/sec
#   - distinct User-Agent identifying the application
#   - contact info if running anything that looks like bulk
# https://operations.osmfoundation.org/policies/nominatim/
UA = 'NowServingTO/1.0 (https://nowservingto.com)'
SLEEP_SEC = 1.1
TIMEOUT_SEC = 15
SAVE_EVERY = 25

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from places_key import cache_key  # canonical shared helper

def geocode_one(address):
    """Returns dict with lat/lng/display_name on success, or {'lat': None, 'lng': None, 'fail': reason}."""
    q = f"{address}, Toronto, Ontario, Canada"
    url = (f"https://nominatim.openstreetmap.org/search"
           f"?q={quote_plus(q)}&format=json&limit=1&countrycodes=ca"
           f"&addressdetails=0")
    try:
        req = Request(url, headers={'User-Agent': UA})
        with urlopen(req, timeout=TIMEOUT_SEC) as r:
            data = json.loads(r.read())
        if not data:
            return {'lat': None, 'lng': None, 'fail': 'no match'}
        h = data[0]
        return {
            'lat': float(h['lat']),
            'lng': float(h['lon']),
            'display_name': h.get('display_name'),
        }
    except Exception as e:
        return {'lat': None, 'lng': None, 'fail': f'{type(e).__name__}: {str(e)[:80]}'}

def main():
    places = json.loads(PLACES_CACHE_PATH.read_text()) if PLACES_CACHE_PATH.exists() else {}
    cache = json.loads(GEOCODE_CACHE_PATH.read_text()) if GEOCODE_CACHE_PATH.exists() else {}
    if not WIRE_PATH.exists():
        sys.exit("corridors.json missing — run build_corridors.py first")
    wire = json.loads(WIRE_PATH.read_text())
    recent = wire.get('newOpenings', {}).get('recent', [])

    targets = []
    seen = set()
    for r in recent:
        name = r.get('operatingName') or ''
        addr = r.get('address') or ''
        if not addr:
            continue
        # Prefer the permit-derived _cacheKey stashed by inject_openings.py
        # (name||addr1 addr3). r.address is just addr1 — possibly overwritten
        # with Places' matchedAddress — neither of which matches places_cache
        # keys. Fall back to building from r.address only when _cacheKey
        # is absent (older corridors.json or non-inject sources).
        k = r.get('_cacheKey') or cache_key(name, addr)
        if k in seen:
            continue
        seen.add(k)
        # Already have coords from Places?
        p = places.get(k)
        if p and (p.get('lat') and p.get('lng')):
            continue
        # Cached success? Skip.
        prev = cache.get(k)
        if prev and prev.get('lat'):
            continue
        targets.append((k, name, addr))

    # Nominatim chokes on Toronto suite suffixes like "220 YONGE ST, D204" or
    # "88 QUEENS QUAY W, FC 9". For previously-failed entries, strip everything
    # after the first comma and retry with just the street.
    def strip_suite(a):
        return a.split(',', 1)[0].strip() if ',' in a else a

    print(f"recent feed entries:    {len(recent)}")
    print(f"already have coords:    {len(recent) - len(targets) - len([1 for r in recent if not r.get('address')])}")
    print(f"to geocode:             {len(targets)}  (~{len(targets)*SLEEP_SEC/60:.0f} min)")
    if not targets:
        print("nothing to do.")
        return

    t0 = time.time()
    n_ok = n_fail = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for i, (k, name, addr) in enumerate(targets, 1):
        result = geocode_one(addr)
        # Retry with suite stripped if first attempt failed and the address had one
        if not result.get('lat'):
            cleaned = strip_suite(addr)
            if cleaned and cleaned != addr:
                time.sleep(SLEEP_SEC)
                retry = geocode_one(cleaned)
                if retry.get('lat'):
                    result = retry
                    result['retried_as'] = cleaned
        result['name'] = name
        result['address'] = addr
        result['geocoded_at'] = now_iso
        result['src'] = 'nominatim'
        cache[k] = result
        if result.get('lat'):
            n_ok += 1
        else:
            n_fail += 1
        if i % SAVE_EVERY == 0 or i == len(targets):
            GEOCODE_CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
            el = time.time() - t0
            rate = i / el if el > 0 else 0
            print(f"  [{i}/{len(targets)}] {el:.0f}s ({rate:.1f}/s)  ok={n_ok} fail={n_fail}")
        time.sleep(SLEEP_SEC)

    GEOCODE_CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    print(f"\nDone in {time.time()-t0:.0f}s: ok={n_ok} fail={n_fail}")

if __name__ == '__main__':
    main()
