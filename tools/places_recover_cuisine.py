#!/usr/bin/env python3
"""
Expand Google Places coverage for entries that don't yet have an own-website.

Why this exists:
  `llm_recover_cuisine.py` prefers `places_cache[k].website` over the verify
  cache's website when classifying cuisine — Places frequently knows the real
  restaurant URL even when our verifier was only able to find the operator's
  Instagram. But `llm_recover_cuisine` can only USE Places data if it's already
  cached. `places_enrich_socials.py` covers entries whose verify-website is on
  IG/FB/TikTok, but it ignores entries with no website at all. This script
  fills that hole.

  (An earlier version of this script tried to map Places' `types` field to our
  cuisine taxonomy directly. A full sweep recovered 0/496 cuisines because
  Google tags most Toronto restaurants with generic types like `restaurant`,
  `food`, `establishment` — not `italian_restaurant` etc. We dropped that
  approach in favour of fetching the real own-website Places knows about.)

Cost: ~$0.017 per Places lookup. Daily delta is ~2-5 entries → pennies.
"""
import sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich_places import enrich_one, CACHE_PATH as PLACES_CACHE_PATH

ROOT = Path(__file__).resolve().parent.parent
WEB_VERIFY_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'

SOCIAL_DOMAINS = ('instagram.com', 'facebook.com', 'tiktok.com')

def _is_social(u): return bool(u) and any(d in u.lower() for d in SOCIAL_DOMAINS)

def needs_lookup(verify_entry, places_entry):
    """Target: any operating entry not yet in places_cache. Previously gated
    on `cuisine is null/unknown`, but that skipped entries with weak name-
    only LLM guesses (e.g. OGUZ UYGHUR POLOV → tibetan), which then never
    got Places data + dropped at the validator. Dropping the cuisine check
    means any fresh operating=yes entry gets a Places shot — Places data
    is the most authoritative signal we have."""
    if verify_entry.get('status') != 'ok' or verify_entry.get('operating') != 'yes':
        return False
    if places_entry:  # already queried (status doesn't matter — don't pay twice)
        return False
    return True

def main():
    verify_cache = json.loads(WEB_VERIFY_PATH.read_text())
    places_cache = json.loads(PLACES_CACHE_PATH.read_text()) if PLACES_CACHE_PATH.exists() else {}

    targets = [(k, e) for k, e in verify_cache.items()
               if needs_lookup(e, places_cache.get(k))]
    print(f"Places lookups needed (cuisine=null, no Places cache yet): {len(targets)}")
    print(f"  estimated cost: ${len(targets)*0.017:.2f}")
    if not targets:
        return

    n_with_site = n_no_site = n_closed = n_err = 0
    t0 = time.time()
    for i, (k, e) in enumerate(targets, 1):
        name, _, addr = k.partition('||')
        try:
            r = enrich_one(name, addr)
            places_cache[k] = r
            if r.get('status') != 'ok':
                n_err += 1
            elif r.get('businessStatus') and r['businessStatus'] != 'OPERATIONAL':
                n_closed += 1
            else:
                w = r.get('website')
                if w and not _is_social(w) and 'maps.google.' not in w.lower():
                    n_with_site += 1
                else:
                    n_no_site += 1
        except Exception as ex:
            n_err += 1
            print(f'  ERR on {name}: {type(ex).__name__}: {str(ex)[:80]}')
        if i % 25 == 0 or i == len(targets):
            el = time.time() - t0
            print(f'  [{i}/{len(targets)}] {el:.0f}s  has-real-site={n_with_site}  no-site={n_no_site}  closed={n_closed}  err={n_err}')
            PLACES_CACHE_PATH.write_text(json.dumps(places_cache, separators=(',', ':')))
        time.sleep(0.21)
    PLACES_CACHE_PATH.write_text(json.dumps(places_cache, separators=(',', ':')))
    print(f'\nDone: {n_with_site} entries now have an own-website to feed into llm_recover_cuisine,')
    print(f'      {n_no_site} only have a Maps URL, {n_closed} closed, {n_err} not-found / errors')
    print(f'Total cost: ~${(n_with_site + n_no_site + n_closed + n_err) * 0.017:.2f}')

if __name__ == '__main__':
    main()
