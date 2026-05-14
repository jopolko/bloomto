#!/usr/bin/env python3
"""
Build an authoritative chain set from OpenStreetMap.

OSM mappers tag known restaurant chains with `brand=<Name>` (and often
`brand:wikidata=<Qxxx>`). This is curated by humans across the world, and
Toronto's OSM coverage is well-maintained for fast-food and casual-dining
chains. Independent restaurants don't carry `brand=` tags — only chains do.

This is the authoritative source we use to auto-detect chains in
inject_openings.py, replacing the earlier count-heuristic (which mis-tagged
campus food services and missed niche chains under 5 locations).

Queries Overpass API for every restaurant/cafe/bar in Toronto with a `brand`
tag, deduplicates by uppercased brand name, writes to
`tools/cache/osm_chain_set.json`. Free, no API key.

Refresh cadence: weekly is plenty (chain lists move slowly). Cron entry is
optional — falls back to whatever's in the cache file if Overpass is down.
"""
import json, sys, time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / 'tools' / 'cache' / 'osm_chain_set.json'
OVERPASS_URL = 'https://overpass-api.de/api/interpreter'

# Toronto + GTA bounding box covers the licence area. Overpass admin_level for
# "City of Toronto" is 6; querying by bbox is simpler and faster than admin
# area resolution.
TORONTO_BBOX = (43.58, -79.64, 43.86, -79.12)  # (south, west, north, east)

QUERY = f"""
[out:json][timeout:60];
(
  nwr["amenity"~"^(restaurant|fast_food|cafe|bar|pub|food_court|ice_cream|biergarten|nightclub)$"]
     ["brand"]
     ({TORONTO_BBOX[0]},{TORONTO_BBOX[1]},{TORONTO_BBOX[2]},{TORONTO_BBOX[3]});
);
out tags;
"""

def fetch_brands():
    print(f"querying Overpass for branded restaurants in Toronto bbox...")
    req = Request(OVERPASS_URL, data=f"data={QUERY}".encode('utf-8'),
                  headers={'User-Agent': 'nowservingto-osm-chains/1.0'},
                  method='POST')
    t0 = time.time()
    try:
        with urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
    except (HTTPError, URLError) as e:
        sys.exit(f"Overpass request failed: {e}")
    elems = data.get('elements') or []
    print(f"  {len(elems)} branded amenities in {time.time()-t0:.0f}s")

    brands = {}   # UPPER -> {display, wikidata, count}
    for el in elems:
        tags = el.get('tags') or {}
        b = (tags.get('brand') or '').strip()
        if not b: continue
        key = b.upper()
        rec = brands.setdefault(key, {'display': b, 'wikidata': tags.get('brand:wikidata'), 'count': 0})
        rec['count'] += 1
        # Prefer the first wikidata we see; OSM brand:wikidata tags are usually consistent
        if not rec['wikidata'] and tags.get('brand:wikidata'):
            rec['wikidata'] = tags['brand:wikidata']
    return brands

def main():
    brands = fetch_brands()
    # Filter out one-offs that might be miscoded as a "brand". Real chains will
    # have ≥2 OSM-tagged locations in Toronto. (Note: this is OSM coverage, NOT
    # licence-count — chains under-tagged in OSM still pass through; we rely on
    # CHAIN_DENYLIST as backstop for those.)
    real = {k: v for k, v in brands.items() if v['count'] >= 2}
    out = {
        'brands': {k: {'display': v['display'], 'wikidata': v.get('wikidata'), 'osmCount': v['count']}
                   for k, v in sorted(real.items())},
        'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source': 'OpenStreetMap (Overpass API)',
        'bbox': TORONTO_BBOX,
        'minOsmCount': 2,
        'note': 'Operating names matched UPPERCASE against keys; comparison done in inject_openings.is_auto_chain.',
    }
    CACHE_PATH.write_text(json.dumps(out, indent=1))
    print(f"\nwrote {CACHE_PATH}")
    print(f"  {len(real)} distinct chain brands (≥2 OSM-tagged Toronto locations)")
    print(f"\nTop 25 by OSM coverage:")
    for k, v in sorted(real.items(), key=lambda x: -x[1]['count'])[:25]:
        wd = f"  [{v.get('wikidata') or '-'}]"
        print(f"  {v['count']:>3}× {v['display']:<35}{wd}")

if __name__ == '__main__':
    main()
