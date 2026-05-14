#!/usr/bin/env python3
"""
Measurement-only: re-classify the entries where web_verify produced a definitive
cuisine, using the CURRENT prompt in llm_classify_batch.py. Compare results to
the verifier's ground truth and print the error rate.

This is what we use to decide whether the tightened prompt is good enough to
restore the conditional fallback rule in inject_openings.py (i.e., trust
name-only cuisine when web_verify ran but couldn't pin one).

Read-only — does NOT modify llm_cuisine_cache. Uses the batch API for cost +
rate-limit reasons.

Cost: ~$0.20-0.40 for 389 entries on Haiku batch.
"""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Re-use the production classify prompt + valid-keys (the thing under test)
# from llm_classify_batch, and the shared batch HTTP helpers from
# llm_verify_batch (which has them modularized as standalone functions —
# llm_classify_batch's are inline in main()).
from llm_classify_batch import SYSTEM_PROMPT, MODEL, VALID_KEYS
from llm_verify_batch import submit_batch, poll, download_results

ROOT = Path(__file__).resolve().parent.parent
WEB_CACHE_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'

def build_request(name, address):
    """Mirrors build_request in llm_classify_batch but with the address+name
    combined into a single user message, same as production."""
    return {
        'params': {
            'model': MODEL,
            'max_tokens': 20,
            'system': SYSTEM_PROMPT,
            'messages': [{
                'role': 'user',
                'content': f"Operating Name: {name}\nAddress: {address}\n\nCuisine key:",
            }],
        },
    }

def parse_cuisine(msg):
    text_blocks = [b.get('text', '') for b in msg.get('content', []) if b.get('type') == 'text']
    raw = (text_blocks[-1] if text_blocks else '').strip().lower()
    # Take first word/token
    token = raw.split()[0].strip(",.;:'\"`") if raw else ''
    return token if token in VALID_KEYS else 'unknown'

def main():
    wv = json.loads(WEB_CACHE_PATH.read_text())
    # Ground truth = entries where web_verify ran AND produced a real cuisine
    # (not null, not 'unknown'). This is the pool we'll measure name-only against.
    gt = [(k, e.get('cuisine')) for k, e in wv.items()
          if e.get('status') == 'ok' and e.get('operating') == 'yes'
          and e.get('cuisine') and e.get('cuisine') != 'unknown']
    print(f"Ground-truth pool: {len(gt)} entries")
    if not gt:
        sys.exit("No ground truth available — verify cache is empty?")

    id_to = {}
    requests = []
    for i, (k, true_cuisine) in enumerate(gt):
        name, _, address = k.partition('||')
        cid = f"m{i:04d}"
        id_to[cid] = (k, name, true_cuisine)
        rec = build_request(name, address)
        rec['custom_id'] = cid
        requests.append(rec)

    batch_id = submit_batch(requests, 'MEASURE')
    info = poll(batch_id, 'MEASURE')
    results = download_results(info)
    print(f"results landed: {len(results)}")

    agree = 0
    disagree = []
    parse_fail = 0
    umbrella_to_specific = 0   # GT was umbrella, classifier said specific (false positive)
    specific_to_umbrella = 0   # GT was specific, classifier said umbrella (recoverable miss)

    UMBRELLAS = {'middle_east', 'south_asian', 'caribbean', 'latin',
                 'african_horn', 'african_west', 'eastern_eu'}
    UMBRELLA_FAMILY = {
        'middle_east': {'lebanese','turkish','syrian','persian','egyptian','israeli','yemeni','armenian','georgian'},
        'south_asian': {'indian','pakistani','afghan','bangladeshi','tamil','tibetan','sri_lankan','nepalese'},
        'caribbean':   {'jamaican','trinidadian','guyanese','haitian','cuban','dominican'},
        'latin':       {'mexican','salvadoran','peruvian','colombian','brazilian','argentinian','venezuelan'},
        'african_horn':{'ethiopian','eritrean','somali'},
        'african_west':{'nigerian','ghanaian','moroccan','senegalese'},
        'eastern_eu':  {'ukrainian','russian','hungarian','polish'},
    }
    def in_same_family(gt_c, pred_c):
        # Is pred under the umbrella of gt? Or vice versa?
        if gt_c in UMBRELLAS and pred_c in UMBRELLA_FAMILY.get(gt_c, set()): return True
        if pred_c in UMBRELLAS and gt_c in UMBRELLA_FAMILY.get(pred_c, set()): return True
        return False

    for r in results:
        cid = r.get('custom_id')
        meta = id_to.get(cid)
        if not meta: continue
        key, name, true_c = meta
        res = r.get('result', {})
        if res.get('type') != 'succeeded':
            parse_fail += 1
            continue
        pred = parse_cuisine(res['message'])
        if pred == true_c:
            agree += 1
        elif pred == 'unknown':
            # classifier abstained on a verified-cuisine entry — not "wrong",
            # just unhelpful. Treat as miss for recovery purposes.
            disagree.append((name, pred, true_c, 'abstained'))
        elif in_same_family(true_c, pred):
            # Cross-umbrella agreement: e.g., GT=lebanese, pred=middle_east → not strictly wrong
            if pred in UMBRELLAS and true_c not in UMBRELLAS:
                specific_to_umbrella += 1
            elif true_c in UMBRELLAS and pred not in UMBRELLAS:
                umbrella_to_specific += 1
            disagree.append((name, pred, true_c, 'umbrella-relation'))
        else:
            disagree.append((name, pred, true_c, 'WRONG'))

    total = len(results) - parse_fail
    wrong = [d for d in disagree if d[3] == 'WRONG']
    print(f"\nResults vs ground truth ({total} measured):")
    print(f"  AGREE (exact match):                 {agree}     ({agree*100//max(total,1)}%)")
    print(f"  abstained as unknown:                {sum(1 for d in disagree if d[3] == 'abstained')}")
    print(f"  umbrella-family agreement:           {sum(1 for d in disagree if d[3] == 'umbrella-relation')}")
    print(f"    of which spec→umbrella (safe):     {specific_to_umbrella}")
    print(f"    of which umbrella→spec (BAD):      {umbrella_to_specific}")
    print(f"  WRONG (different cuisine entirely):  {len(wrong)}    ({len(wrong)*100//max(total,1)}%)")
    print(f"  parse_fail:                          {parse_fail}")
    print()
    print('--- First 25 WRONG predictions ---')
    for name, pred, true_c, _ in wrong[:25]:
        print(f'  {name[:36]:<36}  predicted {pred:<14}  actual {true_c}')

if __name__ == '__main__':
    main()
