#!/usr/bin/env python3
"""
Experimental: re-run Layer 4 web_search batch on entries currently stuck at
cuisine='unknown', with a STRICTER prompt that FORCES Haiku to use two searches
and operator-heavy queries (filetype:pdf, site:blogto.com etc.) on the second.

Hypothesis: today's batch had ~67 entries return 'unknown'. We don't know how
many of those would have been recovered if Haiku had been forced to try a PDF
or site-restricted second search instead of giving up after the first generic
search returned nothing useful.

Read-only on llm_cuisine_cache; writes to web_verify_cache only on cuisine flip.
Cost: ~67 entries × ~$0.02 = $1.50.
"""
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_search_recover_batch import (
    MODEL, build_request as _orig_build_request, parse_result_msg,
    submit_batch, poll, download_results, http,
)

ROOT = Path(__file__).resolve().parent.parent
WEB_CACHE_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'

# Aggressive variant: MANDATES the second search and operator-style queries.
SYSTEM_PROMPT_FORCED = """You classify a Toronto restaurant by cuisine using Google web search results.
The restaurant's own website couldn't be read (JS shell, captcha, PDF menu, etc.) so you must
infer cuisine from search snippets, blog posts, Google Maps listings, and any PDF menus that
Google has indexed about this place.

You have access to web_search and MUST use BOTH searches available — even if the first feels
sufficient. The second search MUST use a different operator combination than the first,
chosen from:
  • `"<NAME>" toronto menu filetype:pdf`  (Google indexes PDF text; this often returns the
    full menu when the site is a JS shell)
  • `"<NAME>" (site:blogto.com OR site:reddit.com OR site:nowtoronto.com OR site:toronto.com)`
    (Toronto food blogs and r/toronto almost always state cuisine in their posts)
  • `intitle:menu "<NAME>"` OR `inurl:menu "<NAME>" toronto`
  • `"<NAME>" toronto -yelp -doordash -ubereats -skipthedishes -tripadvisor`
    (Strips aggregator listings with zero cuisine info; quoted exact name forces match)
  • `"<NAME>" toronto (chef OR opening OR new restaurant) site:torontolife.com OR site:eater.com`

Always quote the business name with `"…"` on the second search. Pick the operator combo that's
most likely to unlock the failure mode (PDF menus for sparse-site cases, food-blog domain
restrictions for well-known places, exclusion-style for places drowning in aggregator listings).

Return a single JSON object on ONE line, no prose:
{"cuisines":["<key1>","<key2>"],"evidence":"<one short sentence with the actual snippet/source>"}

Valid cuisine keys: italian, chinese, japanese, korean, vietnamese, filipino, thai, indonesian,
malaysian, burmese, cambodian, laotian, south_asian, indian, pakistani, afghan, bangladeshi,
tamil, tibetan, sri_lankan, nepalese, caribbean, jamaican, trinidadian, guyanese, haitian,
cuban, dominican, greek, portuguese, polish, french, irish_uk, german, jewish_deli, spanish,
eastern_eu, ukrainian, russian, hungarian, middle_east, lebanese, turkish, syrian, persian,
israeli, egyptian, yemeni, armenian, georgian, latin, mexican, salvadoran, peruvian, colombian,
brazilian, argentinian, venezuelan, african_horn, ethiopian, eritrean, somali, african_west,
nigerian, ghanaian, moroccan, senegalese, unknown.

PREFER specific country buckets. Use ["south_asian"] / ["middle_east"] / ["caribbean"] / ["latin"]
ONLY when no specific country is stated. Multi-country menus: list each (e.g. Afghan + Pakistani
+ Indian = ["afghan","pakistani","indian"]).

CRITICAL: Pan-Asian fusion / American Southern / packaged-food brand / Canadian chain → ["unknown"].
Only return ["unknown"] AFTER you've done BOTH searches and neither surfaced cuisine signal."""

def build_request(name, address):
    return {
        'params': {
            'model': MODEL,
            'max_tokens': 800,
            'system': SYSTEM_PROMPT_FORCED,
            'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 2}],
            'messages': [{
                'role': 'user',
                'content': f"Restaurant: {name}\nAddress: {address}\n\nSearch Google TWICE and tell me the cuisine.",
            }],
        },
    }

def main():
    cache = json.loads(WEB_CACHE_PATH.read_text())
    # Targets: entries where today's batched Layer 4 stamped 'unknown' (the hard cases)
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    targets = []
    for k, e in cache.items():
        sra = e.get('search_recovered_at', '')
        if today not in sra: continue
        # Currently 'unknown' from today's batch
        if e.get('cuisine') == 'unknown' and e.get('recovery_source') != 'web_search_batch':
            targets.append(k)
    # Cap for safety
    MAX_TEST = 70
    if len(targets) > MAX_TEST:
        print(f'Capping {len(targets)} → {MAX_TEST} for cost control')
        targets = targets[:MAX_TEST]
    print(f"Force-PDF retry targets: {len(targets)}")
    print(f"  estimated cost: ${len(targets)*0.02:.2f}")
    if not targets: return

    id_to_key = {}
    requests = []
    for i, k in enumerate(targets):
        name, _, address = k.partition('||')
        cid = f"f{i:04d}"
        id_to_key[cid] = k
        rec = build_request(name, address)
        rec['custom_id'] = cid
        requests.append(rec)

    full_id = submit_batch(requests, 'FORCE-PDF')
    info = poll(full_id, 'FORCE-PDF')
    results = download_results(info)

    n_flipped = n_still_unknown = n_err = 0
    tot_searches = 0
    flips = []
    for obj in results:
        cid = obj.get('custom_id')
        key = id_to_key.get(cid)
        if not key: continue
        res = obj.get('result', {})
        if res.get('type') != 'succeeded':
            n_err += 1
            continue
        cuisines, evidence, ns, _, _ = parse_result_msg(res['message'])
        tot_searches += ns
        real = [c for c in cuisines if c and c != 'unknown']
        if real:
            n_flipped += 1
            flips.append((key.split('||')[0], real, evidence[:80]))
            # Apply: write back to cache
            cache[key]['cuisine'] = real[0]
            cache[key]['cuisines'] = real
            cache[key]['evidence'] = evidence
            cache[key]['recovery_source'] = 'web_search_force_pdf'
            cache[key].pop('search_recovery_note', None)
        else:
            n_still_unknown += 1

    WEB_CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    print(f"\n=== Force-PDF results ===")
    print(f"  flipped to cuisine:    {n_flipped}")
    print(f"  still unknown:         {n_still_unknown}")
    print(f"  errors:                {n_err}")
    print(f"  total web_searches:    {tot_searches}  (target: ~{len(targets)*2})")
    print()
    print('=== Flips (newly recovered entries) ===')
    for name, cs, ev in flips:
        print(f'  {name[:38]:<38}  →  {cs}  | {ev}')

if __name__ == '__main__':
    main()
