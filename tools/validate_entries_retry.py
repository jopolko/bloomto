#!/usr/bin/env python3
"""Retry the validator on entries that didn't get validated_at stamped in the
first pass. Bumps max_tokens from 300 to 800 to catch responses that were
truncated mid-JSON. Same prompt + parsing as validate_entries_batch.py."""
import json, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_entries_batch import (
    SYSTEM_PROMPT, build_request, parse_result,
    WEB_VERIFY_PATH, PLACES_PATH, URL_HEALTH_PATH,
)
from llm_verify_batch import submit_batch, poll, download_results

def main():
    wv = json.loads(WEB_VERIFY_PATH.read_text())
    pc = json.loads(PLACES_PATH.read_text()) if PLACES_PATH.exists() else {}
    health = json.loads(URL_HEALTH_PATH.read_text()) if URL_HEALTH_PATH.exists() else {}
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    targets = []
    for k, e in wv.items():
        if e.get('status') != 'ok' or e.get('operating') != 'yes': continue
        if today in (e.get('validated_at') or ''): continue
        targets.append(k)
    print(f"Retry targets (unvalidated): {len(targets)}  est cost ~${len(targets)*0.0015:.2f}")
    if not targets: return

    id_to_key = {}
    requests = []
    for i, k in enumerate(targets):
        cid = f"r{i:04d}"
        id_to_key[cid] = k
        rec = build_request(k, wv[k], pc.get(k))
        rec['params']['max_tokens'] = 800  # was 300; some responses got truncated
        rec['custom_id'] = cid
        requests.append(rec)

    full_id = submit_batch(requests, 'VALIDATE-RETRY')
    info = poll(full_id, 'VALIDATE-RETRY')
    results = download_results(info)

    n_ok = n_fail = 0
    n_same_no = n_isr_no = n_cuisine_changed = n_website_dropped = n_website_changed = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for obj in results:
        cid = obj.get('custom_id')
        key = id_to_key.get(cid)
        if not key: continue
        res = obj.get('result', {})
        if res.get('type') != 'succeeded':
            n_fail += 1
            continue
        parsed = parse_result(res['message'])
        if parsed is None:
            n_fail += 1
            continue
        n_ok += 1
        if parsed['is_same_business'] == 'no':
            n_same_no += 1
            if key in pc: pc.pop(key)
        if parsed['is_restaurant'] == 'no':
            n_isr_no += 1
            wv[key]['validator_drop'] = 'not-restaurant'
            wv[key]['validator_evidence'] = parsed['evidence']
        else:
            wv[key].pop('validator_drop', None)

        real_cuisines = [c for c in parsed['cuisines'] if c and c != 'unknown']
        current = wv[key].get('cuisines') or ([wv[key].get('cuisine')] if wv[key].get('cuisine') else [])
        current = [c for c in current if c and c != 'unknown']
        if real_cuisines and set(real_cuisines) != set(current):
            n_cuisine_changed += 1
            wv[key]['cuisine'] = real_cuisines[0]
            wv[key]['cuisines'] = real_cuisines
            wv[key]['evidence'] = parsed['evidence']
            wv[key]['recovery_source'] = 'unified_validator'

        cur_website = wv[key].get('website') or ''
        places_website = (pc.get(key) or {}).get('website') if pc.get(key) and pc[key].get('status') == 'ok' else None
        if parsed['best_website'] is None:
            for u in (cur_website, places_website):
                if u and u.startswith(('http://', 'https://')):
                    health[u] = {'status': None, 'checked_at': now_iso,
                                 'ok': False, 'reason': 'validator: aggregator wrapper or dead'}
            if cur_website: n_website_dropped += 1
        elif parsed['best_website'] and parsed['best_website'] != cur_website:
            n_website_changed += 1
            wv[key]['website'] = parsed['best_website']

        wv[key]['validated_at'] = now_iso

    WEB_VERIFY_PATH.write_text(json.dumps(wv, separators=(',', ':')))
    PLACES_PATH.write_text(json.dumps(pc, separators=(',', ':')))
    URL_HEALTH_PATH.write_text(json.dumps(health, separators=(',', ':')))

    print(f"\n=== Retry results ===")
    print(f"  parsed OK:                     {n_ok}")
    print(f"  parse failed again:            {n_fail}  (will need third pass if material)")
    print(f"  is_same_business = no:         {n_same_no}")
    print(f"  is_restaurant = no:            {n_isr_no}")
    print(f"  cuisine changed:               {n_cuisine_changed}")
    print(f"  website dropped:               {n_website_dropped}")
    print(f"  website changed:               {n_website_changed}")

if __name__ == '__main__':
    main()
