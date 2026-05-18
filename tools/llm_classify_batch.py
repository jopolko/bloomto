#!/usr/bin/env python3
"""
Submit a Message Batches job for cuisine classification. Picks up:
  - Newly-licensed entries in the last 365 days that aren't yet in the cache
  - Entries previously marked status='error' for retry

50% off vs sync, much higher rate limits, polls until done, merges into the cache.
Designed to be safe-to-call from the daily cron — exits cleanly with no spend if
nothing is missing/errored.

Reads ANTHROPIC_API_KEY from /var/secrets/nowservingto.env.
"""
import os, sys, csv, json, time
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

CSV_PATH = '/tmp/business_licences_alt.csv'
FOOD_CATS = {
    'EATING OR DRINKING ESTABLISHMENT',
    'TAKE-OUT OR RETAIL FOOD ESTABLISHMENT',
    'EATING ESTABLISHMENT',
    'RETAIL STORE (FOOD)',
}

def _parse_d(s):
    if not s: return None
    s = s.strip()
    for fmt in ('%Y-%m-%d','%Y/%m/%d','%m/%d/%Y','%Y-%m-%dT%H:%M:%S'):
        try: return datetime.strptime(s.split(' ')[0], fmt).date()
        except ValueError: pass
    return None

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / 'tools' / 'cache' / 'llm_cuisine_cache.json'
SECRETS = Path('/var/secrets/nowservingto.env')
MODEL = 'claude-haiku-4-5-20251001'
POLL_INTERVAL_SEC = 30

SYSTEM_PROMPT = """You classify a Toronto restaurant by cuisine from the operating
name + licence address ALONE — no Places match, no website, no reviews available
at this stage of the pipeline. A later validator pass sees the richer evidence.

Return JSON on one line: {"cuisines":["k1","k2",...],"evidence":"<short>"}

Valid cuisine keys (use only these):
italian, chinese, japanese, korean, vietnamese, filipino, thai, indonesian, malaysian,
burmese, cambodian, laotian, south_asian, indian, pakistani, afghan, bangladeshi, tamil,
tibetan, sri_lankan, nepalese, caribbean, jamaican, trinidadian, guyanese, haitian,
cuban, dominican, greek, portuguese, polish, french, irish_uk, german, jewish_deli,
spanish, eastern_eu, ukrainian, russian, hungarian, middle_east, lebanese, turkish,
syrian, persian, israeli, egyptian, yemeni, armenian, georgian, latin, mexican,
salvadoran, peruvian, colombian, brazilian, argentinian, venezuelan, african_horn,
ethiopian, eritrean, somali, african_west, nigerian, ghanaian, moroccan, senegalese,
unknown.

Multi-cuisine OK when the name explicitly states 2-3 countries side-by-side
("Afghan, Pakistani & Indian Flavors"). Default to ["unknown"] when the name
gives no cuisine signal (no country marker, no signature dish word). When the
signal is a regional dish shared across multiple countries (jollof, kebab,
shawarma, biryani, dumpling, roti), tag the umbrella (african_west, middle_east,
south_asian) instead of guessing a specific country."""

# Cuisine taxonomy is the canonical one from cuisines.py.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from cuisines import VALID_CUISINE_KEYS as VALID_KEYS, parse_cuisines_from_llm

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

def http_request(method, url, data=None):
    body = json.dumps(data).encode('utf-8') if data else None
    req = Request(url, data=body, headers=HEADERS, method=method)
    try:
        with urlopen(req, timeout=120) as r:
            raw = r.read()
            ctype = r.headers.get('Content-Type', '')
            if 'application/json' in ctype:
                return json.loads(raw)
            return raw
    except HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode('utf-8')[:300]}")
        raise

def main():
    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    print(f"cache state: total={len(cache)}")

    # 1. Walk the CSV for entries in the last 365 days that aren't cached as 'ok'
    cutoff = date.today() - timedelta(days=365)
    targets = []  # list of (cache_key, name, address)
    seen = set()
    if Path(CSV_PATH).exists():
        with open(CSV_PATH, encoding='utf-8', errors='replace') as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                cat = (row.get('Category') or '').strip()
                if cat not in FOOD_CATS: continue
                if (row.get('Cancel Date') or '').strip(): continue
                iss = _parse_d(row.get('Issued'))
                if not iss or iss < cutoff: continue
                name = (row.get('Operating Name') or '').strip()
                if not name: continue
                addr1 = (row.get('Licence Address Line 1') or '').strip()
                addr3 = (row.get('Licence Address Line 3') or '').strip()
                address = (addr1 + ' ' + addr3).strip() or '—'
                key = f"{name.upper()}||{address.upper()}"
                if key in seen: continue
                seen.add(key)
                ex = cache.get(key)
                if ex and ex.get('status') == 'ok': continue  # already classified successfully
                targets.append((key, name, address))
    n_new = len([1 for k,_,_ in targets if k not in cache])
    n_retry = len(targets) - n_new
    print(f"  targets: {len(targets)} ({n_new} new, {n_retry} retries from previous errors)")

    if not targets:
        print("nothing to classify.")
        return

    # 2. Build batch payload
    requests = []
    target_keys = []
    for key, name, address in targets:
        custom_id = 'c' + str(hash(key) & 0x7fffffff)
        requests.append({
            'custom_id': custom_id,
            'params': {
                'model': MODEL,
                'max_tokens': 80,
                'system': SYSTEM_PROMPT,
                'messages': [{
                    'role': 'user',
                    'content': f"Operating name: {name}\nAddress: {address}",
                }],
            },
        })
        target_keys.append(key)

    id_to_key = {r['custom_id']: k for r, k in zip(requests, target_keys)}

    print(f"submitting batch of {len(requests)} requests…")
    submit_resp = http_request('POST', 'https://api.anthropic.com/v1/messages/batches', {'requests': requests})
    batch_id = submit_resp['id']
    print(f"  batch_id: {batch_id}")
    print(f"  status: {submit_resp['processing_status']}")

    # Poll
    print("polling…")
    t0 = time.time()
    while True:
        time.sleep(POLL_INTERVAL_SEC)
        info = http_request('GET', f'https://api.anthropic.com/v1/messages/batches/{batch_id}')
        st = info['processing_status']
        counts = info.get('request_counts', {})
        el = time.time() - t0
        print(f"  [{el:.0f}s]  status={st}  counts={counts}")
        if st == 'ended':
            break
        if st in ('cancelling', 'canceled', 'expired'):
            sys.exit(f"batch ended unexpectedly: {st}")

    # Download results — they come as JSONL from results_url
    results_url = info.get('results_url')
    if not results_url:
        sys.exit("no results_url on completed batch")
    print(f"downloading results from {results_url}")
    raw = http_request('GET', results_url)
    if isinstance(raw, dict):
        # If JSON parsed (single object), shouldn't happen — JSONL is multi-line
        raw_text = json.dumps(raw)
    else:
        raw_text = raw.decode('utf-8')

    # Parse JSONL, merge into cache
    n_ok = n_err = 0
    total_in = total_out = 0
    for line in raw_text.strip().split('\n'):
        if not line.strip(): continue
        obj = json.loads(line)
        cid = obj.get('custom_id')
        key = id_to_key.get(cid)
        if not key: continue
        result = obj.get('result', {})
        rtype = result.get('type')
        if rtype != 'succeeded':
            n_err += 1
            cache[key] = {'status': 'error', 'error': f'batch {rtype}'}
            continue
        msg = result.get('message', {})
        usage = msg.get('usage', {})
        total_in += usage.get('input_tokens', 0)
        total_out += usage.get('output_tokens', 0)
        text = ''.join(b.get('text','') for b in msg.get('content', []) if b.get('type')=='text').strip()
        # Parse the JSON object — new format `{"cuisines":["italian"]}`. Fall back to
        # plain-token scan for legacy single-key responses.
        cuisines = []
        for line in text.split('\n'):
            s = line.strip().lstrip('`').strip()
            if s.startswith('{') and s.endswith('}'):
                try:
                    cuisines = parse_cuisines_from_llm(json.loads(s))
                    break
                except Exception:
                    continue
        if not cuisines:
            # Legacy bare-token fallback (older prompt outputs)
            for tok in text.lower().replace(',', ' ').split():
                t = tok.strip('.: \t\n"\'`{}[]')
                if t in VALID_KEYS:
                    cuisines = [t]; break
        if not cuisines: cuisines = ['unknown']
        cache[key] = {
            'status': 'ok',
            'cuisine': cuisines[0],          # primary (first listed) — backwards compat
            'cuisines': cuisines,            # full list for multi-cuisine entries
            'raw': text[:200],
            'in_tok': usage.get('input_tokens', 0),
            'out_tok': usage.get('output_tokens', 0),
            'via': 'batch',
        }
        n_ok += 1

    CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    # Batch API is 50% off
    cost = (total_in/1e6 * 1.0 + total_out/1e6 * 5.0) * 0.5
    print(f"\nbatch merged: ok={n_ok} err={n_err}  tokens in={total_in:,} out={total_out:,}  est ${cost:.3f} (50%-off)")
    try:
        sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
        from usage_log import log_usage
        log_usage('anthropic.haiku.batch.in',  units=total_in,  meta={'script':'classify_batch','batch_id':full_id})
        log_usage('anthropic.haiku.batch.out', units=total_out, meta={'script':'classify_batch','batch_id':full_id})
    except Exception: pass

    # Final distribution
    breakdown = {}
    for v in cache.values():
        if v.get('status') != 'ok': continue
        c = v.get('cuisine', 'unknown')
        breakdown[c] = breakdown.get(c, 0) + 1
    total = sum(breakdown.values())
    print(f"\nFull cache: {total} classified entries")
    for c, n in sorted(breakdown.items(), key=lambda x: -x[1]):
        print(f"  {c:14s} {n:>5}")

if __name__ == '__main__':
    main()
