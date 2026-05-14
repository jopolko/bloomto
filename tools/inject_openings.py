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
GEOCODE_CACHE_PATH = f'{ROOT}/tools/cache/geocode_cache.json'
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
    GEOCODE_CACHE = json.load(open(GEOCODE_CACHE_PATH))
except FileNotFoundError:
    GEOCODE_CACHE = {}
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
    'south_asian':'South Asian','indian':'Indian','pakistani':'Pakistani','afghan':'Afghan',
    'bangladeshi':'Bangladeshi','tamil':'Tamil','tibetan':'Tibetan',
    'caribbean':'Caribbean','jamaican':'Jamaican','trinidadian':'Trinidadian','guyanese':'Guyanese','haitian':'Haitian',
    'greek':'Greek','portuguese':'Portuguese','polish':'Polish','french':'French',
    'irish_uk':'Irish/UK','german':'German','jewish_deli':'Jewish deli',
    'eastern_eu':'Eastern European','ukrainian':'Ukrainian','russian':'Russian','hungarian':'Hungarian',
    'middle_east':'Middle Eastern','lebanese':'Lebanese','turkish':'Turkish','syrian':'Syrian','persian':'Persian',
    'latin':'Latin American','mexican':'Mexican','salvadoran':'Salvadoran','peruvian':'Peruvian','colombian':'Colombian','brazilian':'Brazilian',
    'african_horn':'East African','ethiopian':'Ethiopian','eritrean':'Eritrean','somali':'Somali',
    'african_west':'West African','nigerian':'Nigerian','ghanaian':'Ghanaian','moroccan':'Moroccan',
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

# Map Toronto FSA (first 2 chars of postal) → former municipality / district.
# Toronto's pre-1998 boroughs get treated as natural orientation anchors. Roughly:
#   M1 = Scarborough, M2/M3 = North York, M4 = East York / midtown east,
#   M5 = Downtown, M6 = West Toronto / York, M8/M9 = Etobicoke
DISTRICT_BY_FSA = {
    'M1': 'Scarborough', 'M2': 'North York', 'M3': 'North York',
    'M4': 'East Toronto', 'M5': 'Downtown',  'M6': 'West Toronto',
    'M7': 'Downtown',    'M8': 'Etobicoke',  'M9': 'Etobicoke',
}
def district_from_postal(addr_with_postal):
    """Pull the first 2 chars of a Toronto postal code from any address string."""
    import re
    m = re.search(r'\bM[0-9][A-Z]\b', (addr_with_postal or '').upper())
    if not m: return None
    return DISTRICT_BY_FSA.get(m.group(0)[:2])

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
    'PIZZAVILLE', 'LITTLE CAESAR', 'PAPA JOHN', 'DOMINO', 'PIZZA HUT', '241 PIZZA',
    'MUCHO BURRITO', 'BAR BURRITO', 'BURRITO BOYZ',
    'THAI EXPRESS', 'EDO JAPAN', 'BENTO BENTO', 'FRESHII', 'BOOSTER JUICE',
    'SECOND CUP', 'SMOKE\'S POUTINERIE', 'SMOKES POUTINERIE',
    'HERO BURGER', 'HERO CERTIFIED', 'FIVE GUYS', 'NEW YORK FRIES',
    'CHIPOTLE', 'TACO BELL', 'TACO TIME',
    'DAIRY QUEEN', 'BASKIN-ROBBIN', 'BASKIN ROBBIN',
    'SWISS CHALET', 'ST-HUBERT', 'WHITE SPOT',
    'DOLLARAMA', 'SHOPPERS DRUG MART', '7-ELEVEN', 'CIRCLE K', 'COUCHE-TARD',
    'FRESHCO', 'METRO', 'SOBEYS', 'LOBLAWS', 'NO FRILLS', 'COSTCO', 'WALMART',
    'BENTO SUSHI',  # chain, even though Japanese — too sprawling to be useful as "newest" signal
    'FAT BASTARD BURRITO',  # Canadian chain themed as Mexican
)

import re as _re
def is_chain(name_upper):
    """Match chain names ONLY at the start of the operating name. Chains typically
    appear as 'CHAIN' or 'CHAIN LOCATION' or 'CHAIN #123'. This avoids false positives
    like 'OM MA JOHN'S PIZZA & THAI EXPRESS' being matched as the chain 'THAI EXPRESS'."""
    n = (name_upper or '').strip()
    for c in CHAIN_DENYLIST:
        # Match at start, followed by word boundary, end-of-string, or common location separators
        if _re.match(r'^' + _re.escape(c) + r'(\b|$|[/#@,])', n):
            return True
    return False

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
    website field when url_health_cache reports it as broken. Coords come from
    Places when available, else from the Nominatim geocode cache."""
    name_up = (name or '').strip().upper()
    addr_up = (address or '').strip().upper()
    key = f"{name_up}||{addr_up}"
    # Geocode cache is keyed by street address only (no postal code). Try the
    # full key first, then a stripped-postal fallback to match older cache entries.
    addr_no_postal = _re.sub(r'\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d$', '', addr_up)
    geo = GEOCODE_CACHE.get(key) or GEOCODE_CACHE.get(f"{name_up}||{addr_no_postal}")
    geo_coords = (geo.get('lat'), geo.get('lng')) if (geo and geo.get('lat') and geo.get('lng')) else (None, None)
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
            if out.get('lat') is None and geo_coords[0] is not None:
                out['lat'], out['lng'] = geo_coords
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
        if out.get('lat') is None and geo_coords[0] is not None:
            out['lat'], out['lng'] = geo_coords
        return out
    return None

from urllib.parse import quote_plus
# Dedupe by (operating_name, street_address). When Toronto's MLS issues two licence rows
# for the same physical business (e.g. "Take-Out" + "Eating Establishment" categories, or
# a renewed licence overlapping the old one), we want one entry. Keep the EARLIEST
# Issued date — that's when the kitchen actually opened, not just when a category was added.
seen_entries = {}
n_food_active = 0; n_food_active_365 = 0; n_tagged_365 = 0; n_tagged_30 = 0
n_dropped_unverified = 0; n_dropped_closed = 0; n_deduped = 0

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

        # Build candidate entry
        days_open = max(0, (REFERENCE_DATE - iss).days)
        fallback_maps = f"https://www.google.com/maps/search/?api=1&query={quote_plus(op_raw + ' ' + addr1 + ' Toronto')}"
        # Stable, URL-safe slug — kebab-case the name + leading address number for
        # disambiguation across multi-location chains/branches.
        name_part = _re.sub(r'[^\w\s-]', '', op_raw or '').strip().lower()
        name_part = _re.sub(r'[\s_]+', '-', name_part).strip('-')
        addr_num_m = _re.match(r'^(\d+)', (addr1 or '').strip())
        addr_num = addr_num_m.group(1) if addr_num_m else ''
        slug = (name_part + (f'-{addr_num}' if addr_num else ''))[:80]
        entry = {
            'operatingName': op_raw,
            'cuisine': cuisine,
            'cuisineSource': source,
            'issuedDate': iss.isoformat(),
            'daysOpen': days_open,
            'address': addr1,
            'slug': slug,
            'fallbackMapsUrl': fallback_maps,
        }
        district = district_from_postal(address_full)
        if district: entry['district'] = district
        entry.update({k: v for k, v in verification.items() if v is not None})

        # Dedupe by (name_upper, addr_upper). Keep EARLIEST issuedDate.
        dedup_key = (op_raw.upper(), addr1.upper())
        existing = seen_entries.get(dedup_key)
        if existing is None:
            seen_entries[dedup_key] = entry
        else:
            n_deduped += 1
            if iss.isoformat() < existing['issuedDate']:
                seen_entries[dedup_key] = entry  # this row is earlier — keep it

# Now bucket the deduped entries by cuisine and compute counts
opens_365_by_cuisine = defaultdict(list)
for entry in seen_entries.values():
    n_tagged_365 += 1
    if entry['daysOpen'] <= 30: n_tagged_30 += 1
    opens_365_by_cuisine[entry['cuisine']].append(entry)

print(f"  verification gate: kept {n_tagged_365}, dropped {n_dropped_unverified} unverified + {n_dropped_closed} closed/temp + {n_deduped} duplicate rows collapsed")

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
all_recent = all_recent[:1500]  # large enough to include all 365-day verified-open

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

# ── SEO/LLM-EO injection: sitemap + index.html static-feed + JSON-LD ItemList ──
# Mirrors the dynamic feed for crawlers and no-JS visitors. Re-runs every cron.
SITE_BASE = 'https://nowservingto.com'
SITEMAP_PATH = f'{ROOT}/sitemap.xml'
INDEX_PATH = f'{ROOT}/index.html'

# Python-side cuisine palette mirrors the one in index.html. Used to color the pre-rendered
# static cuisine pills so crawlers see proper structured visual styling too.
PALETTE_HEX = {
    'italian':'#c83624','caribbean':'#1a8a5a','south_asian':'#d4a017','indian':'#e88e2c',
    'pakistani':'#a06030','afghan':'#7a5d3a','bangladeshi':'#b88820','chinese':'#b13e6a',
    'vietnamese':'#4a8b8b','japanese':'#2f3aa3','korean':'#6b2456','filipino':'#e08226',
    'tamil':'#8a5d20','tibetan':'#b15a25','greek':'#1f7a6a','portuguese':'#9b2538',
    'polish':'#4a5a6a','french':'#5a3a7a','irish_uk':'#2a6a40','german':'#6a5a30',
    'jewish_deli':'#4a4a8a','eastern_eu':'#7a4a4a','ukrainian':'#6a5a8a','russian':'#7a4a4a',
    'hungarian':'#8a5050','middle_east':'#b87a25','lebanese':'#c89538','turkish':'#a8662a',
    'syrian':'#9b5520','persian':'#8a4a25','latin':'#cc4a4a','mexican':'#d63d2a',
    'salvadoran':'#c8553a','peruvian':'#b35b50','colombian':'#cc6248','brazilian':'#3d8a47',
    'african_horn':'#a0522d','ethiopian':'#a0522d','eritrean':'#8a4528','somali':'#b06530',
    'african_west':'#5a8a3a','nigerian':'#4a7a30','ghanaian':'#6a8a40','moroccan':'#b87a2a',
    'jamaican':'#1f7a4a','trinidadian':'#2a9560','guyanese':'#3a8060','haitian':'#1a6855',
    'thai':'#7a8a3a','indonesian':'#7a6a40','malaysian':'#5a7a55','burmese':'#8a7050',
}

def _esc(s):
    """HTML-escape a string."""
    if s is None: return ''
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))

def _ago(days):
    if days <= 1: return 'licensed today'
    if days <= 60: return f'licensed {days}d ago'
    if days <= 365: return f'licensed {round(days/30)}mo ago'
    return f'licensed {days/365:.1f}y ago'

# Build static HTML rows for the top 30 newest verified-open entries.
top_for_static = all_recent[:30]
static_rows_html = []
for r in top_for_static:
    color = PALETTE_HEX.get(r.get('cuisine'), '#777')
    label = CUISINE_LABEL.get(r.get('cuisine'), r.get('cuisine') or '')
    name = _esc(r['operatingName'])
    addr = _esc(r.get('address') or '')
    district = _esc(r.get('district') or '')
    addr_html = f'{addr}<span class="oad-d"> · {district}</span>' if district else addr
    issued = _esc(r['issuedDate'])
    ago = _esc(_ago(r['daysOpen']))
    link = r.get('website') or r.get('mapsUrl') or r.get('fallbackMapsUrl') or ''
    # Same-tab navigation — back button cleanly returns to NowServingTO (target="_blank"
    # on mobile would strand users on the Maps tab when they bounce back from the app).
    name_html = f'<a href="{_esc(link)}" rel="noopener">{name}</a>' if link else name
    static_rows_html.append(
        f'<div class="open-row">'
        f'<div class="od">{issued}<span class="ago">{ago}</span></div>'
        f'<div class="on">{name_html}<span class="oad">{addr_html}</span></div>'
        f'<div class="oc"><span class="pill" style="background:{color}">{_esc(label)}</span></div>'
        f'</div>'
    )
static_block = '\n    '.join(static_rows_html)

# Build JSON-LD ItemList — top 30 entries as Restaurant items
ld_items = []
for i, r in enumerate(top_for_static, 1):
    rest = {
        '@type': 'Restaurant',
        'name': r['operatingName'],
        'address': {'@type': 'PostalAddress', 'streetAddress': r.get('address') or '', 'addressLocality': 'Toronto', 'addressRegion': 'ON', 'addressCountry': 'CA'},
        'servesCuisine': CUISINE_LABEL.get(r.get('cuisine'), r.get('cuisine') or ''),
        'dateOpened': r.get('issuedDate'),
    }
    if r.get('website'): rest['url'] = r['website']
    if r.get('rating'): rest['aggregateRating'] = {'@type': 'AggregateRating', 'ratingValue': r['rating'], 'reviewCount': r.get('reviewCount') or 1}
    ld_items.append({'@type': 'ListItem', 'position': i, 'item': rest})
ld_payload = {
    '@context': 'https://schema.org',
    '@type': 'ItemList',
    'name': "Toronto's newest restaurants by cuisine",
    'description': 'Restaurants newly licensed in Toronto in the past 365 days, classified by cuisine.',
    'itemListElement': ld_items,
}
ld_json_str = json.dumps(ld_payload, separators=(',', ':'))

# Rewrite the index.html markers in place
import re
try:
    html = open(INDEX_PATH).read()
    html = re.sub(
        r'(<!-- STATIC-FEED-START[^>]*-->).*?(<!-- STATIC-FEED-END -->)',
        f'\\1\n    {static_block}\n    \\2',
        html, count=1, flags=re.DOTALL,
    )
    # Markers MUST sit outside <script type="application/ld+json"> so the script
    # content stays pure JSON (Google's structured-data parser rejects HTML
    # comments inside the script body).
    ld_script_block = f'<script type="application/ld+json" id="ld-itemlist">{ld_json_str}</script>'
    html = re.sub(
        r'(<!-- LD-ITEMLIST-START -->).*?(<!-- LD-ITEMLIST-END -->)',
        f'\\1\n{ld_script_block}\n\\2',
        html, count=1, flags=re.DOTALL,
    )
    open(INDEX_PATH, 'w').write(html)
    print(f"  pre-rendered {len(top_for_static)} static feed rows + JSON-LD ItemList into index.html")
except Exception as e:
    print(f"  WARN: index.html injection failed: {e}")

# Write sitemap.xml with today's lastmod + one URL per cuisine landing page so
# Google indexes "newest ethiopian toronto" etc. separately from the home page.
url_blocks = [
    f'  <url>\n    <loc>{SITE_BASE}/</loc>\n    <lastmod>{REFERENCE_DATE.isoformat()}</lastmod>\n    <changefreq>daily</changefreq>\n    <priority>1.0</priority>\n  </url>'
]
for c in cuisines_out:
    # Sitemap every cuisine with at least 1 verified opening. Smaller cuisines
    # often have the BEST ranking opportunity ("newest Eritrean Toronto" has
    # almost no competing content), and excluding under-represented communities
    # would contradict the project ethos.
    if c.get('count365d', 0) < 1: continue
    url_blocks.append(
        f'  <url>\n    <loc>{SITE_BASE}/cuisine/{c["key"]}</loc>\n    <lastmod>{REFERENCE_DATE.isoformat()}</lastmod>\n    <changefreq>daily</changefreq>\n    <priority>0.8</priority>\n  </url>'
    )
sitemap = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    + '\n'.join(url_blocks) + '\n'
    '</urlset>\n'
)
with open(SITEMAP_PATH, 'w') as f: f.write(sitemap)
print(f"  wrote sitemap.xml ({len(url_blocks)} URLs)")

print(f"Injected newOpenings into {DATA_PATH}")
print(f"  {n_food_active_365:,} active food licences issued in last 365d")
print(f"  {n_tagged_365:,} cuisine-tagged ({data['newOpenings']['tagRate365d']}%)")
print(f"  {len(cuisines_out)} cuisines with at least 1 new opening")
print(f"  {n_tagged_30:,} tagged openings in last 30 days")
print()
print("Top cuisines by 12-month new-opening count:")
for c in cuisines_out[:12]:
    print(f"  {c['label']:20s} {c['count365d']:>4} new (last 30d: {c['count30d']})   newest: {c['newest']['operatingName'][:42]}")
