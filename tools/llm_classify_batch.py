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

SYSTEM_PROMPT = """You classify Toronto restaurants by cuisine from operating name + address.

Output: ONE lowercase key, no other text. Choose from:
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

ALWAYS prefer the most SPECIFIC bucket. Only use the broader umbrella when the name fits a
region but no specific country signal is present.

South Asian — PREFER specific country over umbrella:
- indian: pan-Indian, Mughlai, Punjabi-NOT-Pakistani, North/South Indian, "India", "Indian",
  tandoori-house, masala-house, biryani-house (when not specifically Pakistani), naan house,
  dosa, idli, thali, samosa house. THIS is the right bucket for most "South Asian" places.
- pakistani: explicitly Pakistani — Karachi, Lahore, "halal pak", "Pak Punjab"
- afghan: Kabul, Kandahar, mantu, kabuli pulao
- bangladeshi: Dhaka, Bengali, "bangla", Bengali sweets
- tamil: Sri Lankan Tamil or South Indian Tamil (Jaffna, Eelam, Madras, Chennai, kothu)
- tibetan: Tibetan / Himalayan (momo, Lhasa, Shangri-La)
- south_asian: ONLY for genuinely multi-country South Asian buffets/mixes. Default to indian.

Southeast Asian:
- vietnamese: Pho, banh mi, Saigon, Hanoi
- thai: pad thai, tom yum, Bangkok
- filipino: adobo, lechon, Pinoy, Manila, kainan
- indonesian: nasi goreng, rendang, satay, Jakarta, Bali
- malaysian: nasi lemak, laksa, Kuala Lumpur, Penang
- burmese: Myanmar, Yangon, Rangoon, mohinga

East African:
- ethiopian: Injera, Habesha, Addis, Awasa, Mercato, doro wat
- eritrean: Asmara, Massawa
- somali: Banadir, Mogadishu, Hargeisa, suqaar
- african_horn: generic Horn of Africa umbrella (use only if no specific country signal)

West African:
- nigerian: Lagos, Naija, Yoruba/Igbo names, jollof + suya combos, egusi
- ghanaian: Accra, Ghanaian, waakye, banku
- moroccan: tagine, Marrakech, Fez, Casablanca, couscous (split from west, technically North Africa)
- african_west: generic West African umbrella (Senegal, Mali, etc., or no specific country)

Caribbean (cultural grouping — Guyana is geographically South America but culinarily fits here):
- jamaican: Jamaica, jerk, ackee, patty (most common in Toronto)
- trinidadian: Trini, doubles, bake-and-shark
- guyanese: Guyana, Guyanese (note: technically South America, but Toronto's Guyanese restaurants
  share dishes with Trinidad — roti, curry, doubles)
- haitian: Haiti, Port-au-Prince, griot, diri
- caribbean: generic Caribbean umbrella (Bahamian, Bajan, multi-island, "Caribbean Foods")

Middle East / Mediterranean:
- lebanese: Beirut, Lebanon, shawarma + manakish combo
- turkish: Istanbul, Turkish, doner, baklava-shop
- syrian: Damascus, Aleppo, Syrian
- persian: Iran, Tehran, Isfahan, Shiraz, kabab koobideh, joojeh, ghormeh
- middle_east: generic Mediterranean / Mid East umbrella (Mediterranean Grill, etc.)

Eastern European:
- ukrainian: Kyiv, Ukrainian, varenyky, borscht-specific
- russian: Moscow, Russian, blini
- hungarian: Budapest, Hungarian, goulash, paprikash
- eastern_eu: generic E.Euro umbrella (Romanian, Bulgarian, Czech, Polish-not-already-tagged)

Latin American:
- mexican: taqueria, taco, tortilleria, Oaxaca, mole, al pastor, mariachi
- salvadoran: Salvador, pupusas, El Salvador
- peruvian: Peru, Lima, ceviche, lomo saltado
- colombian: Colombia, Bogota, arepa, empanada-Colombian
- brazilian: Brazil, Sao Paulo, churrasco, açaí
- latin: generic Latin/Hispanic umbrella (Cuban, Venezuelan, etc.)

Other:
- jewish_deli: kosher, bagel shops, Ashkenazi/Israeli
- irish_uk: explicitly Irish or British pub/eatery (NOT every place named "Pub")
- italian: pizza/pasta/gelato/ristorante (chain pizza counts as italian)
- chinese: mainland/HK/Cantonese/Szechuan (not Korean BBQ, not Vietnamese)
- japanese: sushi, ramen, izakaya, Japanese
- korean: BBQ, kimchi, Seoul
- french: bistro, brasserie, patisserie, croissant
- greek: souvlaki, gyro, Greek
- portuguese: padaria, pastel, Portuguese
- polish: pierogi, Polish

CRITICAL — American fast-food chains and Canadian chains = unknown, NOT their themed cuisine:
- Popeyes Louisiana Kitchen → unknown (Cajun chain; NOT caribbean, NOT jamaican)
- KFC, KFC/Taco Bell combos → unknown (American chain; not mexican even with Taco Bell)
- Mary Brown's, Church's Chicken, Wendy's, A&W → unknown
- Applebee's, IHOP, Denny's, Outback, Boston Pizza → unknown
- Tim Hortons, Second Cup, Starbucks → unknown
- Subway, Mr. Sub, Quiznos → unknown
- A "themed" American chain inherits no ethnic bucket. Genuine ethnic chains (Pizza Hut for italian, Bento Sushi for japanese) are fine to tag by theme.

A generic name like "JIM'S BAR", "DOWNTOWN GRILL", or "MAIN STREET CAFE" with no ethnic signal = unknown.
A surname-only name like "PARK'S RESTAURANT" without other context = unknown (don't guess from a last name).

Packaged-food brands and food manufacturers with a retail counter at their factory
are NOT consumer restaurants — return unknown:
- "SHIMLA FOODS TAKE OUT", "PATEL BROTHERS", "ROMA FOODS" → unknown
- Names ending in "FOODS", "IMPORTS", "BRANDS", "DISTRIBUTORS" at industrial-zone
  addresses (Steeles, Caledonia, Dixie, etc.) → unknown
- A roti shop or butcher with hot samosas to order is fine — only flag pure
  manufacturer/distributor operations.

When uncertain between two specific buckets, pick the umbrella.
When uncertain whether there's any cultural signal at all, pick unknown."""

VALID_KEYS = {
    'italian','chinese','japanese','korean','vietnamese','filipino','thai','indonesian','malaysian','burmese',
    'cambodian','laotian',
    'south_asian','indian','pakistani','afghan','bangladeshi','tamil','tibetan','sri_lankan','nepalese',
    'caribbean','jamaican','trinidadian','guyanese','haitian','cuban','dominican',
    'greek','portuguese','polish','french','irish_uk','german','jewish_deli','spanish',
    'eastern_eu','ukrainian','russian','hungarian',
    'middle_east','lebanese','turkish','syrian','persian','israeli','egyptian','yemeni','armenian','georgian',
    'latin','mexican','salvadoran','peruvian','colombian','brazilian','argentinian','venezuelan',
    'african_horn','ethiopian','eritrean','somali',
    'african_west','nigerian','ghanaian','moroccan','senegalese',
    'unknown'
}

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
                'max_tokens': 16,
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
        text = ''.join(b.get('text','') for b in msg.get('content', []) if b.get('type')=='text').strip().lower()
        cuisine = None
        for tok in text.replace(',', ' ').split():
            t = tok.strip('.: \t\n')
            if t in VALID_KEYS:
                cuisine = t; break
        if cuisine is None: cuisine = 'unknown'
        cache[key] = {
            'status': 'ok',
            'cuisine': cuisine,
            'raw': text,
            'in_tok': usage.get('input_tokens', 0),
            'out_tok': usage.get('output_tokens', 0),
            'via': 'batch',
        }
        n_ok += 1

    CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    # Batch API is 50% off
    cost = (total_in/1e6 * 1.0 + total_out/1e6 * 5.0) * 0.5
    print(f"\nbatch merged: ok={n_ok} err={n_err}  tokens in={total_in:,} out={total_out:,}  est ${cost:.3f} (50%-off)")

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
