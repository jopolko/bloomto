#!/usr/bin/env python3
"""
Find official websites for cuisine-tagged new openings that we don't already
have website data for (i.e. not in tools/cache/places_cache.json). Uses Claude
Haiku 4.5 with the built-in web_search tool.

Strategy: limit to entries that will appear in the visible feed
(newOpenings.recent + per-cuisine recent5). Cache results so re-runs are free.

Cost: ~$0.011 per business (1 search + small LLM token use).
"""
import os, sys, json, time, threading
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / 'data' / 'corridors.json'
WEB_CACHE_PATH = ROOT / 'tools' / 'cache' / 'website_cache.json'
PLACES_CACHE_PATH = ROOT / 'tools' / 'cache' / 'places_cache.json'
SECRETS = Path('/var/secrets/nowservingto.env')
MODEL = 'claude-haiku-4-5-20251001'
RATE_PER_MINUTE = 40
WORKERS = 2
CHECKPOINT_EVERY = 10

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
    if not SECRETS.exists(): sys.exit(f"missing {SECRETS}")
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if line.startswith('ANTHROPIC_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    sys.exit("ANTHROPIC_API_KEY not in secrets file")

API_KEY = load_api_key()

SYSTEM_PROMPT = """You are looking up the OFFICIAL website of a Toronto restaurant.

You have access to web_search. Search for the restaurant by name and address, then
return ONE single line containing only the canonical official website URL.

Rules:
- Return ONLY the URL. No prose, no markdown, no quotes.
- If you can find a real official site (not Yelp, TripAdvisor, blogTO, OpenTable,
  UberEats, DoorDash, Google Maps, Instagram aggregator), return that URL.
- Instagram or Facebook page is acceptable ONLY if the restaurant has no other
  web presence.
- If nothing definitive after one search, return: none
- Never invent a URL."""

def lookup_website(name, address, retries=2):
    LIMITER.acquire()
    body = json.dumps({
        'model': MODEL,
        'max_tokens': 200,
        'system': SYSTEM_PROMPT,
        'tools': [{
            'type': 'web_search_20250305',
            'name': 'web_search',
            'max_uses': 2,
        }],
        'messages': [{
            'role': 'user',
            'content': f"Restaurant: {name}\nAddress: {address}\n\nFind its official website."
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
        with urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
    except HTTPError as e:
        if e.code == 429 and retries > 0:
            time.sleep(2.5)
            return lookup_website(name, address, retries - 1)
        return {'status': 'error', 'error': f'http {e.code}', 'detail': e.read().decode('utf-8')[:200]}
    except Exception as e:
        return {'status': 'error', 'error': str(e)[:200]}

    # Extract final text from message
    text_parts = [b.get('text', '') for b in resp.get('content', []) if b.get('type') == 'text']
    final = (text_parts[-1] if text_parts else '').strip().split()
    candidate = final[0] if final else ''
    # sanitize: keep URL or 'none'
    if candidate.lower() in ('none', 'no', '-', 'n/a'): candidate = None
    elif not (candidate.startswith('http://') or candidate.startswith('https://')): candidate = None

    usage = resp.get('usage', {})
    return {
        'status': 'ok',
        'website': candidate,
        'in_tok': usage.get('input_tokens', 0),
        'out_tok': usage.get('output_tokens', 0),
        'web_search_count': usage.get('server_tool_use', {}).get('web_search_requests', 0) if isinstance(usage.get('server_tool_use'), dict) else 0,
    }

from places_key import cache_key  # canonical shared helper

def main():
    data = json.loads(DATA_PATH.read_text())
    no = data.get('newOpenings')
    if not no: sys.exit("data/corridors.json has no newOpenings — inject first")

    web_cache = json.loads(WEB_CACHE_PATH.read_text()) if WEB_CACHE_PATH.exists() else {}
    places_cache = json.loads(PLACES_CACHE_PATH.read_text()) if PLACES_CACHE_PATH.exists() else {}

    # Collect all visible entries
    pairs = {}
    def add(e):
        k = e.get('_cacheKey') or cache_key(e.get('operatingName'), e.get('address'))
        if k in pairs: return
        # Skip if already has website (from places_cache merge) OR website lookup done
        if e.get('website'): return
        pl = places_cache.get(k)
        if pl and pl.get('status') == 'ok' and pl.get('website'): return
        wb = web_cache.get(k)
        if wb and wb.get('status') == 'ok': return
        pairs[k] = e
    for e in no.get('recent', []): add(e)
    for c in no.get('cuisines', []):
        for e in c.get('recent5', []): add(e)
        if c.get('newest'): add(c['newest'])

    to_fetch = list(pairs.items())
    print(f"website cache: {len(web_cache)} entries cached, {len(to_fetch)} to fetch")
    print(f"estimated spend: ${len(to_fetch) * 0.011:.2f}")

    if not to_fetch:
        print("nothing to do.")
        return

    t0 = time.time()
    ok = no_url = err = 0
    total_in = total_out = 0
    total_searches = 0

    def worker(item):
        k, e = item
        res = lookup_website(e.get('operatingName'), e.get('address'))
        return k, e, res

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(worker, t): t for t in to_fetch}
        for fut in as_completed(futures):
            try: k, e, res = fut.result()
            except Exception as ex_: print(f"  worker err: {ex_}"); continue
            web_cache[k] = res
            done += 1
            if res.get('status') == 'ok':
                total_in += res.get('in_tok', 0)
                total_out += res.get('out_tok', 0)
                total_searches += res.get('web_search_count', 0)
                if res.get('website'): ok += 1
                else: no_url += 1
            else: err += 1
            if done % CHECKPOINT_EVERY == 0 or done == len(to_fetch):
                WEB_CACHE_PATH.write_text(json.dumps(web_cache, separators=(',', ':')))
                el = time.time() - t0
                # Haiku 4.5: $1/M input, $5/M output. Web search: $10/1000.
                cost = total_in/1e6 + total_out/1e6*5 + total_searches*0.01
                print(f"  [{done:>3}/{len(to_fetch)}]  ok={ok}  no_url={no_url}  err={err}  searches={total_searches}  est ${cost:.3f}  ({el:.0f}s)")
    WEB_CACHE_PATH.write_text(json.dumps(web_cache, separators=(',', ':')))

    # Merge into corridors.json
    print("Merging websites into corridors.json…")
    def merge(e):
        k = e.get('_cacheKey') or cache_key(e.get('operatingName'), e.get('address'))
        wb = web_cache.get(k)
        if wb and wb.get('status') == 'ok' and wb.get('website'):
            e['website'] = wb['website']
    for e in no.get('recent', []): merge(e)
    for c in no.get('cuisines', []):
        for e in c.get('recent5', []): merge(e)
        if c.get('newest'): merge(c['newest'])
    DATA_PATH.write_text(json.dumps(data, separators=(',', ':')))
    print(f"  wrote {DATA_PATH}")

if __name__ == '__main__':
    main()
