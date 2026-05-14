#!/usr/bin/env python3
"""
Batch version of llm_search_recover_cuisine.py — Layer 4 cuisine recovery using
Haiku + web_search submitted via the Message Batches API.

Why batch:
  Sync hits Anthropic's per-org web_search rate limit hard — a single recovery
  run on 2026-05-14 rate-limited 285/371 requests. Batch's per-org limits are
  dramatically higher, plus it's 50% off ($10/1K → $5/1K on web_search).
  Latency is ~hours but that's fine for a daily cron.

Pattern follows tools/llm_verify_batch.py:
  1. Build candidates from web_verify_cache (entries with recovery_note set
     and cuisine still null/unknown; 30-day re-attempt window)
  2. Canary: submit 1 request first to confirm web_search-in-batch works
  3. Submit full batch
  4. Poll until done
  5. Merge results back into web_verify_cache (recovery_source=web_search_batch)

Reads ANTHROPIC_API_KEY from /var/secrets/nowservingto.env.
"""
import os, sys, json, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent
WEB_CACHE_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'
SECRETS = Path('/var/secrets/nowservingto.env')
MODEL = 'claude-haiku-4-5-20251001'
POLL_INTERVAL_SEC = 30
REATTEMPT_DAYS = 30  # don't re-query the same entry sooner than this

# Cuisine taxonomy is the canonical one from cuisines.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cuisines import VALID_CUISINE_KEYS

SYSTEM_PROMPT = """You classify a Toronto restaurant by cuisine using Google web search results.
The restaurant's own website couldn't be read (JS shell, captcha, PDF menu, etc.) so you must
infer cuisine from search snippets, review excerpts, blog posts, and Google Maps listings that
Google has indexed about this place.

You have access to web_search (up to 2 uses). Use Google's search operators aggressively —
they're the difference between thin generic listings and rich menu text.

FIRST SEARCH — a broad probe with the name in quotes + neighborhood/street + Toronto.
Look for cuisine hints in snippets: menu items in reviews, food-blog descriptors, Google
Maps cuisine labels, "best <cuisine> in Toronto" mentions.

SECOND SEARCH — only if the first surfaced no cuisine signal. Pick the highest-leverage
operator combination for the failure mode:
  • Site is a JS shell hiding a PDF menu: `"<NAME>" toronto menu filetype:pdf`
    (Google indexes PDF text; this often returns the full menu when the site can't.)
  • Place is well-known locally: `"<NAME>" (site:blogto.com OR site:reddit.com OR site:nowtoronto.com)`
    Toronto food blogs and r/toronto almost always state the cuisine.
  • Generic short name (e.g. "Mola", "Cuon Cuon"): `"<EXACT NAME>" toronto -yelp -doordash -ubereats`
    (Quotes force exact match; `-` strips aggregator listings with zero cuisine info.)
  • Name + suspected cuisine: `intitle:menu "<NAME>"` or `"<NAME>" toronto (pho OR biryani OR pasta OR <cuisine guess>)`
  • Owner / chef coverage: `"<NAME>" toronto (chef OR opening OR new restaurant) site:torontolife.com OR site:eater.com`

Combine quoted phrases (`"<NAME>"`), `site:`, `filetype:pdf`, `intitle:`, `inurl:`, `OR`,
and `-` exclusions. Always quote the business name on the second search.

Return a single JSON object on ONE line, no prose:
{"cuisine":"<key>","evidence":"<one short sentence with the actual snippet/source>"}

Valid cuisine keys: italian, chinese, japanese, korean, vietnamese, filipino, thai, indonesian,
malaysian, burmese, cambodian, laotian, south_asian, indian, pakistani, afghan, bangladeshi,
tamil, tibetan, sri_lankan, nepalese, caribbean, jamaican, trinidadian, guyanese, haitian,
cuban, dominican, greek, portuguese, polish, french, irish_uk, german, jewish_deli, spanish,
eastern_eu, ukrainian, russian, hungarian, middle_east, lebanese, turkish, syrian, persian,
israeli, egyptian, yemeni, armenian, georgian, latin, mexican, salvadoran, peruvian, colombian,
brazilian, argentinian, venezuelan, african_horn, ethiopian, eritrean, somali, african_west,
nigerian, ghanaian, moroccan, senegalese, unknown.

CRITICAL: Pan-Asian / 3+ regional fusion → unknown.
CRITICAL: American Southern (Cajun, Creole, New Orleans, BBQ, soul) → unknown.
CRITICAL: Packaged-food brand / grocery / chocolatier / distributor / factory outlet → unknown.
CRITICAL: American/Canadian chains (Popeyes, KFC, Boston Pizza, Tim Hortons, etc.) → unknown.
CRITICAL: If search results don't surface menu items, food-blog cuisine descriptors, or a
Google Maps cuisine label — only generic "restaurant" / "open now" / delivery-app listings —
return cuisine=unknown. Don't guess from the name alone."""

def load_api_key():
    if not SECRETS.exists(): sys.exit(f"{SECRETS} missing")
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if line.startswith('ANTHROPIC_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    sys.exit("ANTHROPIC_API_KEY not in secrets")

API_KEY = load_api_key()
HEADERS = {
    'x-api-key': API_KEY,
    'anthropic-version': '2023-06-01',
    'content-type': 'application/json',
}

def http(method, url, data=None):
    body = json.dumps(data).encode('utf-8') if data is not None else None
    req = Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urlopen(req, timeout=120) as r:
            raw = r.read()
            ctype = r.headers.get('Content-Type', '')
            return json.loads(raw) if 'application/json' in ctype else raw
    except HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode('utf-8')[:400]}")
        raise

def needs_search_recovery(entry):
    """Same gating as the sync variant — must have a failed Layer-2 attempt
    (recovery_note set) and still be operating with null/unknown cuisine.
    Re-attempt every 30 days via search_recovered_at."""
    if entry.get('status') != 'ok' or entry.get('operating') != 'yes':
        return False
    c = entry.get('cuisine')
    if c and c != 'unknown':
        return False
    if not entry.get('recovery_note'):
        return False
    sra = entry.get('search_recovered_at')
    if sra:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(sra)).days
            if age < REATTEMPT_DAYS: return False
        except Exception:
            pass
    return True

def build_request(name, address):
    return {
        'params': {
            'model': MODEL,
            'max_tokens': 800,
            'system': SYSTEM_PROMPT,
            'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 2}],
            'messages': [{
                'role': 'user',
                'content': f"Restaurant: {name}\nAddress: {address}\n\nSearch Google and tell me the cuisine.",
            }],
        },
    }

def parse_result_msg(msg):
    """Pull cuisine + evidence from the final text block of a batch result message.
    Returns (cuisine, evidence, n_searches, in_tok, out_tok) — cuisine is None if
    Haiku didn't follow the JSON format, 'unknown' if it explicitly couldn't tell,
    or a valid taxonomy key."""
    usage = msg.get('usage', {})
    server_tool = usage.get('server_tool_use') or {}
    text_blocks = [b.get('text', '') for b in msg.get('content', []) if b.get('type') == 'text']
    text = (text_blocks[-1] if text_blocks else '').strip()
    parsed = None
    for line in text.split('\n'):
        s = line.strip().lstrip('`').strip()
        if s.startswith('{') and s.endswith('}'):
            try: parsed = json.loads(s); break
            except: continue
    if parsed is None:
        try: parsed = json.loads(text)
        except: parsed = {}
    cuisine = parsed.get('cuisine')
    if isinstance(cuisine, str): cuisine = cuisine.strip().lower()
    if cuisine not in VALID_CUISINE_KEYS: cuisine = None
    return (
        cuisine,
        (parsed.get('evidence') or '')[:200],
        server_tool.get('web_search_requests', 0),
        usage.get('input_tokens', 0),
        usage.get('output_tokens', 0),
    )

def submit_batch(requests, label):
    print(f"submitting {label}: {len(requests)} request(s)…")
    r = http('POST', 'https://api.anthropic.com/v1/messages/batches', {'requests': requests})
    print(f"  batch_id={r['id']}  status={r['processing_status']}")
    return r['id']

def poll(batch_id, label):
    t0 = time.time()
    while True:
        time.sleep(POLL_INTERVAL_SEC)
        info = http('GET', f'https://api.anthropic.com/v1/messages/batches/{batch_id}')
        st = info['processing_status']
        cnt = info.get('request_counts', {})
        el = time.time() - t0
        print(f"  [{el:.0f}s] {label} status={st}  counts={cnt}")
        if st == 'ended': return info
        if st in ('cancelling', 'canceled', 'expired'):
            sys.exit(f"{label} ended unexpectedly: {st}")

def download_results(info):
    url = info.get('results_url')
    if not url: sys.exit("no results_url")
    raw = http('GET', url)
    raw_text = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else json.dumps(raw)
    out = []
    for line in raw_text.strip().split('\n'):
        if line.strip(): out.append(json.loads(line))
    return out

def main():
    cache = json.loads(WEB_CACHE_PATH.read_text())
    candidates = [(k, e) for k, e in cache.items() if needs_search_recovery(e)]
    print(f"verify cache entries:              {len(cache)}")
    print(f"needing batched search recovery:   {len(candidates)}")
    if not candidates:
        print("nothing to recover."); return

    # Build a sortable, deterministic custom_id → key map
    id_to_key = {}
    full_requests = []
    for i, (k, e) in enumerate(candidates):
        name = k.split('||')[0]
        address = k.split('||')[1] if '||' in k else ''
        cid = f"r{i:04d}"
        id_to_key[cid] = k
        rec = build_request(name, address)
        rec['custom_id'] = cid
        full_requests.append(rec)

    # ---- Canary: single request to confirm web_search-in-batch works ----
    canary_cid = full_requests[0]['custom_id']
    canary_req = [{'custom_id': 'canary_' + canary_cid, 'params': full_requests[0]['params']}]
    canary_id = submit_batch(canary_req, 'CANARY')
    info = poll(canary_id, 'CANARY')
    results = download_results(info)
    if not results or results[0].get('result', {}).get('type') != 'succeeded':
        sys.exit(f"canary did not succeed: {results}")
    canary_msg = results[0]['result']['message']
    canary_blocks = canary_msg.get('content', [])
    has_search_result = any(b.get('type') == 'web_search_tool_result' for b in canary_blocks)
    print(f"  canary: server_tool_use present={any(b.get('type')=='server_tool_use' for b in canary_blocks)}  web_search_tool_result present={has_search_result}")
    if not has_search_result:
        sys.exit("ABORT: canary returned no web_search_tool_result — web_search likely not supported in Message Batches API for this model. Fall back to sync.")

    # Merge canary result for the matching real key
    now_iso = datetime.now(timezone.utc).isoformat()
    cuisine, evidence, n_searches, in_tok, out_tok = parse_result_msg(canary_msg)
    canary_key = candidates[0][0]
    cache[canary_key]['search_recovered_at'] = now_iso
    if cuisine and cuisine != 'unknown':
        cache[canary_key]['cuisine'] = cuisine
        cache[canary_key]['evidence'] = evidence
        cache[canary_key]['recovery_source'] = 'web_search_batch'
        print(f"  canary recovered: {candidates[0][0].split('||')[0]} → {cuisine}")
    elif cuisine == 'unknown':
        cache[canary_key]['cuisine'] = 'unknown'
        cache[canary_key]['search_recovery_note'] = evidence[:120] or 'search recovery — still unknown'
        print(f"  canary verdict: {candidates[0][0].split('||')[0]} → unknown (expected for non-restaurants)")
    else:
        cache[canary_key]['search_recovery_note'] = 'canary parse_failed'
    WEB_CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))

    # ---- Full batch ----
    full_id = submit_batch(full_requests, 'FULL')
    info = poll(full_id, 'FULL')
    results = download_results(info)

    n_recovered = n_unknown = n_parse_fail = n_err = 0
    tot_in = tot_out = tot_search = 0
    for obj in results:
        cid = obj.get('custom_id')
        key = id_to_key.get(cid)
        if not key: continue
        result = obj.get('result', {})
        # Always stamp search_recovered_at so we don't re-attempt before the 30-day window
        cache[key]['search_recovered_at'] = now_iso
        if result.get('type') != 'succeeded':
            n_err += 1
            cache[key]['search_recovery_note'] = f"batch {result.get('type')}"
            continue
        cuisine, evidence, ns, in_tok, out_tok = parse_result_msg(result['message'])
        tot_in += in_tok; tot_out += out_tok; tot_search += ns
        if cuisine and cuisine != 'unknown':
            n_recovered += 1
            cache[key]['cuisine'] = cuisine
            cache[key]['evidence'] = evidence
            cache[key]['recovery_source'] = 'web_search_batch'
        elif cuisine == 'unknown':
            n_unknown += 1
            cache[key]['cuisine'] = 'unknown'
            cache[key]['search_recovery_note'] = evidence[:120] or 'search recovery — still unknown'
        else:
            n_parse_fail += 1
            cache[key]['search_recovery_note'] = 'parse_failed'

    WEB_CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    # Batch cost: web_search at $5/1K (50% off sync), tokens roughly $0.40/M input + $2/M output for Haiku at batch rates
    cost = tot_search * 0.005 + tot_in * 0.0000004 + tot_out * 0.000002
    print(f"\nDone: recovered={n_recovered}  unknown={n_unknown}  parse_failed={n_parse_fail}  errors={n_err}")
    print(f"  web_search calls: {tot_search}  in_tokens: {tot_in:,}  out_tokens: {tot_out:,}")
    print(f"  approx cost: ${cost:.2f}")

if __name__ == '__main__':
    main()
