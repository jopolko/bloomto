#!/usr/bin/env python3
"""
LLM cuisine classification for ALL active food businesses licensed in the
last 365 days. Calls Claude Haiku 4.5 once per business with operating
name + address, returns one of 22 cuisine keys or 'unknown'. Caches.

Cost target: ~$0.40 for the full 2,000-business population.
Reads ANTHROPIC_API_KEY from /var/secrets/nowservingto.env.
"""
import os, sys, csv, json, time, threading
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed

class RateLimiter:
    """Shared across workers: cap to N requests in any rolling 60-second window."""
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
                    self.calls.append(now)
                    return
                sleep_for = 60 - (now - self.calls[0]) + 0.05
            time.sleep(max(0.05, sleep_for))

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / 'tools' / 'cache' / 'llm_cuisine_cache.json'
SECRETS = Path('/var/secrets/nowservingto.env')
CSV_PATH = '/tmp/bl1.csv'
MODEL = 'claude-haiku-4-5-20251001'

WINDOW_DAYS = 365
WORKERS = 4
RATE_PER_MINUTE = 45  # Tier 1 cap is 50 RPM for Haiku; leave headroom
CHECKPOINT_EVERY = 25
LIMITER = RateLimiter(RATE_PER_MINUTE)

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
FOOD_CATS = {
    'EATING OR DRINKING ESTABLISHMENT',
    'TAKE-OUT OR RETAIL FOOD ESTABLISHMENT',
    'EATING ESTABLISHMENT',
    'RETAIL STORE (FOOD)',
}

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
When uncertain between two specific buckets, pick the umbrella.
When uncertain whether there's any cultural signal at all, pick unknown."""

def load_api_key():
    if not SECRETS.exists(): sys.exit(f"missing {SECRETS}")
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if line.startswith('ANTHROPIC_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    sys.exit("ANTHROPIC_API_KEY not in secrets file")

API_KEY = load_api_key()

def classify_one(name, address, retries=3):
    LIMITER.acquire()
    body = json.dumps({
        'model': MODEL,
        'max_tokens': 16,
        'system': SYSTEM_PROMPT,
        'messages': [{
            'role': 'user',
            'content': f"Operating name: {name}\nAddress: {address}"
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
        with urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
    except HTTPError as e:
        if e.code == 429 and retries > 0:
            time.sleep(2.0)  # back off and retry
            return classify_one(name, address, retries - 1)
        return {'status': 'error', 'error': f'http {e.code}', 'detail': e.read().decode('utf-8')[:200]}
    except Exception as e:
        return {'status': 'error', 'error': str(e)[:200]}
    text = ''.join(c['text'] for c in resp.get('content', []) if c.get('type') == 'text').strip().lower()
    # Be forgiving: extract first known key from text
    cuisine = None
    for tok in text.replace(',', ' ').split():
        t = tok.strip('.: \t\n')
        if t in VALID_KEYS:
            cuisine = t; break
    if cuisine is None:
        cuisine = 'unknown'
    usage = resp.get('usage', {})
    return {
        'status': 'ok',
        'cuisine': cuisine,
        'raw': text,
        'in_tok': usage.get('input_tokens', 0),
        'out_tok': usage.get('output_tokens', 0),
    }

def parse_d(s):
    if not s: return None
    s = s.strip()
    for fmt in ('%Y-%m-%d','%Y/%m/%d','%m/%d/%Y','%Y-%m-%dT%H:%M:%S'):
        try: return datetime.strptime(s.split(' ')[0], fmt).date()
        except ValueError: pass
    return None

def main():
    today = date.today()
    cutoff = today - timedelta(days=WINDOW_DAYS)

    # Load existing cache
    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    print(f"cache: {len(cache)} entries already classified")

    # Collect target rows from the CSV
    targets = []
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
            addr1 = (row.get('Licence Address Line 1') or '').strip()
            addr3 = (row.get('Licence Address Line 3') or '').strip()
            address = (addr1 + ' ' + addr3).strip() or '—'
            key = f"{name.upper()}||{address.upper()}"
            # Skip only entries that succeeded last time; retry errors
            existing = cache.get(key)
            if existing and existing.get('status') == 'ok': continue
            targets.append((key, name, address))

    print(f"to classify: {len(targets)} (skipping {len(cache)} cached)")
    if not targets:
        print("nothing to do.")
        return

    # Run in parallel
    t0 = time.time()
    total_in = total_out = 0
    done = 0
    write_lock_counter = [0]

    def worker(item):
        key, name, address = item
        res = classify_one(name, address)
        return key, name, address, res

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(worker, t): t for t in targets}
        for fut in as_completed(futures):
            try:
                key, name, address, res = fut.result()
            except Exception as e:
                print(f"  worker exception: {e}")
                continue
            cache[key] = res
            done += 1
            if res.get('status') == 'ok':
                total_in += res.get('in_tok', 0)
                total_out += res.get('out_tok', 0)
            if done % CHECKPOINT_EVERY == 0 or done == len(targets):
                CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))
                el = time.time() - t0
                rate = done / max(el, 0.1)
                # Haiku 4.5 pricing: $1/M input, $5/M output (estimate)
                est_cost = total_in / 1e6 * 1.0 + total_out / 1e6 * 5.0
                print(f"  [{done:>4}/{len(targets)}]  {rate:.1f}/s  tokens in={total_in:,} out={total_out:,}  est ${est_cost:.3f}  ({el:.0f}s)")

    CACHE_PATH.write_text(json.dumps(cache, separators=(',', ':')))

    # Final breakdown
    print()
    breakdown = {}
    for k, v in cache.items():
        if v.get('status') != 'ok': continue
        c = v.get('cuisine', 'unknown')
        breakdown[c] = breakdown.get(c, 0) + 1
    total = sum(breakdown.values())
    print(f"== Cuisine distribution ({total} total) ==")
    for c, n in sorted(breakdown.items(), key=lambda x: -x[1]):
        print(f"  {c:14s} {n:>5}  ({n*100/total:.1f}%)")

if __name__ == '__main__':
    main()
