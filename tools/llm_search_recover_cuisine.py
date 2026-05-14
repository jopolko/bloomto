#!/usr/bin/env python3
"""
Layer 4 cuisine recovery: Haiku + web_search for entries whose website fetch failed.

Layer 2 (`llm_recover_cuisine.py`) tries to fetch the actual restaurant website
and read menu words. For ~98% of remaining null-cuisine entries, that fetch
returns no usable text — JS-only SPAs, Cloudflare-blocked, embedded ordering
iframes, image-only Squarespace shells. Headless rendering would mostly hit
the same walls (Turnstile challenges, third-party iframes, PDF menus).

This layer outsources the rendering to Google: Haiku queries Google by name +
neighborhood, Google returns snippets it extracted from its own (JS-rendered,
captcha-bypassed, iframe-followed) crawl, and Haiku classifies cuisine from
those snippets + the indexed review excerpts.

Targets:
  status=ok, operating=yes, cuisine null/unknown, AND a previous layer wrote a
  recovery_note (i.e. the website-fetch path already tried and failed). We re-
  attempt every 30 days via `search_recovered_at`.

Cost: ~$0.02 per entry on Haiku sync with 2 server-side web_search calls.
"""
import os, re, sys, json, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
WEB_VERIFY_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'
SECRETS = Path('/var/secrets/nowservingto.env')
MODEL = 'claude-haiku-4-5-20251001'
WORKERS = 2  # web_search is server-side and rate-limited; concurrency >2 triggers 429s

# Cuisine taxonomy is the canonical one from cuisines.py.
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

def classify_via_search(name, address):
    """Sync Haiku call with web_search tool. Returns (cuisine, evidence, n_searches)."""
    payload = json.dumps({
        'model': MODEL,
        'max_tokens': 800,
        'system': SYSTEM_PROMPT,
        'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 2}],
        'messages': [{
            'role': 'user',
            'content': f"Restaurant: {name}\nAddress: {address}\n\nSearch Google and tell me the cuisine.",
        }],
    }).encode('utf-8')
    req = Request('https://api.anthropic.com/v1/messages', data=payload, headers={
        'x-api-key': API_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
    }, method='POST')
    with urlopen(req, timeout=90) as r:
        msg = json.loads(r.read())
    blocks = msg.get('content', [])
    # Count actual web_search invocations for cost transparency
    n_searches = sum(1 for b in blocks if b.get('type') == 'server_tool_use' and b.get('name') == 'web_search')
    text_blocks = [b.get('text', '') for b in blocks if b.get('type') == 'text']
    text = (text_blocks[-1] if text_blocks else '').strip()
    for line in text.split('\n'):
        s = line.strip().lstrip('`').strip()
        if s.startswith('{') and s.endswith('}'):
            try:
                d = json.loads(s)
                cuisine = (d.get('cuisine') or '').strip().lower()
                evidence = (d.get('evidence') or '')[:200]
                if cuisine in VALID_CUISINE_KEYS:
                    return cuisine, evidence, n_searches
            except Exception:
                continue
    return None, None, n_searches

def needs_search_recovery(entry):
    if entry.get('status') != 'ok' or entry.get('operating') != 'yes':
        return False
    c = entry.get('cuisine')
    if c and c != 'unknown': return False
    # Only try this layer for entries the website-fetch layer already failed on.
    # If recovery_note is absent, Layer 2 hasn't tried yet — let it run first.
    if not entry.get('recovery_note'):
        return False
    sra = entry.get('search_recovered_at')
    if sra:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(sra)).days
            if age < 30: return False
        except Exception:
            pass
    return True

def main():
    cache = json.loads(WEB_VERIFY_PATH.read_text())
    targets = [(k, e) for k, e in cache.items() if needs_search_recovery(e)]

    # Allow a one-shot cap so the first production run doesn't blast through the
    # whole backlog before we've seen the yield: `LIMIT=50 python ...`
    limit = int(os.environ.get('LIMIT', '0') or '0')
    if limit and len(targets) > limit:
        print(f"LIMIT={limit} — capping from {len(targets)} eligible")
        targets = targets[:limit]

    print(f"verify cache entries:               {len(cache)}")
    print(f"needing search-based recovery:      {len(targets)}")
    print(f"  estimated cost (~$0.02 each):     ${len(targets)*0.02:.2f}")
    if not targets:
        return

    def work(key, e):
        name = key.split('||')[0]
        address = key.split('||')[1] if '||' in key else ''
        for attempt in range(3):
            try:
                cuisine, evidence, n_searches = classify_via_search(name, address)
                return key, cuisine, evidence, n_searches, None, False
            except HTTPError as ex:
                if ex.code == 429 and attempt < 2:
                    time.sleep(2 ** attempt + 1)  # 2s, 3s, then give up
                    continue
                body = ''
                try: body = ex.read().decode('utf-8', errors='replace')[:200]
                except Exception: pass
                # Treat 429 specially: it's transient. Don't stamp recovered_at,
                # so the entry is eligible to retry on the next cron run.
                is_rate_limit = (ex.code == 429)
                return key, None, None, 0, f"HTTP {ex.code}: {body[:100]}", is_rate_limit
            except URLError as ex:
                return key, None, None, 0, f"URLError: {str(ex)[:80]}", False
            except Exception as ex:
                return key, None, None, 0, f"{type(ex).__name__}: {str(ex)[:80]}", False
        return key, None, None, 0, "exhausted retries", True

    now_iso = datetime.now(timezone.utc).isoformat()
    n_recovered = n_unknown = n_failed = n_rate_limited = total_searches = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(work, k, v) for k, v in targets]
        for i, fut in enumerate(as_completed(futures), 1):
            key, cuisine, evidence, n_searches, err, is_rate_limit = fut.result()
            total_searches += n_searches
            if is_rate_limit:
                # Transient — don't stamp recovered_at so it's eligible to retry
                # tomorrow. Just record what happened for debugging.
                n_rate_limited += 1
                cache[key]['search_recovery_note'] = err[:120]
            else:
                cache[key]['search_recovered_at'] = now_iso
                if err:
                    n_failed += 1
                    cache[key]['search_recovery_note'] = err[:120]
                elif cuisine == 'unknown' or cuisine is None:
                    n_unknown += 1
                    cache[key]['search_recovery_note'] = (evidence or 'search recovery — still unknown')[:120]
                    if cuisine == 'unknown':
                        cache[key]['cuisine'] = 'unknown'
                else:
                    n_recovered += 1
                    cache[key]['cuisine'] = cuisine
                    cache[key]['evidence'] = (evidence or '')[:200]
                    cache[key]['recovery_source'] = 'web_search'
            if i % 10 == 0 or i == len(targets):
                el = time.time() - t0
                print(f"  [{i}/{len(targets)}] {el:.0f}s  recovered={n_recovered}  unknown={n_unknown}  failed={n_failed}  rate-limited={n_rate_limited}  searches={total_searches}")
                WEB_VERIFY_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    WEB_VERIFY_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    actual_cost = total_searches * 0.01 + (len(targets) - n_rate_limited) * 0.005
    print(f"\nDone in {time.time()-t0:.0f}s: recovered={n_recovered}  unknown={n_unknown}  failed={n_failed}  rate-limited(retry tomorrow)={n_rate_limited}")
    print(f"  web_search calls: {total_searches}  approx cost: ${actual_cost:.2f}")

if __name__ == '__main__':
    main()
