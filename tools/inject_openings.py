#!/usr/bin/env python3
"""
One-off: read existing data/corridors.json, compute citywide cuisine-tagged
NEW OPENINGS (Issued in last 365 days, no Cancel Date) from /tmp/bl1.csv, and
inject under key 'newOpenings'. Mirrors logic that will live in build_corridors.py.
"""
import csv, json
from datetime import datetime, date, timedelta
from collections import defaultdict

REFERENCE_DATE = date.today()  # use real today; build_corridors.py uses its own TODAY constant
import os
# Derive repo root from this script's location so the same code works on dev (WSL) and prod (VPS).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = '/tmp/business_licences_alt.csv'  # shared with build_corridors.py
LLM_CACHE_PATH = f'{ROOT}/tools/cache/llm_cuisine_cache.json'
PLACES_CACHE_PATH = f'{ROOT}/tools/cache/places_cache.json'
WEB_VERIFY_CACHE_PATH = f'{ROOT}/tools/cache/web_verify_cache.json'
DATA_PATH = f'{ROOT}/data/corridors.json'

# Load LLM cache for cuisine override
try:
    LLM_CACHE = json.load(open(LLM_CACHE_PATH))
except FileNotFoundError:
    LLM_CACHE = {}
try:
    PLACES_CACHE = json.load(open(PLACES_CACHE_PATH))
except FileNotFoundError:
    PLACES_CACHE = {}
try:
    WEB_VERIFY_CACHE = json.load(open(WEB_VERIFY_CACHE_PATH))
except FileNotFoundError:
    WEB_VERIFY_CACHE = {}
try:
    URL_HEALTH_CACHE = json.load(open(f'{ROOT}/tools/cache/url_health_cache.json'))
except FileNotFoundError:
    URL_HEALTH_CACHE = {}

def url_is_alive(url):
    """True if URL not in health cache, or last check said ok. False if known-broken."""
    if not url: return False
    h = URL_HEALTH_CACHE.get(url)
    if not h: return True   # never checked → optimistic
    return bool(h.get('ok'))
WINDOW_365 = REFERENCE_DATE - timedelta(days=365)
WINDOW_30  = REFERENCE_DATE - timedelta(days=30)

CUISINE_PATTERNS = {
    'caribbean':     ['ROTI','JERK','CARIBBEAN','JAMAICAN','JAMAICA','ACKEE','PATTY','OXTAIL','PLANTAIN','RASTAFAR','RIDDIM','IRIE','RUDIE','REGGAE','ISLAND'],
    'south_asian':   ['TANDOORI','MASALA','BIRYANI','TIKKA','NAAN','BHATURA','PANEER','DOSA','IDLI','PUNJABI','KARAHI','SAMOSA','KABAB','KEBAB','INDIA','INDIAN','BHOJAN','THALI'],
    'chinese':       ['WOK','CHINESE','CHINA','DIM SUM','SZECHUAN','SICHUAN','HUNAN','CANTONESE','HONG KONG','CHOPSTICK','BAO','BUBBLE TEA','MANDARIN','HOUSE OF NOODLE','MISTER WOK','BAMBOO','MAJESTIC'],
    'vietnamese':    ['PHO','BANH MI','BUN ','VIETNAMESE','SAIGON','HANOI','VIETNAM'],
    'korean':        ['KIMCHI','KOREAN','BIBIMBAP','GANGNAM','SEOUL','HANSAM'],
    'italian':       ['PIZZA','PIZZERIA','PASTA','RISTORANTE','TRATTORIA','GELATERIA','GELATO','ITALIANO','ITALIA','NAPOLI','MILANO','VERONA','TOSCANA'],
    'portuguese':    ['PADARIA','PORTUGUESA','PORTUGUESE','PASTEL','BACALAU','PORTUGAL','LISBOA','PORTO'],
    'greek':         ['SOUVLAKI','GYRO','GREEK','HELLENIC','ATHENS','OLYMPIA','MYKONOS','SANTORINI','ZORBA','KEFI'],
    'japanese':      ['SUSHI','RAMEN','IZAKAYA','JAPANESE','TOKYO','OSAKA','SAKURA','TERIYAKI','SAKE','TONKATSU','UDON','SOBA'],
    'filipino':      ['ADOBO','LECHON','KAINAN','FILIPINO','PINOY','MANILA','PINAS','TAGALOG','SARISAR','TANGKE'],
    'tibetan':       ['MOMO','TIBETAN','TIBET','LHASA','HIMALAY','SHANGRI'],
    'african_horn':  ['ETHIOPIAN','INJERA','ERITREAN','SOMALI','HABESHA','ADDIS','ASMARA','MOGADISHU','HARGEISA'],
    'african_west':  ['NIGERIAN','GHANAIAN','SENEGAL','MALI','AFRICAN','SUYA','JOLLOF','EGUSI','FUFU'],
    'latin':         ['TACO','TAQUERIA','EMPANADA','SALVADOR','LATINO','LATINA','MEXICAN','MEXICO','PERUVIAN','COLOMBIAN','VENEZUELAN','PUPUSAS','CHURRO','ASADO'],
    'polish':        ['POLSKI','POLSKA','PIEROGI','POLISH','KRAKOW','WARSZAW','KIELBASA'],
    'middle_east':   ['SHAWARMA','FALAFEL','LEBANESE','SYRIAN','ARABIAN','PERSIAN','IRANIAN','TURKISH','ANATOLIA','BEIRUT','MEDITERRAN'],
    'tamil':         ['TAMIL','EELAM','JAFFNA','CHENNAI','SRI ','MADRAS','KOTHU'],
    'irish_uk':      ['IRISH','DUBLIN','CELTIC','KILKENNY','SCOTTISH','HIGHLAND','LONDON','BRITISH','CHIPS & FISH','FISH & CHIPS','PUB'],
    'french':        ['FRENCH','BISTRO','BRASSERIE','BOULANGERIE','PATISSERIE','CHEZ ','LE PARIS','LYON','MARSEILLE','CROISSANT'],
    'german':        ['GERMAN','BIERGARTEN','WURST','BAVARIA','SCHWARZWALD','OKTOBER','BRATWURST','SCHNITZEL'],
    'jewish_deli':   ['KOSHER','BAGEL','KNISH','SHTETL','SHWARTZ','UNITED BAKERS','MATZO','YIDDISH','SCHWARTZS'],
    'eastern_eu':    ['UKRAINIAN','RUSSIAN','BULGARIAN','HUNGARIAN','ROMANIAN','BORSCHT','PEROGY','PYROGY','VARENY','KYIV','KIEV','ODESA','PRAGUE','GOULASH','CZECH'],
}
CUISINE_LABEL = {
    'italian':'Italian','chinese':'Chinese','japanese':'Japanese','korean':'Korean',
    'vietnamese':'Vietnamese','filipino':'Filipino','thai':'Thai',
    'indonesian':'Indonesian','malaysian':'Malaysian','burmese':'Burmese',
    'south_asian':'South Asian (other)','pakistani':'Pakistani','afghan':'Afghan',
    'bangladeshi':'Bangladeshi','tamil':'Tamil','tibetan':'Tibetan',
    'caribbean':'Caribbean (other)','jamaican':'Jamaican','trinidadian':'Trinidadian','guyanese':'Guyanese','haitian':'Haitian',
    'greek':'Greek','portuguese':'Portuguese','polish':'Polish','french':'French',
    'irish_uk':'Irish/UK','german':'German','jewish_deli':'Jewish deli',
    'eastern_eu':'Eastern European (other)','ukrainian':'Ukrainian','russian':'Russian','hungarian':'Hungarian',
    'middle_east':'Middle Eastern (other)','lebanese':'Lebanese','turkish':'Turkish','syrian':'Syrian','persian':'Persian',
    'latin':'Latin American (other)','mexican':'Mexican','salvadoran':'Salvadoran','peruvian':'Peruvian','colombian':'Colombian','brazilian':'Brazilian',
    'african_horn':'East African (other)','ethiopian':'Ethiopian','eritrean':'Eritrean','somali':'Somali',
    'african_west':'West African (other)','nigerian':'Nigerian','ghanaian':'Ghanaian','moroccan':'Moroccan',
}
FOOD_CATS = {
    'EATING OR DRINKING ESTABLISHMENT',
    'TAKE-OUT OR RETAIL FOOD ESTABLISHMENT',
    'EATING ESTABLISHMENT',
    'RETAIL STORE (FOOD)',
}

def parse_d(s):
    if not s: return None
    s = s.strip()
    for fmt in ('%Y-%m-%d','%Y/%m/%d','%m/%d/%Y','%Y-%m-%dT%H:%M:%S'):
        try: return datetime.strptime(s.split(' ')[0], fmt).date()
        except ValueError: pass
    return None

# Chain denylist: substring match against UPPERCASE operating name. If any of these appears,
# force cuisine to None regardless of what the LLM said. Cheap, deterministic safety net.
# Add new chains to this list as you spot them.
CHAIN_DENYLIST = (
    'POPEYES', 'POPEYE\'S', 'KFC', 'CHURCH\'S CHICKEN', 'CHURCHS CHICKEN', 'MARY BROWN',
    'WENDY', 'BURGER KING', 'MCDONALD', 'HARVEY', 'A&W', 'TIM HORTON', 'COFFEE TIME',
    'SECOND CUP', 'STARBUCKS', 'TIMOTHY\'S COFFEE',
    'SUBWAY', 'MR. SUB', 'MR SUB', 'QUIZNOS', 'EXTREME PITA', 'PITA PIT',
    'APPLEBEE', 'OUTBACK', 'IHOP', 'DENNY', 'JACK ASTOR', 'SCORES', 'KELSEY',
    'MONTANA', 'EAST SIDE MARIO', 'BOSTON PIZZA', 'PIZZA NOVA', 'PIZZA PIZZA',
    'PIZZAVILLE', 'LITTLE CAESAR', 'PAPA JOHN', 'DOMINO',
    'CHIPOTLE', 'TACO BELL', 'TACO TIME',
    'DAIRY QUEEN', 'BASKIN-ROBBIN', 'BASKIN ROBBIN',
    'SWISS CHALET', 'ST-HUBERT', 'WHITE SPOT',
    'DOLLARAMA', 'SHOPPERS DRUG MART', '7-ELEVEN', 'CIRCLE K', 'COUCHE-TARD',
    'FRESHCO', 'METRO', 'SOBEYS', 'LOBLAWS', 'NO FRILLS', 'COSTCO', 'WALMART',
    'BENTO SUSHI',  # chain, even though Japanese — too sprawling to be useful as "newest" signal
    'FAT BASTARD BURRITO',  # Canadian chain themed as Mexican
)

def is_chain(name_upper):
    return any(c in name_upper for c in CHAIN_DENYLIST)

def keyword_classify(op_upper):
    for cuisine, keys in CUISINE_PATTERNS.items():
        for k in keys:
            if k in op_upper: return cuisine
    return None

VALID_LLM_KEYS = set(CUISINE_LABEL.keys())  # every key with a display label is valid
CUISINE_LABEL.setdefault('thai', 'Thai')

def get_cuisine(name, address):
    """Priority: web_verify (search-informed) > LLM name-only > keyword.
    Web_verify wins because it has actual web evidence (menus, owner bios, reviews),
    not just the operating name. Name-only LLM is the fallback when search hasn't run.
    Chain denylist short-circuits everything — chains are never an ethnic-cuisine signal.
    """
    name_upper = (name or '').strip().upper()
    if is_chain(name_upper):
        return None, None  # chain → never shown
    key = f"{name_upper}||{(address or '').strip().upper()}"
    # Otherwise consult caches in priority order

    # 1. Web-verified cuisine (search-informed; richest signal)
    w = WEB_VERIFY_CACHE.get(key)
    if w and w.get('status') == 'ok' and w.get('cuisine'):
        c = w['cuisine']
        if c == 'unknown': return None, None
        if c in VALID_LLM_KEYS: return c, 'web_search'
    # 2. Name-only LLM classification
    llm = LLM_CACHE.get(key)
    if llm and llm.get('status') == 'ok':
        c = llm.get('cuisine')
        if c == 'unknown': return None, None
        if c in VALID_LLM_KEYS: return c, 'llm'
    # 3. Keyword fallback
    kw = keyword_classify((name or '').upper())
    if kw: return kw, 'keyword'
    return None, None

def verification_for(name, address):
    """Returns dict of fields to merge if verified-open, else None. Drops the
    website field when url_health_cache reports it as broken."""
    key = f"{(name or '').strip().upper()}||{(address or '').strip().upper()}"
    # Source 1: Google Places
    p = PLACES_CACHE.get(key)
    if p and p.get('status') == 'ok':
        bs = p.get('businessStatus')
        if bs == 'OPERATIONAL':
            out = {'businessStatus': bs, 'verifiedBy': 'places'}
            for k in ('website', 'mapsUrl', 'rating', 'reviewCount', 'matchedName', 'lat', 'lng'):
                if p.get(k) is not None: out[k] = p[k]
            if out.get('website') and not url_is_alive(out['website']):
                del out['website']  # let mapsUrl be the link instead
            return out
        if bs in ('CLOSED_TEMPORARILY', 'CLOSED_PERMANENTLY'):
            return None
    # Source 2: Web-search verification
    w = WEB_VERIFY_CACHE.get(key)
    if w and w.get('status') == 'ok' and w.get('operating') == 'yes':
        out = {'businessStatus': 'OPERATIONAL', 'verifiedBy': 'web_search'}
        if w.get('website') and url_is_alive(w['website']):
            out['website'] = w['website']
        if p and p.get('status') == 'ok':
            for k in ('mapsUrl', 'rating', 'reviewCount', 'matchedName', 'lat', 'lng'):
                if p.get(k) is not None: out.setdefault(k, p[k])
        return out
    return None

opens_365_by_cuisine = defaultdict(list)
n_food_active = 0; n_food_active_365 = 0; n_tagged_365 = 0; n_tagged_30 = 0
n_dropped_unverified = 0; n_dropped_closed = 0

with open(CSV_PATH, encoding='utf-8', errors='replace') as f:
    rdr = csv.DictReader(f)
    for row in rdr:
        cat = (row.get('Category') or '').strip()
        if cat not in FOOD_CATS: continue
        if (row.get('Cancel Date') or '').strip(): continue  # active only
        n_food_active += 1
        iss = parse_d(row.get('Issued'))
        if not iss or iss < WINDOW_365: continue
        n_food_active_365 += 1
        op_raw = (row.get('Operating Name') or '').strip()
        if not op_raw: continue
        addr1 = (row.get('Licence Address Line 1') or '').strip()
        addr3 = (row.get('Licence Address Line 3') or '').strip()
        address_full = (addr1 + ' ' + addr3).strip()
        cuisine, source = get_cuisine(op_raw, address_full)
        if not cuisine: continue

        # Verification gate: Places=OPERATIONAL OR web_search verified-yes.
        verification = verification_for(op_raw, address_full)
        if verification is None:
            n_dropped_unverified += 1
            continue

        n_tagged_365 += 1
        if iss >= WINDOW_30: n_tagged_30 += 1
        days_open = max(0, (REFERENCE_DATE - iss).days)
        entry = {
            'operatingName': op_raw,
            'cuisine': cuisine,
            'cuisineSource': source,
            'issuedDate': iss.isoformat(),
            'daysOpen': days_open,
            'address': address_full,
        }
        entry.update({k: v for k, v in verification.items() if v is not None})
        opens_365_by_cuisine[cuisine].append(entry)

print(f"  verification gate: kept {n_tagged_365}, dropped {n_dropped_unverified} unverified + {n_dropped_closed} closed/temp")

# Sort each cuisine's list by issued date desc (newest first)
for c in opens_365_by_cuisine:
    opens_365_by_cuisine[c].sort(key=lambda r: r['issuedDate'], reverse=True)

# Summary per cuisine
cuisines_out = []
for c, entries in opens_365_by_cuisine.items():
    cuisines_out.append({
        'key': c,
        'label': CUISINE_LABEL.get(c, c),
        'count365d': len(entries),
        'count30d': sum(1 for e in entries if e['daysOpen'] <= 30),
        'newest': entries[0],          # the absolute newest one
        'recent5': entries[:10],        # for per-cuisine card (bumped to 10, key kept for back-compat)
    })
cuisines_out.sort(key=lambda r: -r['count365d'])

# Flat feed: all openings, newest first, cap 80
all_recent = []
for c, entries in opens_365_by_cuisine.items():
    all_recent.extend(entries)
all_recent.sort(key=lambda r: r['issuedDate'], reverse=True)
all_recent = all_recent[:300]

# Inject
data = json.load(open(DATA_PATH))
data['newOpenings'] = {
    'asOf': REFERENCE_DATE.isoformat(),
    'windowDays': 365,
    'totalActiveScanned': n_food_active,
    'totalNewActive365d': n_food_active_365,
    'totalTagged365d': n_tagged_365,
    'totalTagged30d': n_tagged_30,
    'tagRate365d': round(n_tagged_365 / max(n_food_active_365, 1) * 100, 1),
    'cuisines': cuisines_out,
    'recent': all_recent,
}
with open(DATA_PATH, 'w') as f:
    json.dump(data, f, separators=(',', ':'))

print(f"Injected newOpenings into {DATA_PATH}")
print(f"  {n_food_active_365:,} active food licences issued in last 365d")
print(f"  {n_tagged_365:,} cuisine-tagged ({data['newOpenings']['tagRate365d']}%)")
print(f"  {len(cuisines_out)} cuisines with at least 1 new opening")
print(f"  {n_tagged_30:,} tagged openings in last 30 days")
print()
print("Top cuisines by 12-month new-opening count:")
for c in cuisines_out[:12]:
    print(f"  {c['label']:20s} {c['count365d']:>4} new (last 30d: {c['count30d']})   newest: {c['newest']['operatingName'][:42]}")
