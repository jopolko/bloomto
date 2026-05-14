#!/usr/bin/env python3
"""
Batch version of llm_verify.py. Submits a Message Batches job for entries
needing web_search verification (or re-verification).

50% off, much higher rate limits than sync. Polls until done, merges into
tools/cache/web_verify_cache.json.

Auto-canary: submits 1 request first to confirm web_search-in-batch works.
If the canary returns clean web_search_tool_result content, proceeds with
the full batch. Otherwise fails fast with a clear error.

Reads ANTHROPIC_API_KEY from /var/secrets/nowservingto.env.
"""
import os, sys, csv, json, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent.parent
WEB_CACHE_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'
PLACES_CACHE_PATH = ROOT / 'tools' / 'cache' / 'places_cache.json'
LLM_CACHE_PATH = ROOT / 'tools' / 'cache' / 'llm_cuisine_cache.json'
SECRETS = Path('/var/secrets/nowservingto.env')
MODEL = 'claude-haiku-4-5-20251001'
POLL_INTERVAL_SEC = 30
RECHECK_DAYS = 7
CSV_PATH = '/tmp/business_licences_alt.csv'

# Cuisine taxonomy is the canonical one from cuisines.py.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from cuisines import VALID_CUISINE_KEYS

SYSTEM_PROMPT = """You verify a Toronto restaurant's existence AND identify its cuisine
from web search results. Many places are brand new with only sparse online presence — that's fine.

You have access to web_search (up to 2 uses). First search: confirm the place exists and
identify cuisine. SECOND search (only if needed): if your first search did NOT surface a
Google Maps / Google Business listing URL for this restaurant, do one targeted search like
`"<NAME>" "<address or street>" site:google.com/maps` to find one. A Google Maps profile
is strongly preferred over Instagram/Facebook for the `website` field — see rules below.

Return a single JSON object on ONE line, no markdown, no prose:
{"operating":"yes|no|unclear","cuisine":"<key>","website":"<url or null>","evidence":"<one short sentence>"}

Valid cuisine keys:
italian, chinese, japanese, korean, vietnamese, filipino, thai, indonesian, malaysian, burmese,
cambodian, laotian,
south_asian, indian, pakistani, afghan, bangladeshi, tamil, tibetan, sri_lankan, nepalese,
caribbean, jamaican, trinidadian, guyanese, haitian, cuban, dominican,
greek, portuguese, polish, french, irish_uk, german, jewish_deli, spanish,
eastern_eu, ukrainian, russian, hungarian,
middle_east, lebanese, turkish, syrian, persian, israeli, egyptian, yemeni, armenian, georgian,
latin, mexican, salvadoran, peruvian, colombian, brazilian, argentinian, venezuelan,
african_horn, ethiopian, eritrean, somali,
african_west, nigerian, ghanaian, moroccan, senegalese, unknown

Use search evidence (menus, reviews, owner bios, articles) to choose cuisine — NOT just the
operating name. Prefer the most SPECIFIC bucket (ethiopian over african_horn; mexican over latin).
Use the umbrella only when the country isn't clear from evidence.

IMPORTANT: ALWAYS return a non-null cuisine value. If you find a restaurant exists but
cannot determine its cuisine from the web evidence (generic operating signals like
delivery-platform listings but no menu/cuisine clues, or a redirected/parked website
without informative content), explicitly return "unknown". Do NOT leave the cuisine
field null — "unknown" is the correct response when the cuisine truly isn't clear,
and lets us avoid falling back to a name-only guess that may be wrong (e.g., "Tumi
Dumpling House" sounds Tibetan from the name alone but is actually Chinese).

CRITICAL: American/Canadian chains = unknown regardless of theme.
- Popeyes Louisiana Kitchen → unknown (NOT caribbean, NOT jamaican)
- KFC, Mary Brown's, Wendy's, A&W → unknown
- Applebee's, IHOP, Boston Pizza, Tim Hortons, Subway → unknown
- If the website says "Proudly Canadian" or the place describes itself as American/Canadian
  fusion without a clear country-of-origin cuisine → unknown.
- A surname-only name without other ethnic signal → unknown.

CRITICAL: Pan-Asian / Asia-Pacific / Asian fusion restaurants that draw from 3+ regional
cuisines (e.g., Korean + Hawaiian + Vietnamese + Chinese) don't fit any single bucket.
Return unknown — the directory promises a specific cuisine and fusion betrays that.
- "Koha Pacific Kitchen" (Korean + Hawaiian poke + bao + banh mi) → unknown
- "Bao Banh Bowl" / "Asia-Pacific Kitchen" / "Pan-Asian Grill" → unknown
- A Korean restaurant that ALSO has a few sushi rolls is still korean — only flag
  when the menu spans 3+ regional cuisines as roughly equal billing.

CRITICAL: American Southern themes (New Orleans, Cajun, Creole, Bayou, Soul, Memphis BBQ,
Texas BBQ, Tex-Mex non-Mexican) are NOT Caribbean or Latin. We have no taxonomy bucket
for US Southern cuisine — return unknown.
- "New Orleans Seafood & Steakhouse" → unknown (NOT caribbean, NOT latin)
- "Bayou Bar & Grill" → unknown
- "Memphis BBQ" → unknown
- A Louisiana/Mississippi reference in the name = American South, not Caribbean.

CRITICAL: Packaged-food brands, manufacturers, distributors, importers, and wholesalers
that hold a take-out/retail licence at their factory or warehouse are NOT consumer
restaurants. Return cuisine=unknown.
- If the website's primary purpose is selling packaged goods (a Products catalog,
  Where-to-buy locator, distributor inquiries) rather than menu/hours/reservations → unknown
- If the address is in an industrial park (e.g. Steeles, Caledonia, Dixie warehouses)
  AND the business name contains "Foods", "Imports", "Brands", "Distributors", "Inc.",
  or "Co.", it is overwhelmingly a manufacturer with no walk-in dining → unknown
- Examples: "Shimla Foods Take Out" at 6801 Steeles W (brand sold at grocery stores),
  "Patel Brothers" warehouse counter, "Roma Foods" factory outlet → all unknown
- Genuine retail with hot prepared food (a roti shop, a butcher counter that fries
  samosas to order, a grocery store with a full hot table) → tag with the appropriate cuisine

Rules for "operating":
- "yes" — ANY plausible online evidence the place exists. The bar is LOW.
  Acceptable: own website, Instagram or Facebook page matching the name, a Google Maps
  profile with the address, recent Yelp/blogTO/TripAdvisor mention, food blog write-up,
  recent news article, TikTok/YouTube video, community board mention. If a person could
  reasonably find this place from a single search, it qualifies.
- "no" — explicit evidence it has CLOSED (announcement, successor business now at the
  address, "permanently closed" notice, news about its closing).
- "unclear" — search returned nothing at all relevant; no trace of the business anywhere
  online. Use sparingly — most new licences will have at least an Instagram post.

Rules for "website" (return the BEST link you find, in this STRICT order of preference):
1. The restaurant's own website (.com / .ca etc). Always wins if it exists.
2. The restaurant's Google Maps / Google Business listing URL
   (https://www.google.com/maps/place/... or https://maps.app.goo.gl/...).
   MANDATORY: if a Google Maps profile for this restaurant exists at this address, you MUST
   return it over any Instagram/Facebook/TikTok page — even if the social page is more
   recently active. Maps profiles give hours, photos, reviews, directions, and a stable URL.
   If your first search didn't surface a Maps URL, do a second search specifically to find one
   (see instructions above) before falling back to social.
3. An Instagram or Facebook page that clearly matches the restaurant name — ONLY if no Maps
   listing exists or you genuinely cannot find one after a targeted Maps search.
4. A specific blogTO / Eater / Toronto Star / food-blog article about THIS restaurant
   (not a generic "best of" list mentioning many places).
5. A Yelp or TripAdvisor page for this specific restaurant.
6. If nothing usable above, null.
Skip pure aggregator listings, licence-lookup pages, address directories."""

def load_api_key():
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

def parse_d(s):
    if not s: return None
    s = s.strip()
    for fmt in ('%Y-%m-%d','%Y/%m/%d','%m/%d/%Y','%Y-%m-%dT%H:%M:%S'):
        try: return datetime.strptime(s.split(' ')[0], fmt).date()
        except ValueError: pass
    return None

def cache_key(name, address):
    return f"{(name or '').strip().upper()}||{(address or '').strip().upper()}"

def website_tier(url):
    """1=own site (best), 2=social, 3=blog/aggregator, 4=no link"""
    if not url: return 4
    u = url.lower()
    if any(d in u for d in ('instagram.com', 'facebook.com', 'tiktok.com')): return 2
    if any(d in u for d in ('blogto.com','tripadvisor.','yelp.com','toronto.com','timeout.com','google.com/maps','goo.gl/maps')): return 3
    return 1

RECHECK_BY_TIER = {1: 180, 2: 30, 3: 14, 4: 14}  # days for yes-verdict at each tier
RECHECK_NO = 60
RECHECK_UNCLEAR = 7

def needs_recheck(entry):
    if not entry: return True
    if entry.get('status') != 'ok': return True
    try:
        ts = datetime.fromisoformat(entry['verified_at'])
        age = (datetime.now(timezone.utc) - ts).days
    except Exception:
        return True
    op = entry.get('operating')
    if op == 'yes':
        tier = website_tier(entry.get('website'))
        return age >= RECHECK_BY_TIER[tier]
    if op == 'no':
        return age >= RECHECK_NO
    if op == 'unclear':
        return age >= RECHECK_UNCLEAR
    return True

def build_request(name, address):
    return {
        'params': {
            'model': MODEL,
            'max_tokens': 400,
            'system': SYSTEM_PROMPT,
            'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 2}],
            'messages': [{
                'role': 'user',
                'content': f"Restaurant: {name}\nAddress: {address}\n\nIs this place currently operating? What's its website if any?"
            }],
        },
    }

def parse_result_msg(msg):
    """Pull JSON from final text + count searches + tokens."""
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
        except: parsed = {'operating': 'unclear', 'website': None, 'evidence': 'parse_failed'}
    cuisine = parsed.get('cuisine')
    if isinstance(cuisine, str) and cuisine.lower() in VALID_CUISINE_KEYS:
        cuisine = cuisine.lower()
    else:
        cuisine = None
    return {
        'status': 'ok',
        'operating': parsed.get('operating') if parsed.get('operating') in ('yes','no','unclear') else 'unclear',
        'cuisine': cuisine,
        'website': parsed.get('website') if isinstance(parsed.get('website'), str) and parsed.get('website').startswith(('http://','https://')) else None,
        'evidence': (parsed.get('evidence') or '')[:200],
        'verified_at': datetime.now(timezone.utc).isoformat(),
        'in_tok': usage.get('input_tokens', 0),
        'out_tok': usage.get('output_tokens', 0),
        'searches': server_tool.get('web_search_requests', 0),
        'via': 'batch',
    }

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
    # Load existing caches
    cache = json.loads(WEB_CACHE_PATH.read_text()) if WEB_CACHE_PATH.exists() else {}
    places = json.loads(PLACES_CACHE_PATH.read_text()) if PLACES_CACHE_PATH.exists() else {}
    llm = json.loads(LLM_CACHE_PATH.read_text()) if LLM_CACHE_PATH.exists() else {}
    print(f"caches: web_verify={len(cache)} places={len(places)} llm={len(llm)}")

    # Build candidate list (same gating as sync version)
    from datetime import date, timedelta as td
    today = date.today()
    cutoff = today - td(days=365)
    FOOD_CATS = {
        'EATING OR DRINKING ESTABLISHMENT','TAKE-OUT OR RETAIL FOOD ESTABLISHMENT',
        'EATING ESTABLISHMENT','RETAIL STORE (FOOD)',
    }
    candidates = []
    seen = set()
    with open(CSV_PATH, encoding='utf-8', errors='replace') as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            cat = (row.get('Category') or '').strip()
            if cat not in FOOD_CATS: continue
            if (row.get('Cancel Date') or '').strip(): continue
            iss = parse_d(row.get('Issued'))
            if not iss or iss < cutoff: continue
            name = (row.get('Operating Name') or '').strip()
            if not name: continue
            addr = ((row.get('Licence Address Line 1') or '').strip() + ' ' + (row.get('Licence Address Line 3') or '').strip()).strip()
            k = cache_key(name, addr)
            if k in seen: continue
            seen.add(k)
            llm_e = llm.get(k)
            if not (llm_e and llm_e.get('status') == 'ok' and llm_e.get('cuisine') and llm_e.get('cuisine') != 'unknown'): continue
            p = places.get(k)
            if p and p.get('status') == 'ok' and p.get('businessStatus') == 'OPERATIONAL': continue
            if needs_recheck(cache.get(k)):
                candidates.append((k, name, addr))

    print(f"candidates needing verification: {len(candidates)}")
    if not candidates:
        print("nothing to verify."); return

    # Build a custom_id → key map (sortable, deterministic)
    id_to_key = {}
    full_requests = []
    for i, (k, n, a) in enumerate(candidates):
        cid = f"v{i:04d}"
        id_to_key[cid] = k
        rec = build_request(n, a)
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
    parsed = parse_result_msg(canary_msg)
    cache[candidates[0][0]] = parsed
    WEB_CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    print(f"  canary verdict for {candidates[0][1]}: operating={parsed['operating']} website={parsed.get('website')}")

    # ---- Full batch ----
    full_id = submit_batch(full_requests, 'FULL')
    info = poll(full_id, 'FULL')
    results = download_results(info)

    yes = no = unclear = err = 0
    tot_in = tot_out = tot_search = 0
    for obj in results:
        cid = obj.get('custom_id')
        key = id_to_key.get(cid)
        if not key: continue
        result = obj.get('result', {})
        if result.get('type') != 'succeeded':
            err += 1
            cache[key] = {'status': 'error', 'error': f'batch {result.get("type")}', 'verified_at': datetime.now(timezone.utc).isoformat()}
            continue
        parsed = parse_result_msg(result['message'])
        cache[key] = parsed
        tot_in += parsed.get('in_tok', 0)
        tot_out += parsed.get('out_tok', 0)
        tot_search += parsed.get('searches', 0)
        op = parsed.get('operating')
        if op == 'yes': yes += 1
        elif op == 'no': no += 1
        else: unclear += 1

    WEB_CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    # 50% off everything
    cost = (tot_in/1e6 + tot_out/1e6*5 + tot_search*0.01) * 0.5
    print(f"\nDone: yes={yes}  no={no}  unclear={unclear}  err={err}")
    print(f"  tokens in={tot_in:,} out={tot_out:,} searches={tot_search}  est ${cost:.2f} (50% off)")

if __name__ == '__main__':
    main()
