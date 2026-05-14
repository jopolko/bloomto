#!/usr/bin/env python3
"""
Targeted Places enrichment for entries whose verified website is on a social
platform. Goal: replace Instagram/Facebook links with proper Google Maps profile
URLs (or the restaurant's own website if Places knows it).

Reports an explicit before/after breakdown so we know exactly what got fixed.

Cost: ~$0.017 per Places lookup. Run after each major web-verify pass that
leaves entries on social.
"""
import sys, time, json
from pathlib import Path
sys.path.insert(0, '/home/josh/nowservingto/tools')
from enrich_places import enrich_one, CACHE_PATH

ROOT = Path(__file__).resolve().parent.parent
WEB_VERIFY_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'

SOCIAL_DOMAINS = ('instagram.com', 'facebook.com', 'tiktok.com')

def is_social(url):
    return bool(url) and any(d in url.lower() for d in SOCIAL_DOMAINS)

def main():
    wv = json.loads(WEB_VERIFY_PATH.read_text())
    places = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}

    targets = [(k, e) for k, e in wv.items()
               if e.get('status') == 'ok'
               and e.get('operating') == 'yes'
               and is_social(e.get('website'))]
    print(f"social-link entries to address: {len(targets)}\n")

    # Identify entries already covered by Places — those should already be using
    # the Places URL in the rendered page; if not, the lookup key may differ.
    already_covered = sum(
        1 for k, e in targets
        if places.get(k) and places[k].get('status') == 'ok'
        and places[k].get('businessStatus') == 'OPERATIONAL'
    )
    to_enrich = [(k, e) for k, e in targets if k not in places]
    print(f"  in Places already (OPERATIONAL): {already_covered}")
    print(f"  NEW Places lookups to run:       {len(to_enrich)}")
    print(f"  estimated cost:                  ${len(to_enrich)*0.017:.2f}\n")

    n_op = n_closed = n_missing = n_err = 0
    t0 = time.time()
    for i, (k, e) in enumerate(to_enrich, 1):
        name, _, addr = k.partition('||')
        try:
            r = enrich_one(name, addr)
            places[k] = r
            if r.get('status') == 'ok':
                bs = r.get('businessStatus')
                if bs == 'OPERATIONAL': n_op += 1
                elif bs in ('CLOSED_PERMANENTLY', 'CLOSED_TEMPORARILY'): n_closed += 1
                else: n_missing += 1
            else:
                n_missing += 1
        except Exception as ex:
            print(f"  ERROR on {name}: {ex}")
            n_err += 1
        if i % 10 == 0 or i == len(to_enrich):
            print(f"  [{i}/{len(to_enrich)}] {time.time()-t0:.0f}s  operational={n_op} closed={n_closed} not-found={n_missing} err={n_err}")
            CACHE_PATH.write_text(json.dumps(places, separators=(',', ':')))
        time.sleep(0.21)
    CACHE_PATH.write_text(json.dumps(places, separators=(',', ':')))

    print(f"\n=== Results ===")
    print(f"  Places found OPERATIONAL: {n_op}   ← these will upgrade away from social on next inject")
    print(f"  Places says closed:       {n_closed}  ← these will DROP from the feed")
    print(f"  Places couldn't find:     {n_missing}  ← these stay on social (no Google profile exists)")
    print(f"  errors:                   {n_err}")
    print(f"  cost: ~${(n_op + n_closed + n_missing + n_err) * 0.017:.2f}")
    print(f"\nRun: .venv/bin/python tools/inject_openings.py")
    print(f"Then check how many social links remain.")

if __name__ == '__main__':
    main()
