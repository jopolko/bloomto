#!/usr/bin/env python3
"""
One-off: read existing data/corridors.json, compute citywide cuisine-tagged
NEW OPENINGS (Issued in last 365 days, no Cancel Date) from /tmp/bl1.csv, and
inject under key 'newOpenings'. Mirrors logic that will live in build_corridors.py.
"""
import csv, json
from datetime import datetime, date, timedelta
from collections import defaultdict
from pathlib import Path

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

# (CUISINE_PATTERNS regex-keyword dictionary removed 2026-05-14 — it was a
# pre-LLM fallback that pattern-matched operating names to cuisines. Now that
# every entry passes through name-only Haiku in llm_classify_batch.py, the
# regex layer is duplicative AND coarser — Haiku reads "Jollof King" + Places
# context and decides; the regex would have committed to african_west on
# substring "JOLLOF" alone with no nuance. Let Haiku do the work.)

# Canonical cuisine taxonomy — defined in tools/cuisines.py so recovery scripts
# share the same set. Adding a bucket there is enough; do NOT re-declare here.
from cuisines import CUISINE_LABEL, normalize_cuisines
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
def is_denylist_chain(name_upper):
    """Match chain names ONLY at the start of the operating name. Chains typically
    appear as 'CHAIN' or 'CHAIN LOCATION' or 'CHAIN #123'. This avoids false positives
    like 'OM MA JOHN'S PIZZA & THAI EXPRESS' being matched as the chain 'THAI EXPRESS'."""
    n = (name_upper or '').strip()
    for c in CHAIN_DENYLIST:
        # Match at start, followed by word boundary, end-of-string, or common location separators
        if _re.match(r'^' + _re.escape(c) + r'(\b|$|[/#@,])', n):
            return True
    return False

# OSM-derived chain detector. OpenStreetMap mappers tag known chains with
# `brand=<Name>` (and often `brand:wikidata=<Qxxx>`) — an authoritative,
# human-curated source. We query Overpass for every branded amenity in the
# Toronto bbox via tools/build_osm_chain_set.py (refresh weekly), cache the
# result, and match operating names against it here.
#
# Why not the earlier count heuristic? Count-based catches campus food service
# (TMU/UofT), hospitality conglomerates (Aramark, Compass Group), AND legit
# local indies with 5+ locations. OSM separates "is a chain" from "has many
# locations" — only chains carry `brand=` tags. (User design note, 2026-05-14.)
_OSM_CHAIN_PATH = Path(__file__).resolve().parent / 'cache' / 'osm_chain_set.json'
_OSM_CHAIN_PATTERN = None

def _normalize_for_chain_match(s):
    """Strip apostrophes / periods / hyphens before matching. OSM and Toronto's
    licence registry disagree on these all the time: 'OSMOW'S' vs 'OSMOWS',
    'MR. SUB' vs 'MR SUB', 'A&W' vs 'A & W'. After this normalization, the
    boundary regex can be a single fast match."""
    return _re.sub(r"[\'\.\-]", '', (s or '').upper())

def _ensure_osm_chain_pattern():
    global _OSM_CHAIN_PATTERN
    if _OSM_CHAIN_PATTERN is not None: return _OSM_CHAIN_PATTERN
    if not _OSM_CHAIN_PATH.exists():
        _OSM_CHAIN_PATTERN = _re.compile(r'(?!)')  # never-matches sentinel
        return _OSM_CHAIN_PATTERN
    try:
        data = json.loads(_OSM_CHAIN_PATH.read_text())
    except Exception:
        _OSM_CHAIN_PATTERN = _re.compile(r'(?!)')
        return _OSM_CHAIN_PATTERN
    brands_upper = {k for k in (data.get('brands') or {}).keys()}
    cleaned = sorted({_normalize_for_chain_match(b) for b in brands_upper if b},
                     key=lambda x: -len(x))  # longest-first so "MCDONALDS" beats "MCDONALD"
    if not cleaned:
        _OSM_CHAIN_PATTERN = _re.compile(r'(?!)')
    else:
        # Match at start with word/end/separator boundary — same shape as is_denylist_chain
        _OSM_CHAIN_PATTERN = _re.compile(
            r'^(?:' + '|'.join(_re.escape(b) for b in cleaned) + r')(?:\b|$|[/#@,])'
        )
    return _OSM_CHAIN_PATTERN

def is_osm_chain(name_upper):
    """True iff operating name matches an OSM-tagged chain brand at a word
    boundary. Authoritative — only restaurants tagged with `brand=` in OSM
    get hits, so legit indies (Pai Northern Thai etc.) pass through cleanly."""
    p = _ensure_osm_chain_pattern()
    return bool(p.match(_normalize_for_chain_match(name_upper)))

def is_chain(name_upper):
    """Combined chain check: manual denylist OR OSM-derived authoritative list."""
    return is_denylist_chain(name_upper) or is_osm_chain(name_upper)

# Institutional / chain-parent detection by Client Name licence count.
# A Client Name holding ≥10 distinct food licence addresses across the City of
# Toronto is, in practice, always one of: (a) a national/regional chain parent
# corporation, (b) a multi-location franchisee LLC, (c) an institutional food
# service contractor (Aramark, Compass Group, Sodexo, TMU, hospital systems),
# or (d) a grocery/retail-food chain (Loblaws, Metro, Bulk Barn, Walmart).
# None of these belong in a "newest INDEPENDENT cultural-cuisine restaurants"
# directory. This complements is_chain() (which keys off the consumer brand
# name) by catching B2B operators whose Operating Name isn't a known consumer
# brand at all — Aramark cafeterias inside hospitals, TMU food courts, etc.
_INSTITUTIONAL_CLIENT_THRESHOLD = 10
_INSTITUTIONAL_CLIENTS = None

def _build_institutional_clients(csv_path):
    from collections import defaultdict
    client_addrs = defaultdict(set)
    try:
        with open(csv_path, encoding='utf-8', errors='replace') as f:
            for row in csv.DictReader(f):
                cat = (row.get('Category') or '').strip()
                if cat not in FOOD_CATS: continue
                if (row.get('Cancel Date') or '').strip(): continue
                cn = (row.get('Client Name') or '').strip().upper()
                addr1 = (row.get('Licence Address Line 1') or '').strip().upper()
                if cn and addr1:
                    client_addrs[cn].add(addr1)
    except (FileNotFoundError, OSError):
        return set()
    return {cn for cn, addrs in client_addrs.items()
            if len(addrs) >= _INSTITUTIONAL_CLIENT_THRESHOLD}

def _ensure_institutional_clients():
    global _INSTITUTIONAL_CLIENTS
    if _INSTITUTIONAL_CLIENTS is None:
        _INSTITUTIONAL_CLIENTS = _build_institutional_clients(CSV_PATH)
    return _INSTITUTIONAL_CLIENTS

def is_institutional_client(client_name_upper):
    """True iff the Client Name corporation holds ≥10 food licences across
    Toronto. Catches Aramark, Compass Group, TMU, hospital food contractors,
    plus chain-parent franchisee LLCs that don't surface in consumer-brand
    lists (OSM, our denylist)."""
    return (client_name_upper or '').strip() in _ensure_institutional_clients()

def keyword_classify(op_upper):
    for cuisine, keys in CUISINE_PATTERNS.items():
        for k in keys:
            if k in op_upper: return cuisine
    return None

VALID_LLM_KEYS = set(CUISINE_LABEL.keys())  # every key with a display label is valid
# Collects cache cuisines that have no display label, surfaced at end of run as
# a loud warning. Empty in steady state; growth means cuisines.py needs an entry.
_CUISINE_LABEL_GAP = set()

# Brand-level website inheritance: when a multi-location operator (LENA'S ROTI,
# OSMOW'S, etc.) opens a NEW location, the brand-new licence has no Places match
# yet and no own-website verification — but an EARLIER licence at a different
# address may have the brand site cached. Walk web_verify_cache once at startup;
# for each operating name, find the most-common verified non-aggregator website
# across all its rows. Inject can then inherit that website onto rows that
# otherwise have nothing. Stops "links to nowhere on Maps" for brand-new
# locations of established small-chain indies.
_BRAND_WEBSITE_INDEX = None
SOCIAL_DOMAINS_LOCAL = ('instagram.com', 'facebook.com', 'tiktok.com')

def _is_aggregator_or_social(url):
    if not url: return True
    u = url.lower()
    if any(d in u for d in SOCIAL_DOMAINS_LOCAL): return True
    if any(d in u for d in ('skipthedishes.', 'doordash.', 'ubereats.',
                            'grubhub.', 'foodora.', 'menulog.', 'seamless.',
                            'tripadvisor.', 'yelp.com')): return True
    if 'maps.google.' in u or 'goo.gl/maps' in u: return True
    return False

def _build_brand_website_index():
    """For each operating name (uppercased), collect the set of distinct non-
    aggregator websites observed across all its web_verify entries. When a
    given operating name has exactly ONE such website, that's the brand site
    and we can inherit it onto sibling licences. (Single-location indies that
    only appear once in the data also pass this rule — their one website is
    used directly, no inheritance needed since they already have it.)"""
    from collections import defaultdict
    name_to_sites = defaultdict(set)
    for k, e in WEB_VERIFY_CACHE.items():
        if e.get('status') != 'ok': continue
        ws = e.get('website')
        if ws and not _is_aggregator_or_social(ws):
            name = k.split('||')[0].strip().upper()
            name_to_sites[name].add(ws)
    return {n: next(iter(sites)) for n, sites in name_to_sites.items() if len(sites) == 1}

def _ensure_brand_website_index():
    global _BRAND_WEBSITE_INDEX
    if _BRAND_WEBSITE_INDEX is None:
        _BRAND_WEBSITE_INDEX = _build_brand_website_index()
    return _BRAND_WEBSITE_INDEX
CUISINE_LABEL.setdefault('thai', 'Thai')

def get_cuisine(name, address):
    """Returns (cuisines_list, source). cuisines_list is a list of valid cuisine
    keys (1-3 entries for multi-cuisine restaurants); empty list means drop.

    Priority order:
      1. web_verify cache (search-informed) — uses `cuisines` list if present, else
         promotes single `cuisine` for backwards compat
      2. name-only LLM cache (same shape)
      3. keyword classifier (single bucket from operating name)
    Chain denylist short-circuits everything.
    """
    name_upper = (name or '').strip().upper()
    if is_chain(name_upper):
        return [], None
    key = f"{name_upper}||{(address or '').strip().upper()}"

    # 1. Web-verified cuisines — richest signal (web search + page content + Places extras).
    w = WEB_VERIFY_CACHE.get(key)
    if w and w.get('status') == 'ok' and w.get('operating') == 'yes':
        cs = normalize_cuisines(w)
        valid = [c for c in cs if c in VALID_LLM_KEYS]
        for c in cs:
            if c not in VALID_LLM_KEYS: _CUISINE_LABEL_GAP.add(c)
        if valid:
            return valid, 'web_search'
        # Verifier returned unknown OR null cuisine — fall through to name-only.

    # 2. Name-only LLM cache — fallback when web_verify is null/unknown.
    # This IS Haiku (just operating on name alone). The unified validator gives
    # Haiku the full Places + verify context when it runs, so high-quality entries
    # should rarely fall through to this layer alone — but the layer remains for
    # entries Places couldn't match and web_verify never visited.
    llm = LLM_CACHE.get(key)
    if llm and llm.get('status') == 'ok':
        # Explicit "unknown" verdict from name-only stays a drop (we have ZERO signal)
        if llm.get('cuisine') == 'unknown' and not llm.get('cuisines'): return [], None
        cs = normalize_cuisines(llm)
        valid = [c for c in cs if c in VALID_LLM_KEYS]
        for c in cs:
            if c not in VALID_LLM_KEYS: _CUISINE_LABEL_GAP.add(c)
        if valid:
            return valid, 'llm'

    # NOTE: removed the regex keyword_classify fallback (it pattern-matched
    # operating names against CUISINE_PATTERNS — duplicative of what the
    # name-only LLM already sees, and a "dumb" signal user explicitly asked to
    # drop on 2026-05-14). Without web_verify or llm cache classification, the
    # entry has no Haiku-evaluated cuisine → drop.
    return [], None

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
n_dropped_unverified = 0; n_dropped_closed = 0; n_deduped = 0; n_dropped_instore = 0; n_dropped_institutional = 0; n_dropped_weak_match = 0

# Grocery/retail chains whose in-store sushi/sandwich counters are NOT consumer-
# destination restaurants. Three orthogonal signals catch them:
#   1. City's "Free Form Conditions" — "LOCATED INSIDE FORTINO'S", "WITHIN SOBEYS"
#   2. Operating name = grocery-counter franchise brand (AFC, Zenshi, Bento Nouveau)
#   3. Client Name = franchisor corp (Advanced Fresh Concepts)
INSTORE_CHAINS = (
    'SOBEYS', 'LOBLAWS', 'FORTINO', 'METRO', 'FRESHCO', 'WHOLE FOODS',
    'WALMART', 'COSTCO', 'SHOPPERS DRUG MART', 'NO FRILLS', 'FOOD BASICS',
    'LONGO', 'FARM BOY', 'T&T', 'GALLERIA', 'PUSATERI',
)
KIOSK_BRAND_PATTERNS = (
    'AFC SUSHI', 'AFC/', 'ZENSHI', 'BENTO NOUVEAU', 'BENTO SUSHI', 'GENJI',
)
KIOSK_CLIENTS = ('ADVANCED FRESH CONCEPTS',)

def _is_instore_kiosk(row):
    op = (row.get('Operating Name') or '').upper()
    client = (row.get('Client Name') or '').upper()
    if any(b in op for b in KIOSK_BRAND_PATTERNS): return True
    if any(c in client for c in KIOSK_CLIENTS): return True
    cond = ' '.join([
        (row.get('Conditions') or ''),
        (row.get('Free Form Conditions Line 1') or ''),
        (row.get('Free Form Conditions Line 2') or ''),
    ]).upper()
    if 'LOCATED' in cond and any(c in cond for c in INSTORE_CHAINS): return True
    return False

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
        # Drop in-grocery-store kiosks: AFC sushi counters inside Sobeys/Loblaws/etc.
        # are not consumer-destination restaurants even though they hold a take-out
        # licence. Caught by the City's own "Located inside X" conditions field.
        if _is_instore_kiosk(row):
            n_dropped_instore += 1
            continue
        # Institutional / chain-parent operator? Drop. Catches Aramark cafeterias,
        # Compass Group contract kitchens, TMU food courts, etc. — and chain
        # franchisee LLCs (10+ Tim Hortons/McDonald's locations under one corp).
        client_name = (row.get('Client Name') or '').strip().upper()
        if is_institutional_client(client_name):
            n_dropped_institutional += 1
            continue
        op_raw = (row.get('Operating Name') or '').strip()
        if not op_raw: continue
        addr1 = (row.get('Licence Address Line 1') or '').strip()
        addr3 = (row.get('Licence Address Line 3') or '').strip()
        address_full = (addr1 + ' ' + addr3).strip()
        cuisines, source = get_cuisine(op_raw, address_full)
        if not cuisines: continue

        # Unified-validator drop: Haiku looked at name + Places match + types +
        # editorial + reviews and concluded this is not a consumer restaurant
        # (institutional caterer, packaged-food brand, grocery counter, etc.).
        # Authoritative — trumps the cuisine signal.
        wv_e = WEB_VERIFY_CACHE.get(f"{op_raw.strip().upper()}||{address_full.strip().upper()}")
        if wv_e and wv_e.get('validator_drop'):
            n_dropped_unverified += 1   # bucket with other drops; could split out later
            continue

        # Verification gate: Places=OPERATIONAL OR web_search verified-yes.
        verification = verification_for(op_raw, address_full)
        if verification is None:
            n_dropped_unverified += 1
            continue

        # Build candidate entry
        days_open = max(0, (REFERENCE_DATE - iss).days)
        # Stable, URL-safe slug — kebab-case the name + leading address number for
        # disambiguation across multi-location chains/branches.
        name_part = _re.sub(r'[^\w\s-]', '', op_raw or '').strip().lower()
        name_part = _re.sub(r'[\s_]+', '-', name_part).strip('-')
        addr_num_m = _re.match(r'^(\d+)', (addr1 or '').strip())
        addr_num = addr_num_m.group(1) if addr_num_m else ''
        slug = (name_part + (f'-{addr_num}' if addr_num else ''))[:80]
        entry = {
            'operatingName': op_raw,
            'cuisine': cuisines[0],          # primary — backwards-compat for any consumer that reads `cuisine`
            'cuisines': cuisines,             # full multi-cuisine list — what the front-end filters on
            'cuisineSource': source,
            'issuedDate': iss.isoformat(),
            'daysOpen': days_open,
            'address': addr1,
            'slug': slug,
        }
        district = district_from_postal(address_full)
        if district: entry['district'] = district
        entry.update({k: v for k, v in verification.items() if v is not None})

        # fallbackMapsUrl: use the permit's authoritative NAME + ADDRESS as the
        # Google Maps query. Both fields come from the City of Toronto licence
        # data, which is the source of truth for what business should be at
        # what address. Google's geocoder + business-search handles the rest —
        # if the business is indexed, Maps shows the profile; if not, falls
        # back to the address.
        #
        # The earlier worry about this format was EASTERN 828 CAFE & GRILL
        # searching "EASTERN 828 CAFE & GRILL 828 EASTERN AVE" and getting
        # the established car wash at the same address. That case is now
        # caught upstream by the weak_match drop (no Places, no website,
        # name-only cuisine) so it never reaches URL construction. Entries
        # that DO reach here have at least one of: Places match, real website,
        # or brand-inherited website — i.e., something attesting to the
        # business's existence beyond the licence row.
        if op_raw and addr1:
            entry['fallbackMapsUrl'] = (
                f"https://www.google.com/maps/search/?api=1"
                f"&query={quote_plus(op_raw + ' ' + addr1 + ' Toronto')}"
            )
        elif addr1:
            entry['fallbackMapsUrl'] = (
                f"https://www.google.com/maps/?q={quote_plus(addr1 + ' Toronto, ON')}"
            )
        else:
            entry['fallbackMapsUrl'] = ''

        # Brand-website inheritance: if this entry has no website but the same
        # operating name has exactly one known brand website cached across other
        # licences, use it. Catches the brand-new-location case (LENA'S ROTI's
        # 3999 Keele licence has no Places yet, but the brand's lenasroti.ca is
        # cached on its other Toronto licences). Site is non-aggregator, so safe.
        if not entry.get('website'):
            brand_site = _ensure_brand_website_index().get(op_raw.strip().upper())
            if brand_site:
                entry['website'] = brand_site
                entry['websiteSource'] = 'brand_inherited'

        # Weak-match drop: if NONE of Places / a working website / a real cuisine
        # signal exist, we're sending the user to a location we can't verify is
        # the right place. Better to hide the entry than risk landing them at a
        # neighbouring business (e.g., EASTERN 828 CAFE → adjacent car wash).
        # Conditions for "weak":
        #   - no Places match (no matchedName / no mapsUrl), AND
        #   - no working website (already dropped by url_is_alive if broken), AND
        #   - cuisine came from name-only LLM or keyword guess (no real evidence).
        # These entries will be re-queued for the next cron — their caches'
        # recovered_at timestamps gate the 30-day re-attempt window, by which
        # point Google/Yelp/blogs may have indexed the place and a stronger
        # signal will arrive.
        # Re-evaluate weak_match AFTER brand-website inheritance — a brand site
        # is a real positive signal even if the verifier didn't catch it for
        # this specific location.
        weak_match = (
            not entry.get('matchedName')
            and not entry.get('mapsUrl')
            and not entry.get('website')
            and source in ('llm', 'keyword')
        )
        if weak_match:
            n_dropped_weak_match += 1
            continue

        # Dedupe by (name_upper, addr_upper). Keep EARLIEST issuedDate.
        dedup_key = (op_raw.upper(), addr1.upper())
        existing = seen_entries.get(dedup_key)
        if existing is None:
            seen_entries[dedup_key] = entry
        else:
            n_deduped += 1
            if iss.isoformat() < existing['issuedDate']:
                seen_entries[dedup_key] = entry  # this row is earlier — keep it

# Now bucket the deduped entries by cuisine and compute counts.
# Multi-cuisine entries (e.g., "Afghan + Pakistani + Indian") appear in EACH
# of their cuisine buckets — totalTagged365d counts entries (not bucket-rows),
# so a 3-cuisine place still counts as 1 toward the total.
opens_365_by_cuisine = defaultdict(list)
for entry in seen_entries.values():
    n_tagged_365 += 1
    if entry['daysOpen'] <= 30: n_tagged_30 += 1
    for c in entry.get('cuisines') or [entry['cuisine']]:
        opens_365_by_cuisine[c].append(entry)

print(f"  verification gate: kept {n_tagged_365}, dropped {n_dropped_unverified} unverified + {n_dropped_closed} closed/temp + {n_dropped_instore} in-store kiosks + {n_dropped_institutional} institutional-operator rows + {n_dropped_weak_match} weak-match (no Places / no site / name-guess only) + {n_deduped} duplicate rows collapsed")

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

# Flat feed: all openings, newest first. Iterate seen_entries directly (NOT the
# per-cuisine buckets) so multi-cuisine entries — which appear in multiple cuisine
# buckets by design — are NOT duplicated in the flat feed.
all_recent = sorted(seen_entries.values(),
                    key=lambda r: r['issuedDate'], reverse=True)[:1500]

# Inject
from datetime import timezone
data = json.load(open(DATA_PATH))
# Stamp top-level generatedAt too — daily inject regenerates the dataset, so the
# subtitle "updated <date>" should reflect today, not the last build_corridors run.
data['generatedAt'] = datetime.now(timezone.utc).isoformat()
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
    # Multi-cuisine row: emit one colored pill per declared cuisine, falling
    # back to the single `cuisine` field for legacy entries.
    cuisine_keys = r.get('cuisines') or ([r['cuisine']] if r.get('cuisine') else [])
    pills_html = ''.join(
        f'<span class="pill" style="background:{PALETTE_HEX.get(k, "#777")}">{_esc(CUISINE_LABEL.get(k, k))}</span>'
        for k in cuisine_keys
    )
    name = _esc(r['operatingName'])
    addr = _esc(r.get('address') or '')
    district = _esc(r.get('district') or '')
    addr_url = r.get('mapsUrl') or r.get('fallbackMapsUrl') or ''
    addr_inner = f'<a href="{_esc(addr_url)}" rel="noopener">{addr}</a>' if addr_url and addr else addr
    addr_html = f'{addr_inner}<span class="oad-d"> · {district}</span>' if district else addr_inner
    ago = _esc(_ago(r['daysOpen']))
    link = r.get('website') or r.get('mapsUrl') or r.get('fallbackMapsUrl') or ''
    name_html = f'<a href="{_esc(link)}" rel="noopener">{name}</a>' if link else name
    multi_attr = ' data-multi' if len(cuisine_keys) > 1 else ''
    static_rows_html.append(
        f'<div class="open-row"{multi_attr}>'
        f'<div class="od"><span class="ago">{ago}</span></div>'
        f'<div class="on">{name_html}<span class="oad">{addr_html}</span></div>'
        f'<div class="oc">{pills_html}</div>'
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
        # schema.org/Restaurant.servesCuisine accepts an array of strings, so we
        # emit the full multi-cuisine list when available (better SEO signal).
        'servesCuisine': [CUISINE_LABEL.get(k, k) for k in (r.get('cuisines') or [r.get('cuisine')]) if k],
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

# Loud sanity check: if any recovery script tagged a cuisine that cuisines.py
# doesn't have a display label for, those entries were silently dropped above.
# Surface it so the fix (add the key to cuisines.py CUISINE_LABEL) is obvious.
if _CUISINE_LABEL_GAP:
    print()
    print(f"!!!!!!  WARNING: {len(_CUISINE_LABEL_GAP)} cuisine key(s) in cache but missing from cuisines.py CUISINE_LABEL:")
    for c in sorted(_CUISINE_LABEL_GAP):
        print(f"          {c!r}")
    print("        These entries were SILENTLY DROPPED from the feed. Add them to tools/cuisines.py.")
