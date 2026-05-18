#!/usr/bin/env python3
"""
Verify that a Toronto restaurant licence corresponds to an actually-operating
business. For each unverified cuisine-tagged opening, runs ONE web_search via
Claude Haiku 4.5 and returns:
    {operating: 'yes' | 'no' | 'unclear', website: url-or-null, evidence: '...'}

Cache: tools/cache/web_verify_cache.json
Caches all verdicts with a verified_at timestamp. Re-runs:
  - Always re-fetch entries that are missing or older than RECHECK_DAYS
    AND that were not previously 'yes' (those stay verified).
  - 'yes' entries stay cached forever — once verified open, we trust it.

Cost: ~$0.011 per call (1 web_search + small Haiku token use).
"""
import os, sys, json, time, threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / 'data' / 'corridors.json'
WEB_CACHE_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'
PLACES_CACHE_PATH = ROOT / 'tools' / 'cache' / 'places_cache.json'
LLM_CACHE_PATH = ROOT / 'tools' / 'cache' / 'llm_cuisine_cache.json'
SECRETS = Path('/var/secrets/nowservingto.env')
MODEL = 'claude-haiku-4-5-20251001'
RATE_PER_MINUTE = 35   # web_search calls are heavier; leave more headroom
WORKERS = 2
CHECKPOINT_EVERY = 15
RECHECK_DAYS = 7        # re-check non-'yes' verdicts after this many days

class RateLimiter:
    def __init__(self, max_per_minute):
        self.max = max_per_minute
        self.lock = threading.Lock()
        self.calls = []
    def acquire(self):
        while True:
            with self.lock:
                now = time.time()
                self.calls = [t for t in self.calls if now - t < 60]
                if len(self.calls) < self.max:
                    self.calls.append(now); return
                sleep_for = 60 - (now - self.calls[0]) + 0.05
            time.sleep(max(0.05, sleep_for))

LIMITER = RateLimiter(RATE_PER_MINUTE)

def load_api_key():
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if line.startswith('ANTHROPIC_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    sys.exit("ANTHROPIC_API_KEY not in secrets")

API_KEY = load_api_key()

SYSTEM_PROMPT = """You verify a Toronto restaurant's existence AND identify its cuisine
from web search results. Many places are brand new with only sparse online presence — that's fine.

You have access to web_search. Use one search to find evidence.

Return a single JSON object on ONE line, no markdown, no prose:
{"operating":"yes|no|unclear","cuisine":"<key>","website":"<url or null>","evidence":"<one short sentence>"}

Valid cuisine keys:
italian, chinese, japanese, korean, vietnamese, filipino, thai, indonesian, malaysian, burmese,
south_asian, indian, pakistani, afghan, bangladeshi, tamil, tibetan,
caribbean, jamaican, trinidadian, guyanese, haitian,
greek, portuguese, polish, french, irish_uk, german, jewish_deli,
eastern_eu, ukrainian, russian, hungarian,
middle_east, lebanese, turkish, syrian, persian,
latin, mexican, salvadoran, peruvian, colombian, brazilian,
african_horn, ethiopian, eritrean, somali,
african_west, nigerian, ghanaian, moroccan, unknown

Use search evidence (menus, reviews, owner bios, articles) to choose cuisine — NOT just the
operating name. Prefer the most SPECIFIC bucket (ethiopian over african_horn; mexican over latin).
Use the umbrella only when the country isn't clear from evidence.

CRITICAL: American/Canadian chains = unknown regardless of theme.
- Popeyes Louisiana Kitchen → unknown (NOT caribbean, NOT jamaican)
- KFC, Mary Brown's, Wendy's, A&W → unknown
- Applebee's, IHOP, Boston Pizza, Tim Hortons, Subway → unknown
- If the website says "Proudly Canadian" or the place describes itself as American/Canadian
  fusion without a clear country-of-origin cuisine → unknown.
- A surname-only name without other ethnic signal → unknown.

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

Rules for "website" (return the BEST link you find, in this order of preference):
1. The restaurant's own website (.com / .ca etc).
2. The restaurant's Google Maps / Google Business listing URL
   (https://www.google.com/maps/place/... or https://maps.app.goo.gl/...).
   Maps profiles give hours, photos, reviews, directions — strongly prefer over social.
3. An Instagram or Facebook page that clearly matches the restaurant name.
4. A specific blogTO / Eater / Toronto Star / food-blog article about THIS restaurant
   (not a generic "best of" list mentioning many places).
5. A Yelp or TripAdvisor page for this specific restaurant.
6. If nothing usable above, null.
Skip pure aggregator listings, licence-lookup pages, address directories."""

def verify_one(name, address, retries=2):
    LIMITER.acquire()
    body = json.dumps({
        'model': MODEL,
        'max_tokens': 400,
        'system': SYSTEM_PROMPT,
        'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 1}],
        'messages': [{
            'role': 'user',
            'content': f"Restaurant: {name}\nAddress: {address}\n\nIs this place currently operating? What's its website if any?"
        }],
    }).encode('utf-8')
    req = Request(
        'https://api.anthropic.com/v1/messages',
        data=body,
        headers={
            'x-api-key': API_KEY,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        }
    )
    try:
        with urlopen(req, timeout=90) as r:
            resp = json.loads(r.read())
    except HTTPError as e:
        if e.code == 429 and retries > 0:
            time.sleep(3.0)
            return verify_one(name, address, retries - 1)
        return {'status': 'error', 'error': f'http {e.code}'}
    except Exception as e:
        return {'status': 'error', 'error': str(e)[:200]}

    text = ''.join(b.get('text', '') for b in resp.get('content', []) if b.get('type') == 'text').strip()
    # Parse the JSON from final text; tolerate stray whitespace/markdown
    parsed = None
    for line in text.split('\n'):
        s = line.strip().lstrip('`').strip()
        if s.startswith('{') and s.endswith('}'):
            try: parsed = json.loads(s); break
            except: continue
    if parsed is None:
        # fallback: try to load whole text as json
        try: parsed = json.loads(text)
        except: parsed = {'operating': 'unclear', 'website': None, 'evidence': 'parse_failed:' + text[:120]}

    usage = resp.get('usage', {})
    server_tool = usage.get('server_tool_use') or {}
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
    }

from places_key import cache_key  # canonical shared helper

def website_tier(url):
    """1=own site (best), 2=social, 3=blog/aggregator, 4=no link"""
    if not url: return 4
    u = url.lower()
    if any(d in u for d in ('instagram.com', 'facebook.com', 'tiktok.com')): return 2
    if any(d in u for d in ('blogto.com','tripadvisor.','yelp.com','toronto.com','timeout.com','google.com/maps','goo.gl/maps')): return 3
    return 1

RECHECK_BY_TIER = {1: 180, 2: 30, 3: 14, 4: 14}
RECHECK_NO = 60
RECHECK_UNCLEAR = 7

VALID_CUISINE_KEYS = {
    'italian','chinese','japanese','korean','vietnamese','filipino','thai','indonesian','malaysian','burmese',
    'south_asian','indian','pakistani','afghan','bangladeshi','tamil','tibetan',
    'caribbean','jamaican','trinidadian','guyanese','haitian',
    'greek','portuguese','polish','french','irish_uk','german','jewish_deli',
    'eastern_eu','ukrainian','russian','hungarian',
    'middle_east','lebanese','turkish','syrian','persian',
    'latin','mexican','salvadoran','peruvian','colombian','brazilian',
    'african_horn','ethiopian','eritrean','somali',
    'african_west','nigerian','ghanaian','moroccan','unknown'
}

def needs_recheck(entry):
    """Tier-aware re-check: weak links and unclear verdicts are re-tried periodically
    so that when a place finally gets a proper website (or a blogTO writeup, or even
    Google indexes them), we naturally upgrade the link on next cron run."""
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

def main():
    cache = json.loads(WEB_CACHE_PATH.read_text()) if WEB_CACHE_PATH.exists() else {}
    places = json.loads(PLACES_CACHE_PATH.read_text()) if PLACES_CACHE_PATH.exists() else {}
    llm = json.loads(LLM_CACHE_PATH.read_text()) if LLM_CACHE_PATH.exists() else {}
    print(f"caches: web_verify={len(cache)} places={len(places)} llm={len(llm)}")

    # The set of candidates: every cuisine-tagged entry (LLM=cuisine_X or keyword) that is
    # NOT already Places-OPERATIONAL. We need the source CSV to walk this. For simplicity,
    # we read it directly here rather than via corridors.json which carries only verified ones.
    import csv
    from datetime import date, timedelta as td
    today = date.today()
    cutoff = today - td(days=365)
    FOOD_CATS = {
        'EATING OR DRINKING ESTABLISHMENT','TAKE-OUT OR RETAIL FOOD ESTABLISHMENT',
        'EATING ESTABLISHMENT','RETAIL STORE (FOOD)',
    }
    def parse_d(s):
        if not s: return None
        s = s.strip()
        for fmt in ('%Y-%m-%d','%Y/%m/%d','%m/%d/%Y','%Y-%m-%dT%H:%M:%S'):
            try: return datetime.strptime(s.split(' ')[0], fmt).date()
            except ValueError: pass
        return None

    candidates = []
    seen_keys = set()
    csv_path = '/tmp/business_licences_alt.csv'
    with open(csv_path, encoding='utf-8', errors='replace') as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            cat = (row.get('Category') or '').strip()
            if cat not in FOOD_CATS: continue
            if (row.get('Cancel Date') or '').strip(): continue
            iss = parse_d(row.get('Issued'))
            if not iss or iss < cutoff: continue
            name = (row.get('Operating Name') or '').strip()
            if not name: continue
            addr1 = (row.get('Licence Address Line 1') or '').strip()
            addr3 = (row.get('Licence Address Line 3') or '').strip()
            addr = (addr1 + ' ' + addr3).strip()
            k = cache_key(name, addr)
            if k in seen_keys: continue
            seen_keys.add(k)
            # Must be cuisine-tagged (LLM-ok-not-unknown OR keyword would match — we'll just
            # trust the LLM cache here as the canonical cuisine source)
            llm_entry = llm.get(k)
            if not (llm_entry and llm_entry.get('status') == 'ok' and llm_entry.get('cuisine') and llm_entry.get('cuisine') != 'unknown'):
                continue
            # Skip if Places already verified OPERATIONAL — no need to web-verify
            p = places.get(k)
            if p and p.get('status') == 'ok' and p.get('businessStatus') == 'OPERATIONAL':
                continue
            # Otherwise candidate for web verification
            if needs_recheck(cache.get(k)):
                candidates.append((k, name, addr))

    print(f"candidates needing verification: {len(candidates)}")
    print(f"estimated spend: ${len(candidates) * 0.011:.2f}")

    if not candidates:
        print("nothing to verify.")
        return

    t0 = time.time()
    yes = no = unclear = err = 0
    tot_in = tot_out = tot_search = 0
    done = 0

    def worker(item):
        k, n, a = item
        return k, verify_one(n, a)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(worker, c): c for c in candidates}
        for fut in as_completed(futures):
            try: k, res = fut.result()
            except Exception as e: print(f"worker err: {e}"); continue
            cache[k] = res
            done += 1
            if res.get('status') == 'ok':
                tot_in += res.get('in_tok', 0)
                tot_out += res.get('out_tok', 0)
                tot_search += res.get('searches', 0)
                op = res.get('operating', 'unclear')
                if op == 'yes': yes += 1
                elif op == 'no': no += 1
                else: unclear += 1
            else: err += 1
            if done % CHECKPOINT_EVERY == 0 or done == len(candidates):
                WEB_CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
                el = time.time() - t0
                cost = tot_in/1e6 + tot_out/1e6*5 + tot_search*0.01
                print(f"  [{done:>4}/{len(candidates)}]  yes={yes} no={no} unclear={unclear} err={err}  searches={tot_search}  est ${cost:.2f}  ({el:.0f}s)")

    WEB_CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    print(f"\nDone: yes={yes}  no={no}  unclear={unclear}  err={err}")
    print(f"  net new verified-open entries that can now appear on the page: {yes}")

if __name__ == '__main__':
    main()
