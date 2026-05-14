#!/usr/bin/env python3
"""
One-shot: for entries currently surfacing via the name-only fallback path
(verify cuisine is null/unknown but llm cache has a guess), re-fetch Google
Places to populate the new `reviews` + `editorialSummary` fields, then clear
their `recovered_at` so the next llm_recover_cuisine.py run picks them up
with the richer evidence.

Why: tightened-prompt name-only fallback recovered 167 entries with ~13%
error rate. Layer 2 with Places reviews + editorial as additional evidence
can correct most of those errors, since reviews carry cultural markers
("their kunafa is the best" / "authentic Salvadoran pupusas") that name
alone misses.

Cost: ~175 Places calls (Atmosphere Data SKU — within 10K/month free tier
on our account at ~700 lookups/month total) + free cache writes.
"""
import json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich_places import enrich_one, CACHE_PATH as PLACES_CACHE_PATH

WEB_VERIFY_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'
LLM_CACHE_PATH = ROOT / 'tools' / 'cache' / 'llm_cuisine_cache.json'

def main():
    wv = json.loads(WEB_VERIFY_PATH.read_text())
    llm = json.loads(LLM_CACHE_PATH.read_text())
    places = json.loads(PLACES_CACHE_PATH.read_text()) if PLACES_CACHE_PATH.exists() else {}

    # Identify the fallback set: verify cuisine null/unknown, llm has a real guess
    targets = []
    for k, ve in wv.items():
        if ve.get('status') != 'ok' or ve.get('operating') != 'yes': continue
        vc = ve.get('cuisine')
        if vc and vc != 'unknown': continue
        le = llm.get(k) or {}
        lc = le.get('cuisine')
        if not lc or lc == 'unknown': continue
        targets.append(k)

    print(f"Fallback-tagged entries to re-evaluate: {len(targets)}")
    print(f"  estimated Places re-fetches: {len(targets)}")
    print(f"  free tier coverage: well within 10K/month — $0 expected")
    if not targets:
        return

    t0 = time.time()
    ok = err = no_places = 0
    for i, k in enumerate(targets, 1):
        name, _, addr = k.partition('||')
        try:
            r = enrich_one(name, addr)
            places[k] = r  # overwrite with fresh data including reviews/editorial
            if r.get('status') == 'ok':
                ok += 1
            else:
                no_places += 1
        except Exception as ex:
            err += 1
            print(f"  ERR on {name}: {type(ex).__name__}: {str(ex)[:80]}")

        # Clear recovered_at so llm_recover_cuisine.py is eligible to re-run on it
        if k in wv:
            wv[k].pop('recovered_at', None)
            wv[k].pop('recovery_note', None)
            wv[k].pop('recovery_source', None)
            wv[k].pop('search_recovered_at', None)
            wv[k].pop('search_recovery_note', None)

        if i % 20 == 0 or i == len(targets):
            el = time.time() - t0
            print(f"  [{i}/{len(targets)}] {el:.0f}s  fresh-ok={ok}  no-places-match={no_places}  err={err}")
            PLACES_CACHE_PATH.write_text(json.dumps(places, separators=(',', ':')))
            WEB_VERIFY_PATH.write_text(json.dumps(wv, separators=(',', ':')))
        time.sleep(0.15)

    PLACES_CACHE_PATH.write_text(json.dumps(places, separators=(',', ':')))
    WEB_VERIFY_PATH.write_text(json.dumps(wv, separators=(',', ':')))
    print(f"\nDone in {time.time()-t0:.0f}s: refreshed {ok} Places entries, {no_places} had no Places match, {err} errors")
    print(f"Next step: .venv/bin/python tools/llm_recover_cuisine.py")
    print(f"  Layer 2 will now reclassify these {ok} entries using:")
    print(f"    • Their website (when fetchable)")
    print(f"    • Google Places editorialSummary")
    print(f"    • Up to 5 Google reviews per place — the new cultural-marker source")

if __name__ == '__main__':
    main()
