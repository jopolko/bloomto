#!/usr/bin/env python3
"""
One-shot: refresh every umbrella-tagged entry's Places data + re-classify via
Layer 2. Targets the cases like La Rumba where the broad bucket (caribbean,
latin, south_asian, middle_east, african_horn, african_west, eastern_eu) hides
a more specific country that the Places editorialSummary or reviews would have
revealed — IF that field had existed in our local cache.

Re-fetches Places (free at our scale, populates editorialSummary + reviews),
then runs the same Layer 2 classifier we already use, which now reads:
  website content + Places editorial + Places reviews + the new multi-cuisine prompt.

Expected outcome: most umbrellas split into specific country buckets (matching
the user's "no broad umbrella" preference). Some stay umbrella when truly
multi-region; some flip to ["unknown"] when re-evaluated with stronger
evidence (rare but possible for non-restaurants).

Cost: ~100 × $0.005 = ~$0.50 total. Free Places lookups within free tier.
"""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from enrich_places import enrich_one, CACHE_PATH as PLACES_PATH
from llm_recover_cuisine import classify_one
from concurrent.futures import ThreadPoolExecutor, as_completed

WEB_VERIFY_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'
UMBRELLAS = {'caribbean', 'latin', 'south_asian', 'middle_east',
             'african_horn', 'african_west', 'eastern_eu'}
WORKERS = 4

def main():
    wv = json.loads(WEB_VERIFY_PATH.read_text())
    pc = json.loads(PLACES_PATH.read_text()) if PLACES_PATH.exists() else {}

    targets = []
    for k, e in wv.items():
        if e.get('status') != 'ok' or e.get('operating') != 'yes': continue
        if e.get('cuisine') not in UMBRELLAS: continue
        targets.append(k)
    print(f"Umbrella-tagged entries to refresh: {len(targets)}")
    print(f"  estimated cost: ~${len(targets) * 0.005:.2f}")
    if not targets: return

    # Phase 1: re-fetch Places for all targets — populates editorialSummary + reviews.
    # Sequential because Places has its own rate limits (modest at our scale).
    print()
    print("=== Phase 1: refresh Places ===")
    t0 = time.time()
    for i, k in enumerate(targets, 1):
        name, _, addr = k.partition('||')
        try:
            pc[k] = enrich_one(name, addr)
        except Exception as ex:
            print(f"  ERR refetch {name}: {type(ex).__name__}: {str(ex)[:80]}")
        if i % 20 == 0:
            print(f"  [{i}/{len(targets)}] {time.time()-t0:.0f}s")
            PLACES_PATH.write_text(json.dumps(pc, separators=(',', ':')))
        time.sleep(0.15)
    PLACES_PATH.write_text(json.dumps(pc, separators=(',', ':')))
    print(f"  refresh done in {time.time()-t0:.0f}s")

    # Phase 2: re-classify each via Layer 2 — uses website + Places extras + new prompt.
    print()
    print("=== Phase 2: re-classify via Layer 2 ===")
    n_split_to_specific = n_stayed_umbrella = n_flipped_unknown = n_err = 0
    flips = []

    def work(key):
        name, _, addr = key.partition('||')
        try:
            p = pc.get(key) or {}
            p_reviews = p.get('reviews') if p.get('status') == 'ok' else None
            p_editorial = p.get('editorialSummary') if p.get('status') == 'ok' else None
            p_web = p.get('website') if p.get('status') == 'ok' else None
            # Try to fetch website content too if Places knows one (skip for non-fetchable)
            text = None
            if p_web and not any(d in p_web.lower() for d in ('instagram', 'facebook', 'tiktok', 'maps.google', 'goo.gl/maps')):
                try:
                    from llm_recover_cuisine import fetch_page_text
                    text, _ = fetch_page_text(p_web)
                except Exception: pass
            if not text and not p_reviews and not p_editorial:
                return key, None, None, 'no usable evidence'
            cuisines, evidence = classify_one(name, addr, text, p_reviews, p_editorial)
            return key, cuisines, evidence, None
        except Exception as ex:
            return key, None, None, f'{type(ex).__name__}: {str(ex)[:60]}'

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(work, k): k for k in targets}
        for i, fut in enumerate(as_completed(futures), 1):
            key, cuisines, evidence, err = fut.result()
            if err:
                n_err += 1
                continue
            real = [c for c in (cuisines or []) if c and c != 'unknown']
            old_cuisine = wv[key].get('cuisine')
            if cuisines == ['unknown']:
                # Layer 2 examined richer evidence and decided: not classifiable.
                # Keep the existing umbrella tag — the fallback rule will still
                # surface it. (Could be a non-restaurant we'd want to drop, but
                # that's a separate decision.)
                n_flipped_unknown += 1
                continue
            if not real:
                n_err += 1
                continue
            # Did we split out of the umbrella?
            if real[0] != old_cuisine or len(real) > 1:
                # Check whether it actually moved to a more specific bucket
                if real[0] not in UMBRELLAS or len(real) > 1:
                    n_split_to_specific += 1
                    flips.append((key.split('||')[0], old_cuisine, real, evidence[:60]))
                    wv[key]['cuisine'] = real[0]
                    wv[key]['cuisines'] = real
                    wv[key]['evidence'] = evidence
                    wv[key]['recovery_source'] = 'places_extras_refresh'
                else:
                    n_stayed_umbrella += 1
            else:
                n_stayed_umbrella += 1
            if i % 20 == 0:
                print(f"  [{i}/{len(targets)}] {time.time()-t0:.0f}s  split={n_split_to_specific} stayed={n_stayed_umbrella}")

    WEB_VERIFY_PATH.write_text(json.dumps(wv, separators=(',', ':')))
    print(f"\n=== Results ===")
    print(f"  split umbrella → specific: {n_split_to_specific}")
    print(f"  stayed umbrella:           {n_stayed_umbrella}  (legitimately multi-region or evidence too generic)")
    print(f"  flipped to unknown:        {n_flipped_unknown}")
    print(f"  errors:                    {n_err}")
    print()
    print("=== Notable splits ===")
    for name, old, new, ev in flips[:30]:
        print(f"  {name[:35]:<35}  {old:<14} → {new}  | {ev}")

if __name__ == '__main__':
    main()
