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
from cuisines import CUISINE_LABEL, normalize_cuisines, cuisine_color
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
# REMOVED 2026-05-14: hardcoded chain rules. The unified validator (Haiku
# with full City row + Places + reviews + editorial) decides is_restaurant=no
# for chain franchisees by reading Client Name, Operating Name, Places types,
# and reviews together. No regex denylist. No OSM brand cross-reference. See
# tools/validate_entries_batch.py SYSTEM_PROMPT.


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
# REMOVED 2026-05-14: hardcoded ≥10-licence Client Name threshold. The
# validator now sees Client Name directly and judges institutional/chain
# from corporation identity + Places types + reviews. No magic threshold.

import re as _re
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
# REMOVED 2026-05-14: brand-website inheritance dict. The validator
# returns best_website per entry from full evidence; we honor that directly.

CUISINE_LABEL.setdefault('thai', 'Thai')

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

def get_cuisine(name, address):
    """Returns (cuisines_list, source). cuisines_list is a list of valid cuisine
    keys (1-3 entries for multi-cuisine restaurants); empty list means drop.

    Priority order:
      1. web_verify cache (search-informed by Haiku + web_search, then refined
         by the unified validator that sees the full City row + Places data)
      2. name-only LLM cache (Haiku on operating name alone)
    No chain-denylist short-circuit here — the validator marks chains and
    institutional operators with `validator_drop` directly; inject just honors
    that flag in the main loop.
    """
    name_upper = (name or '').strip().upper()
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
    # Source 2: Web-search verification (no Places match for this address).
    # Per user directive 2026-05-15: when Places has no website, fall back to
    # the WV-surfaced URL (top Google-search match from earlier Haiku
    # web_search) IF the validator approved it. url_is_alive returns False
    # when the validator marked the URL broken in url_health_cache, so a
    # WV-URL that survives that gate has passed Haiku's content review.
    w = WEB_VERIFY_CACHE.get(key)
    if w and w.get('status') == 'ok' and w.get('operating') == 'yes':
        out = {'businessStatus': 'OPERATIONAL', 'verifiedBy': 'web_search'}
        wv_site = w.get('website')
        if wv_site and url_is_alive(wv_site):
            out['website'] = wv_site
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
n_dropped_unverified = 0; n_dropped_closed = 0; n_deduped = 0; n_dropped_instore = 0; n_dropped_institutional = 0; n_dropped_weak_match = 0; n_dropped_brand_new_unverified = 0; n_dropped_validator = 0

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

with open(CSV_PATH, encoding='utf-8', errors='replace') as f:
    rdr = csv.DictReader(f)
    for row in rdr:
        cat = (row.get('Category') or '').strip()
        if cat not in FOOD_CATS: continue
        # Cancel Date rule: drop only if cancelled more than 10 days ago.
        # Future-dated cancellations (place still operating, scheduled closure)
        # AND recent cancellations within 10 days (transition / wind-down period)
        # are KEPT. Per user directive 2026-05-14.
        cancel_raw = (row.get('Cancel Date') or '').strip()
        if cancel_raw:
            cancel_d = parse_d(cancel_raw)
            if cancel_d and (REFERENCE_DATE - cancel_d).days > 10:
                continue
        n_food_active += 1
        iss = parse_d(row.get('Issued'))
        if not iss or iss < WINDOW_365: continue
        n_food_active_365 += 1
        # (REMOVED 2026-05-14: hardcoded in-store-kiosk filter via Conditions regex
        # and Client Name licence-count threshold. The validator sees Client Name +
        # Conditions directly and flags `validator_drop: not-restaurant` for
        # institutional caterers, in-grocery kiosks, packaged-food brands, etc.
        # The validator_drop honoring below handles all of these uniformly.)
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
            n_dropped_validator += 1   # Haiku-judged: chain, institutional, ghost, etc.
            continue

        # Verification gate: Places=OPERATIONAL OR web_search verified-yes.
        verification = verification_for(op_raw, address_full)
        if verification is None:
            n_dropped_unverified += 1   # no Places + no web_verify yet — pending pipeline data
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
            'address': addr1,                 # default: permit address. Overridden by Places.matchedAddress below when available.
            'slug': slug,
        }
        district = district_from_postal(address_full)
        if district: entry['district'] = district
        entry.update({k: v for k, v in verification.items() if v is not None})

        # Social handles extracted by the validator from the fetched website
        # (Instagram / X / Facebook). Used by the X posting bot to @-mention
        # the restaurant when announcing it; absent when the validator
        # couldn't find any social links in the page content.
        if wv_e and wv_e.get('socials'):
            entry['socials'] = wv_e['socials']

        # Per user directive 2026-05-14: Google Places data overrides permit
        # data where Places has authoritative info. Use the matchedAddress as
        # the displayed address (cleaner formatting, validated location).
        # Fallback: permit address (when no Places match).
        places_match = PLACES_CACHE.get(f"{op_raw.strip().upper()}||{address_full.strip().upper()}")
        if places_match and places_match.get('status') == 'ok' and places_match.get('matchedAddress'):
            # Strip the ", Canada" suffix; keep "123 Main St, Toronto, ON M5V 1A1"
            ma = places_match['matchedAddress']
            ma = _re.sub(r',\s*Canada\s*$', '', ma)
            entry['address'] = ma

        # fallbackMapsUrl — Google Maps search using NAME + ADDRESS being
        # DISPLAYED to the user. One consistent format across every listing
        # per user directive 2026-05-15.
        #
        # Why name+address (not address-only): Google's Maps geocoder lands
        # bare addresses in multi-unit buildings on the building centroid
        # (e.g. "364 Huron St C104" → generic spot in a hospital block, not
        # the actual gimbap shop). The Place card is reachable only via
        # name-in-query search. The earlier "no name" rule existed to dodge
        # established-same-name-brand routing (the Mapo Korean BBQ case),
        # but the brand-new-unverified gate above now drops every entry
        # without Places match AND without a validator-approved website,
        # so every entry that reaches this code path has been independently
        # verified to exist at this address — name-in-search routes
        # correctly to the actual Place card.
        disp_addr = (entry.get('address') or addr1 or '').strip()
        if disp_addr:
            parts = [op_raw, disp_addr] if op_raw else [disp_addr]
            q = ' '.join(p for p in parts if p)
            if 'toronto' not in q.lower():
                q = f"{q} Toronto, ON"
            entry['fallbackMapsUrl'] = (
                f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"
            )
        else:
            entry['fallbackMapsUrl'] = ''

        # (REMOVED 2026-05-14: brand-website inheritance dict — the validator
        # returns best_website per entry directly, computed from full evidence.)

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
        # Drop only when there's literally no evidence the place exists —
        # no Places match AND no website AND cuisine came from name-only
        # (no web_search evidence the operator is real). For multi-location
        # indies where validator cleared an address-mismatched Places match
        # but web_verify found the brand online (cuisine source = web_search),
        # KEEP the entry — they may show in a brand-search even if Google
        # hasn't indexed this specific location yet.
        if (not entry.get('matchedName')
            and not entry.get('mapsUrl')
            and not entry.get('website')
            and source in ('llm', None)):
            n_dropped_weak_match += 1
            continue

        # Brand-new + Places-unverified gate (added 2026-05-15 per user
        # directive). Drop entries with NO Places match AND NO validator-
        # approved website AND licence < 30 days old. Rationale: a 6-day-
        # old licence at an address Google doesn't know about is almost
        # always pre-opening build-out (operator gets the licence weeks
        # before doors open) or a permit that never materialized. Sending
        # a user to a plywood storefront is worse UX than not surfacing
        # it. The 30-day cutoff matches the validator's aged-out rule:
        # past 30 days, the validator marks it is_restaurant=no anyway.
        # Re-queues automatically — next cron picks the entry up once
        # Places or a website appears.
        if (not entry.get('matchedName')
            and not entry.get('website')
            and days_open < 30):
            n_dropped_brand_new_unverified += 1
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

print(f"  verification gate: kept {n_tagged_365}, dropped {n_dropped_validator} validator (Haiku: chain/institutional/ghost) + {n_dropped_unverified} unverified (no Places, no web_verify yet) + {n_dropped_closed} closed/temp + {n_dropped_instore} in-store kiosks + {n_dropped_institutional} institutional-operator rows + {n_dropped_weak_match} weak-match (no Places / no site / name-guess only) + {n_dropped_brand_new_unverified} brand-new-unverified (<30d, no Places/website) + {n_deduped} duplicate rows collapsed")

# Sort each cuisine's list by issued date desc (newest first)
for c in opens_365_by_cuisine:
    opens_365_by_cuisine[c].sort(key=lambda r: r['issuedDate'], reverse=True)

# Summary per cuisine
cuisines_out = []
for c, entries in opens_365_by_cuisine.items():
    # Color: prefer the curated palette below; fall back to a deterministic
    # hash-derived color for novel/dynamic cuisines (Hakka, Uyghur, Cape
    # Verdean, etc. — anything Haiku surfaced that wasn't in the seed list).
    color = PALETTE_HEX.get(c) or cuisine_color(c)
    cuisines_out.append({
        'key': c,
        'label': CUISINE_LABEL.get(c, c),
        'color': color,
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
# (PALETTE_HEX moved up; see definition near the top of this file.)

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

# ---------------------------------------------------------------------------
# Static-feed + JSON-LD builders (shared between homepage and per-cuisine pages).
# ---------------------------------------------------------------------------
def build_static_rows(entries):
    """Pre-rendered HTML rows for the top-N feed. Same markup the JS renderer
    produces so visitors / crawlers see real content before JS hydrates."""
    out = []
    for r in entries:
        cuisine_keys = r.get('cuisines') or ([r['cuisine']] if r.get('cuisine') else [])
        pills = ''.join(
            f'<span class="pill" style="background:{PALETTE_HEX.get(k) or cuisine_color(k)}">{_esc(CUISINE_LABEL.get(k, k))}</span>'
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
        out.append(
            f'<div class="open-row"{multi_attr}>'
            f'<div class="od"><span class="ago">{ago}</span></div>'
            f'<div class="on">{name_html}<span class="oad">{addr_html}</span></div>'
            f'<div class="oc">{pills}</div>'
            f'</div>'
        )
    return '\n    '.join(out)


def build_ld_itemlist(entries, name, description):
    items = []
    for i, r in enumerate(entries, 1):
        rest = {
            '@type': 'Restaurant',
            'name': r['operatingName'],
            'address': {'@type': 'PostalAddress', 'streetAddress': r.get('address') or '', 'addressLocality': 'Toronto', 'addressRegion': 'ON', 'addressCountry': 'CA'},
            'servesCuisine': [CUISINE_LABEL.get(k, k) for k in (r.get('cuisines') or [r.get('cuisine')]) if k],
            'dateOpened': r.get('issuedDate'),
        }
        if r.get('website'): rest['url'] = r['website']
        if r.get('rating'): rest['aggregateRating'] = {'@type': 'AggregateRating', 'ratingValue': r['rating'], 'reviewCount': r.get('reviewCount') or 1}
        items.append({'@type': 'ListItem', 'position': i, 'item': rest})
    return {
        '@context': 'https://schema.org',
        '@type': 'ItemList',
        'name': name,
        'description': description,
        'itemListElement': items,
    }


import re

def inject_into_html(html, *, static_block, ld_payload):
    """Replace STATIC-FEED and LD-ITEMLIST marker blocks. Uses lambda
    replacements so backslash sequences in the replacement (e.g. \\uXXXX
    in JSON-LD) aren't interpreted as regex backreferences."""
    html = re.sub(
        r'(<!-- STATIC-FEED-START[^>]*-->).*?(<!-- STATIC-FEED-END -->)',
        lambda m: m.group(1) + '\n    ' + static_block + '\n    ' + m.group(2),
        html, count=1, flags=re.DOTALL,
    )
    ld_json_str = json.dumps(ld_payload, separators=(',', ':'))
    ld_script = f'<script type="application/ld+json" id="ld-itemlist">{ld_json_str}</script>'
    html = re.sub(
        r'(<!-- LD-ITEMLIST-START -->).*?(<!-- LD-ITEMLIST-END -->)',
        lambda m: m.group(1) + '\n' + ld_script + '\n' + m.group(2),
        html, count=1, flags=re.DOTALL,
    )
    return html


# ---------------------------------------------------------------------------
# Inject into the HOMEPAGE (index.html).
# ---------------------------------------------------------------------------
top_for_static = all_recent[:30]
static_block = build_static_rows(top_for_static)
ld_payload = build_ld_itemlist(
    top_for_static,
    name="Toronto's newest restaurants by cuisine",
    description='Restaurants newly licensed in Toronto in the past 365 days, classified by cuisine.',
)
try:
    home_html = open(INDEX_PATH).read()
    home_html = inject_into_html(home_html, static_block=static_block, ld_payload=ld_payload)
    open(INDEX_PATH, 'w').write(home_html)
    print(f"  pre-rendered {len(top_for_static)} static feed rows + JSON-LD ItemList into index.html")
except Exception as e:
    print(f"  WARN: index.html injection failed: {e}")


# ---------------------------------------------------------------------------
# Inject PER-CUISINE landing pages at cuisine/<key>.html.
# ---------------------------------------------------------------------------
# Each cuisine gets its own HTML file with:
#   - title / og:title / twitter:title baked in (server-rendered, so first-pass
#     crawls see cuisine-specific signal instead of the generic home title)
#   - meta description scoped to the cuisine + count
#   - canonical pointing at the /cuisine/<key> route
#   - <h1> inserted after the brand line with "New <Cuisine> restaurants in Toronto"
#   - STATIC-FEED block rendered from THIS cuisine's top-30 (not the mixed feed)
#   - JSON-LD ItemList scoped to this cuisine
# Apache .htaccess rewrites /cuisine/<key> → /cuisine/<key>.html when the file
# exists (added in this same commit).
CUISINE_DIR = Path(ROOT) / 'cuisine'
CUISINE_DIR.mkdir(exist_ok=True)
cuisine_pages_written = 0
template = open(INDEX_PATH).read()   # post-homepage-inject — has the fresh JS bundle
for c in cuisines_out:
    key = c['key']; label = c['label']; n365 = c['count365d']; n30 = c['count30d']
    entries = opens_365_by_cuisine.get(key, [])[:30]
    if not entries: continue

    title = f"New {label} restaurants in Toronto — NowServingTO"
    desc = (f"Every newly licensed {label} restaurant in Toronto over the past 365 "
            f"days, updated daily. {n365} entries tracked, {n30} from the last 30 days.")
    canonical = f"https://nowservingto.com/cuisine/{key}"

    page = template
    # Replace meta tags — first occurrence each.
    page = re.sub(r'<title>[^<]*</title>', f'<title>{_esc(title)}</title>', page, count=1)
    page = re.sub(r'(<meta name="description" content=")[^"]*(")',
                  lambda m: m.group(1) + _esc(desc) + m.group(2), page, count=1)
    page = re.sub(r'(<meta property="og:title" content=")[^"]*(")',
                  lambda m: m.group(1) + _esc(title) + m.group(2), page, count=1)
    page = re.sub(r'(<meta property="og:description" content=")[^"]*(")',
                  lambda m: m.group(1) + _esc(desc) + m.group(2), page, count=1)
    page = re.sub(r'(<meta name="twitter:title" content=")[^"]*(")',
                  lambda m: m.group(1) + _esc(title) + m.group(2), page, count=1)
    page = re.sub(r'(<meta name="twitter:description" content=")[^"]*(")',
                  lambda m: m.group(1) + _esc(desc) + m.group(2), page, count=1)
    page = re.sub(r'(<link rel="canonical" href=")[^"]*(")',
                  lambda m: m.group(1) + canonical + m.group(2), page, count=1)

    # Swap the homepage's generic <h1> for a cuisine-specific one. The
    # homepage h1 reads "New restaurants in Toronto, by cuisine"; we
    # replace it with "New <Cuisine> restaurants in Toronto" so crawlers
    # see a single, cuisine-defining heading on each landing page.
    h1 = (f'<h1 class="cuisine-h1">New <span class="hl">{_esc(label)}</span> '
          f'restaurants in Toronto</h1>')
    page = re.sub(
        r'<h1 class="cuisine-h1">[\s\S]*?</h1>',
        lambda m: h1,
        page, count=1,
    )

    # Also tailor the subtitle to this cuisine.
    page = re.sub(
        r'<div class="sub">[\s\S]*?</div>',
        lambda m: (f'<div class="sub">Every newly licensed {_esc(label)} '
                   f'restaurant in Toronto from the past 12 months, sourced '
                   f'from City of Toronto open data, updated daily — '
                   f'{n365} entries tracked.</div>'),
        page, count=1,
    )

    # Replace STATIC-FEED + LD-ITEMLIST with cuisine-scoped versions.
    cuisine_static = build_static_rows(entries)
    cuisine_ld = build_ld_itemlist(
        entries,
        name=f"Newest {label} restaurants in Toronto",
        description=desc,
    )
    page = inject_into_html(page, static_block=cuisine_static, ld_payload=cuisine_ld)

    (CUISINE_DIR / f'{key}.html').write_text(page)
    cuisine_pages_written += 1
print(f"  wrote {cuisine_pages_written} per-cuisine SEO landing pages → cuisine/<key>.html")


# ---------------------------------------------------------------------------
# Per-LISTING pages at r/<slug>.html  +  OG image cards at og/<slug>.png.
# ---------------------------------------------------------------------------
# Every kept entry gets:
#   r/<slug>.html  — own title/og:image/canonical/h1, single-row static feed,
#                    single-Restaurant JSON-LD. Apache .htaccess rewrites
#                    /r/<slug> → /r/<slug>.html.
#   og/<slug>.png  — 1200×675 branded card used as og:image so X/FB/iMessage
#                    show the personalized image when the URL is shared,
#                    with the IMAGE itself being a click-target to the page.
from og_card import render_card_png as _render_og_card
LISTING_DIR = Path(ROOT) / 'r'
OG_DIR      = Path(ROOT) / 'og'
LISTING_DIR.mkdir(exist_ok=True)
OG_DIR.mkdir(exist_ok=True)

listing_template = open(INDEX_PATH).read()
n_listing_html = 0
n_listing_png  = 0
for entry in seen_entries.values():
    slug = entry.get('slug')
    if not slug: continue

    # 1) PNG card → og/<slug>.png
    try:
        _render_og_card(entry, out_path=str(OG_DIR / f'{slug}.png'))
        n_listing_png += 1
    except Exception as ex:
        print(f"  WARN: og card failed for {slug}: {ex}")
        continue  # skip the HTML too — no point without the og:image

    # 2) HTML → r/<slug>.html
    name = entry.get('operatingName', '')
    keys = entry.get('cuisines') or ([entry['cuisine']] if entry.get('cuisine') else [])
    primary_key = keys[0] if keys else ''
    primary_lbl = CUISINE_LABEL.get(primary_key, primary_key.replace('_', ' ').title()) if primary_key else 'restaurant'
    addr     = entry.get('address') or ''
    district = entry.get('district') or ''

    title = f"{name} — {primary_lbl} restaurant in Toronto · NowServingTO"
    desc_addr = addr + (f', {district}' if district and district not in addr else '')
    desc  = (f"{name} — newly licensed {primary_lbl} restaurant at {desc_addr}. "
             f"Part of NowServingTO's daily-updated directory of Toronto's "
             f"newest restaurants, by cuisine.")
    canonical = f"https://nowservingto.com/r/{slug}"
    og_image  = f"https://nowservingto.com/og/{slug}.png"

    page = listing_template
    page = re.sub(r'<title>[^<]*</title>',
                  lambda m: f'<title>{_esc(title)}</title>', page, count=1)
    for sel, val in [
        (r'(<meta name="description" content=")[^"]*(")',     desc),
        (r'(<meta property="og:title" content=")[^"]*(")',    title),
        (r'(<meta property="og:description" content=")[^"]*(")', desc),
        (r'(<meta property="og:image" content=")[^"]*(")',    og_image),
        # Match the card's actual 1200×675 dimensions (the template defaults
        # to 1200×630 for the homepage og.svg, which is a different image).
        (r'(<meta property="og:image:width" content=")[^"]*(")',  '1200'),
        (r'(<meta property="og:image:height" content=")[^"]*(")', '675'),
        (r'(<meta name="twitter:title" content=")[^"]*(")',   title),
        (r'(<meta name="twitter:description" content=")[^"]*(")', desc),
        (r'(<meta name="twitter:image" content=")[^"]*(")',   og_image),
        (r'(<link rel="canonical" href=")[^"]*(")',           canonical),
    ]:
        page = re.sub(sel, lambda m, v=val: m.group(1) + _esc(v) + m.group(2),
                      page, count=1)

    # Swap the homepage's h1 for the listing's name.
    page = re.sub(r'<h1 class="cuisine-h1">[\s\S]*?</h1>',
                  lambda m: f'<h1 class="cuisine-h1">{_esc(name)}</h1>',
                  page, count=1)
    # Tailor the subtitle to this listing.
    page = re.sub(
        r'<div class="sub">[\s\S]*?</div>',
        lambda m: (f'<div class="sub">Newly licensed {_esc(primary_lbl)} '
                   f'restaurant — {_esc(desc_addr)}. From NowServingTO\'s '
                   f'daily directory of Toronto\'s newest restaurants.</div>'),
        page, count=1,
    )

    # Single-entry static feed + single-Restaurant JSON-LD.
    one_row = build_static_rows([entry])
    listing_ld = {
        '@context': 'https://schema.org',
        '@type': 'Restaurant',
        'name': name,
        'address': {
            '@type': 'PostalAddress', 'streetAddress': addr,
            'addressLocality': 'Toronto', 'addressRegion': 'ON', 'addressCountry': 'CA',
        },
        'servesCuisine': [CUISINE_LABEL.get(k, k) for k in keys if k],
        'url': entry.get('website') or canonical,
        'image': og_image,
        'dateOpened': entry.get('issuedDate'),
    }
    if entry.get('rating'):
        listing_ld['aggregateRating'] = {
            '@type': 'AggregateRating',
            'ratingValue': entry['rating'],
            'reviewCount': entry.get('reviewCount') or 1,
        }
    page = inject_into_html(page, static_block=one_row, ld_payload=listing_ld)

    (LISTING_DIR / f'{slug}.html').write_text(page)
    n_listing_html += 1

print(f"  wrote {n_listing_html} per-listing pages → r/<slug>.html")
print(f"  wrote {n_listing_png} per-listing OG cards → og/<slug>.png")

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
