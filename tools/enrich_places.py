#!/usr/bin/env python3
"""
Enrich data/corridors.json newOpenings entries with Google Places data:
  - website (where available)
  - url (Google Maps profile, always present)
  - rating + user_ratings_total
  - matched place name + lat/lng

Cached in tools/cache/places_cache.json so re-runs are cheap.
Reads GOOGLE_API_KEY from /var/secrets/nowservingto.env.

Cost: ~$0.042 per uncached opening (Find Place + Place Details).
Hard abort at $30 cumulative spend per run for safety.
"""
import os, sys, json, time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / 'data' / 'corridors.json'
CACHE_PATH = ROOT / 'tools' / 'cache' / 'places_cache.json'
SECRETS = Path('/var/secrets/nowservingto.env')

COST_FINDPLACE = 0.017
COST_DETAILS   = 0.025  # Basic + Contact + Atmosphere combined
COST_PER_PAIR  = COST_FINDPLACE + COST_DETAILS
COST_HARD_CAP  = 30.00  # USD safety abort

def load_api_key():
    if not SECRETS.exists():
        sys.exit(f"missing {SECRETS}")
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if line.startswith('GOOGLE_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    sys.exit("GOOGLE_API_KEY not found in secrets file")

API_KEY = load_api_key()

def http_get_json(url, params, timeout=15):
    q = urlencode(params)
    req = Request(f"{url}?{q}", headers={'User-Agent': 'nowservingto-enrich/1.0'})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def find_place(query):
    r = http_get_json(
        'https://maps.googleapis.com/maps/api/place/findplacefromtext/json',
        {'input': query, 'inputtype': 'textquery',
         'fields': 'place_id,name,formatted_address', 'key': API_KEY}
    )
    if r.get('status') != 'OK': return None
    cands = r.get('candidates') or []
    return cands[0] if cands else None

def place_details(place_id):
    r = http_get_json(
        'https://maps.googleapis.com/maps/api/place/details/json',
        {'place_id': place_id,
         'fields': 'name,website,types,rating,user_ratings_total,formatted_address,geometry/location,url,business_status',
         'key': API_KEY}
    )
    if r.get('status') != 'OK': return None
    return r.get('result')

def cache_key(name, address):
    return f"{(name or '').strip().upper()}||{(address or '').strip().upper()}"

def _address_matches(queried_addr, matched_addr):
    """Sanity-check that Google's match actually sits on the same street as the
    queried address. Places' fuzzy text search will confidently return a
    completely different restaurant when the name is garbled ("SONARBANGLA" →
    "Ruposhi Bangla Restaurant" 5 km away)."""
    import re
    if not queried_addr or not matched_addr: return False
    m = re.match(r'^\s*(\d+)\s+([A-Za-z]+)', queried_addr)
    if not m: return True
    num, street = m.group(1), m.group(2).upper()
    addr_up = matched_addr.upper()
    return num in addr_up and street in addr_up

def _coords_from_geocode(operating_name, address):
    """Pull lat/lng from the Nominatim geocode cache when find_place fails — we
    can then use Places Nearby Search to find the actual business at those
    coords, which works even when the name is run-together or has hidden
    keywords like 'Premium' that wreck the text-based queries."""
    try:
        import json
        from pathlib import Path
        gc_path = Path(__file__).parent / 'cache' / 'geocode_cache.json'
        if not gc_path.exists(): return None
        c = json.loads(gc_path.read_text())
        # Geocode cache key uses street-only (no postal), so strip postal first
        import re
        a = re.sub(r'\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d$', '', (address or '').upper()).strip()
        key = f"{(operating_name or '').strip().upper()}||{a}"
        e = c.get(key)
        if e and e.get('lat'): return (e['lat'], e['lng'])
    except Exception:
        pass
    return None

def _name_tokens(s):
    import re
    return {t for t in re.findall(r'[A-Z0-9]{2,}', (s or '').upper())
            if t not in {'THE','AND','OF','INC','LTD','CO','LLC',
                         'RESTAURANT','CAFE','BAR','GRILL','KITCHEN','HOUSE','SHOP',
                         'PREMIUM','EXPRESS','TAKE','OUT','TAKEOUT','BISTRO','EATERY'}}

def _name_overlap(a, b):
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb: return 0.0
    return len(ta & tb) / len(ta | tb)

def _nearby_fallback(lat, lng, name_hint):
    """Places Nearby Search at the geocoded coords. 250m radius accounts for
    Nominatim's typical pin offset from Google's business location. Returns
    the result with highest name overlap so we don't accidentally pick a
    neighbouring restaurant in a strip mall."""
    r = http_get_json('https://maps.googleapis.com/maps/api/place/nearbysearch/json',
        {'location': f'{lat},{lng}', 'radius': 250, 'type': 'restaurant', 'key': API_KEY})
    cands = r.get('results') or []
    if not cands: return None
    # Rank by name overlap to disambiguate when several restaurants are nearby
    scored = [(c, _name_overlap(name_hint, c.get('name', ''))) for c in cands]
    scored.sort(key=lambda x: -x[1])
    best, best_score = scored[0]
    # Require at least one shared content-token (drops random nearby restaurants)
    return best if best_score >= 0.2 else None

def enrich_one(operating_name, address):
    addr_first = (address or '').split('M')[0].strip().rstrip(',')
    query = f"{operating_name} {addr_first} Toronto" if addr_first else f"{operating_name} Toronto"
    cand = find_place(query)
    # If the text query missed the actual restaurant (very common when the name
    # is run-together like "SONARBANGLA" or has hidden marketing keywords like
    # "Premium"), fall back to Nearby Search around the geocoded coords.
    if not cand or not _address_matches(addr_first, cand.get('formatted_address')):
        coords = _coords_from_geocode(operating_name, address)
        if coords:
            nearby = _nearby_fallback(coords[0], coords[1], operating_name)
            if nearby:
                cand = nearby  # Nearby Search has same shape (place_id + name + vicinity)
            else:
                return {'status': 'not_found', 'query': query, 'note': 'no nearby match'}
        else:
            return {'status': 'not_found', 'query': query, 'note': 'no coords for nearby fallback'}
    details = place_details(cand['place_id'])
    if not details:
        return {'status': 'no_details', 'place_id': cand['place_id'], 'query': query}
    loc = (details.get('geometry') or {}).get('location') or {}
    return {
        'status': 'ok',
        'place_id': cand['place_id'],
        'matchedName': details.get('name'),
        'matchedAddress': details.get('formatted_address'),
        'website': details.get('website'),
        'mapsUrl': details.get('url'),
        'rating': details.get('rating'),
        'reviewCount': details.get('user_ratings_total'),
        'types': details.get('types'),
        'lat': loc.get('lat'),
        'lng': loc.get('lng'),
        'businessStatus': details.get('business_status'),
        'query': query,
    }

def main():
    data = json.loads(DATA_PATH.read_text())
    no = data.get('newOpenings')
    if not no:
        sys.exit("data/corridors.json has no newOpenings key — run inject_openings.py first")

    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    print(f"cache: {len(cache)} entries already enriched")

    # Collect unique (name, address) pairs across the recent feed and per-cuisine recent5 lists
    pairs = {}
    def add(e):
        k = cache_key(e.get('operatingName'), e.get('address'))
        if k not in pairs: pairs[k] = e
    for e in no.get('recent', []): add(e)
    for c in no.get('cuisines', []):
        for e in c.get('recent5', []): add(e)
        ne = c.get('newest')
        if ne: add(ne)

    to_fetch = [(k, e) for k, e in pairs.items() if k not in cache]
    print(f"openings to enrich: {len(to_fetch)} (skipping {len(pairs) - len(to_fetch)} already cached)")
    est_cost = len(to_fetch) * COST_PER_PAIR
    print(f"estimated API spend: ${est_cost:.2f}")
    if est_cost > COST_HARD_CAP:
        print(f"  (will abort at hard cap ${COST_HARD_CAP:.2f}; not all entries will be processed)")

    spent = 0.0
    ok = err = 0
    t0 = time.time()
    for i, (k, e) in enumerate(to_fetch, 1):
        if spent + COST_PER_PAIR > COST_HARD_CAP:
            print(f"  HARD CAP HIT at ${spent:.2f} after {i-1} requests — stopping")
            break
        try:
            result = enrich_one(e.get('operatingName'), e.get('address'))
            cache[k] = result
            spent += COST_PER_PAIR
            if result['status'] == 'ok': ok += 1
            else: err += 1
            if i % 25 == 0 or i == len(to_fetch):
                el = time.time() - t0
                print(f"  [{i:>4}/{len(to_fetch)}]  ok={ok}  miss={err}  spent=${spent:.2f}  {el:.0f}s elapsed")
                # checkpoint to disk every 25
                CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
        except Exception as ex:
            print(f"  ERROR on {e.get('operatingName')!r}: {ex}")
            err += 1
        # politeness: ~5 req/sec
        time.sleep(0.2)

    CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    print(f"\nFinal: ok={ok}  miss/err={err}  total spent≈${spent:.2f}  cache now={len(cache)}")

    # Now merge cache → corridors.json newOpenings entries
    print("Merging enrichments back into data/corridors.json…")
    def merge(e):
        k = cache_key(e.get('operatingName'), e.get('address'))
        ent = cache.get(k)
        if not ent or ent.get('status') != 'ok': return
        for key in ('website', 'mapsUrl', 'rating', 'reviewCount', 'matchedName', 'lat', 'lng'):
            if ent.get(key) is not None:
                e[key] = ent[key]
    for e in no.get('recent', []): merge(e)
    for c in no.get('cuisines', []):
        for e in c.get('recent5', []): merge(e)
        ne = c.get('newest')
        if ne: merge(ne)

    # quick stats: how many have website vs maps fallback
    n_recent = len(no.get('recent', []))
    n_web = sum(1 for e in no.get('recent', []) if e.get('website'))
    n_maps = sum(1 for e in no.get('recent', []) if e.get('mapsUrl'))
    print(f"  recent feed coverage: {n_web}/{n_recent} have website, {n_maps}/{n_recent} have any link")

    DATA_PATH.write_text(json.dumps(data, separators=(',', ':')))
    print(f"  wrote {DATA_PATH}")

if __name__ == '__main__':
    main()
