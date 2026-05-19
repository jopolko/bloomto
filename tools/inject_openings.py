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

# OSM-derived chain brand set, built daily by tools/build_osm_chain_set.py
# from OpenStreetMap. Keys are UPPER-CASED brand names ("241 PIZZA", "A&W")
# with osmCount = how many OSM nodes carry that brand tag. Any Toronto
# licence whose operating name matches one of these names is by definition
# a multi-location chain and disqualified from the directory regardless of
# what the per-entry validator decides — the validator can miss chains
# when an entry's specific website/Places data is incomplete, but OSM
# crowd-tagging across many cities never has that gap. Free pre-filter
# that's also cheaper than a Haiku batch call (the dropped entries skip
# all downstream verify/validate work).
try:
    OSM_CHAIN_SET = json.load(open(f'{ROOT}/tools/cache/osm_chain_set.json')).get('brands', {})
except FileNotFoundError:
    OSM_CHAIN_SET = {}

def is_osm_chain(op_raw):
    """True if the operating name matches an OSM-tagged chain brand. Match
    is case-insensitive on the exact name OR a normalized variant (strip
    trailing 'INC'/'LTD' suffixes and trailing licence-numbering like '#3')."""
    if not op_raw or not OSM_CHAIN_SET: return False
    name = op_raw.strip().upper()
    if name in OSM_CHAIN_SET: return True
    import re as _re_chain
    cleaned = _re_chain.sub(r'\s+(INC|LTD|LLC|CORP|CO|LIMITED)\.?$', '', name).strip()
    cleaned = _re_chain.sub(r'\s*#\s*\d+$', '', cleaned).strip()
    return cleaned in OSM_CHAIN_SET

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
from places_key import cache_key
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
    key = cache_key(name, address)

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
    key = cache_key(name, address)
    # Geocode cache is keyed by street address only (no postal code). Try the
    # full key first, then a stripped-postal fallback to match older cache entries.
    addr_no_postal = _re.sub(r'\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d$', '', (address or '').strip().upper())
    geo = GEOCODE_CACHE.get(key) or GEOCODE_CACHE.get(cache_key(name, addr_no_postal))
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
n_dropped_unverified = 0; n_dropped_closed = 0; n_deduped = 0; n_dropped_instore = 0; n_dropped_institutional = 0; n_dropped_weak_match = 0; n_dropped_brand_new_unverified = 0; n_dropped_validator = 0; n_dropped_chain_osm = 0

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
        # Pre-filter: OSM-tagged chain brands. If OpenStreetMap has tagged
        # this exact operating name as a multi-location brand (e.g. 241
        # Pizza, A&W, Subway), it's a chain regardless of what the per-entry
        # validator decides. Saves the downstream verify/validate spend AND
        # catches chain locations where per-entry Haiku evidence is sparse
        # (e.g. UberEats URL for one location and 241pizza.com for others).
        if is_osm_chain(op_raw):
            n_dropped_chain_osm += 1
            continue
        addr1 = (row.get('Licence Address Line 1') or '').strip()
        addr3 = (row.get('Licence Address Line 3') or '').strip()
        address_full = (addr1 + ' ' + addr3).strip()
        cuisines, source = get_cuisine(op_raw, address_full)
        if not cuisines: continue

        # Unified-validator drop: Haiku looked at name + Places match + types +
        # editorial + reviews and concluded this is not a consumer restaurant
        # (institutional caterer, packaged-food brand, grocery counter, etc.).
        # Authoritative — trumps the cuisine signal.
        wv_e = WEB_VERIFY_CACHE.get(cache_key(op_raw, address_full))
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
            # Stash the cache-key built from PERMIT name+address so downstream
            # places_cache lookups work even after entry.address has been
            # overridden by Places' formatted matchedAddress.
            '_cacheKey': cache_key(op_raw, address_full),
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
        places_match = PLACES_CACHE.get(entry['_cacheKey'])
        if places_match and places_match.get('status') == 'ok' and places_match.get('matchedAddress'):
            # Strip the ", Canada" suffix; keep "123 Main St, Toronto, ON M5V 1A1"
            ma = places_match['matchedAddress']
            ma = _re.sub(r',\s*Canada\s*$', '', ma)
            entry['address'] = ma

        # fallbackMapsUrl removed 2026-05-19. The previous design assumed
        # every entry reaching this code path had been independently verified
        # to exist at this address (the brand-new-unverified gate enforced
        # Places match OR website). After 2026-05-19's gate refinement let
        # web-verify-only entries through (e.g. CAFEMIA: DineSafe + Yelp
        # confirmed but no Places profile yet), a name+address search no
        # longer reliably lands on the right business — Google Maps returns
        # OTHER businesses at nearby addresses (Messina/Amico/Tre Mari
        # bakeries instead of CAFEMIA), which is worse UX than no link at
        # all. The row renderer now shows the address as plain text when no
        # Places-backed mapsUrl exists.

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

        # No-destination gate (tightened 2026-05-19 per user directive).
        # Drop entries with NO Places match AND NO website at ANY age.
        # Earlier iterations allowed "web_verify says operating=yes" to
        # carry an entry through even without a destination URL — that
        # produced rows whose name + thumbnail had to fall back to our
        # own /r/<slug> internal page (no Maps, no website to send users
        # to). User feedback: "verification too thin, social only and not
        # even relevant social — I'll risk the clicks." Better to have
        # fewer high-quality listings than ones we can't link anywhere
        # useful. Re-verify will pick the entry up on a future cron once
        # Places indexes the business or a real website is found.
        if not entry.get('matchedName') and not entry.get('website'):
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
# Photo pre-pass — download Place/Street View photos NOW (before serializing
# corridors.json and rendering static feeds) so each entry can carry a
# `photo` field that the frontend renders as a row thumbnail. Same priority
# order as the og:image: cached Places photoRef → Place Details re-fetch
# (bot-eligible <=30d) → Street View → none.
from pathlib import Path as _Path
import subprocess as _sub
_PHOTO_DIR = _Path(ROOT) / 'og' / 'photo'
_THUMB_DIR = _Path(ROOT) / 'og' / 'thumb'
_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
_THUMB_DIR.mkdir(parents=True, exist_ok=True)
from enrich_places import (download_place_photo as _dl_photo,
                            streetview_metadata as _sv_meta,
                            streetview_image as _sv_img,
                            place_details as _pd)

def _make_thumb(src, dst, size=160):
    """Center-square-crop + resize to size×size, save as JPEG q=78. ~6KB per
    thumb. Tries ImageMagick `convert` first (VPS has it), falls back to PIL
    (local dev has it)."""
    try:
        _sub.run(
            ['convert', str(src), '-resize', f'{size}x{size}^',
             '-gravity', 'center', '-extent', f'{size}x{size}',
             '-quality', '78', '-strip', str(dst)],
            check=True, capture_output=True,
        )
        return True
    except (_sub.SubprocessError, FileNotFoundError):
        pass
    try:
        from PIL import Image
        with Image.open(str(src)) as im:
            im = im.convert('RGB')
            # Center-square crop
            w, h = im.size
            s = min(w, h)
            im = im.crop(((w-s)//2, (h-s)//2, (w+s)//2, (h+s)//2))
            im = im.resize((size, size), Image.LANCZOS)
            im.save(str(dst), 'JPEG', quality=78, optimize=True)
        return True
    except Exception:
        return False

n_photo_downloads = 0
n_streetview_downloads = 0
n_thumb_renders = 0
for entry in seen_entries.values():
    slug = entry.get('slug')
    if not slug: continue
    photo_path = _PHOTO_DIR / f'{slug}.jpg'
    thumb_path = _THUMB_DIR / f'{slug}.jpg'

    if not photo_path.exists():
        pe = PLACES_CACHE.get(entry.get('_cacheKey', '')) or {}
        photo_ref = pe.get('photoRef')
        # Backfill photoRef from place_details when missing — every kept
        # entry deserves a thumbnail, not just bot-eligible ones. Costs
        # ~$0.025 per first-time fetch then cached forever.
        if (pe.get('status') == 'ok' and pe.get('place_id') and not photo_ref):
            try:
                det = _pd(pe['place_id'])
                photos = det.get('photos') or []
                if photos:
                    photo_ref = photos[0].get('photo_reference')
                    pe['photoRef'] = photo_ref
                    PLACES_CACHE[entry['_cacheKey']] = pe
            except Exception: pass
        # 1) Try Places photo
        if photo_ref:
            data, _ = _dl_photo(photo_ref, max_width=1600)
            if data:
                photo_path.write_bytes(data); n_photo_downloads += 1
        # 2) Fall back to Street View (free metadata check first; only
        # pay the ~$0.007 image fetch when imagery actually exists).
        if (not photo_path.exists()
                and entry.get('lat') is not None and entry.get('lng') is not None):
            meta = _sv_meta(entry['lat'], entry['lng'])
            if meta and meta.get('status') == 'OK':
                data, _ = _sv_img(entry['lat'], entry['lng'], size='640x640', fov=80)
                if data:
                    photo_path.write_bytes(data); n_streetview_downloads += 1

    if photo_path.exists():
        # Make sure thumbnail exists too (regen when full photo is fresher)
        if not thumb_path.exists() or thumb_path.stat().st_mtime < photo_path.stat().st_mtime:
            if _make_thumb(photo_path, thumb_path, size=160):
                n_thumb_renders += 1
        entry['photo'] = f'/og/photo/{slug}.jpg'
        if thumb_path.exists():
            entry['thumb'] = f'/og/thumb/{slug}.jpg'

print(f"  photos: {n_photo_downloads} new Places + {n_streetview_downloads} new Street View "
      f"(total entries with photos: {sum(1 for e in seen_entries.values() if e.get('photo'))}; "
      f"{n_thumb_renders} thumbnails regenerated)")

opens_365_by_cuisine = defaultdict(list)
for entry in seen_entries.values():
    n_tagged_365 += 1
    if entry['daysOpen'] <= 30: n_tagged_30 += 1
    for c in entry.get('cuisines') or [entry['cuisine']]:
        opens_365_by_cuisine[c].append(entry)

print(f"  verification gate: kept {n_tagged_365}, dropped {n_dropped_chain_osm} OSM-known chains + {n_dropped_validator} validator (Haiku: chain/institutional/ghost) + {n_dropped_unverified} unverified (no Places, no web_verify yet) + {n_dropped_closed} closed/temp + {n_dropped_instore} in-store kiosks + {n_dropped_institutional} institutional-operator rows + {n_dropped_weak_match} weak-match (no Places / no site / name-guess only) + {n_dropped_brand_new_unverified} brand-new-unverified (<30d, no Places/website) + {n_deduped} duplicate rows collapsed")

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

# Prune cuisines_dynamic.json — keep only keys that are actually IN USE by
# the current feed. Sub-cuisines collapsed by the prompt's parent-country
# rule (e.g., Sichuan → Chinese) leave orphan entries in the dynamic dict
# that would otherwise clutter the cuisine dropdown forever. Seed
# (curated) cuisines in CUISINE_LABEL are never pruned — they may have 0
# entries today but reappear tomorrow.
try:
    from cuisines import _load_dynamic, _save_dynamic, _DYNAMIC_PATH
    in_use = {c['key'] for c in cuisines_out}
    dyn = _load_dynamic()
    pruned = {k: v for k, v in dyn.items() if k in in_use}
    if len(pruned) != len(dyn):
        print(f"  pruned {len(dyn) - len(pruned)} unused dynamic cuisines from {_DYNAMIC_PATH.name}")
        _save_dynamic(pruned)
except Exception as ex:
    print(f"  WARN: dynamic-cuisine prune failed: {ex}")

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
        # Address link: ONLY the Places CID deep-link (mapsUrl). When there's
        # no Places match, show plain text — a name+address search lands on
        # neighboring businesses and an address-only search lands on a
        # generic building. Plain text is more honest.
        addr_url = r.get('mapsUrl') or ''
        addr_inner = f'<a href="{_esc(addr_url)}" rel="noopener">{addr}</a>' if addr_url and addr else addr
        addr_html = f'{addr_inner}<span class="oad-d"> · {district}</span>' if district else addr_inner
        ago = _esc(_ago(r['daysOpen']))
        # Name + thumbnail link precedence: own website > Places deep-link >
        # our own /r/<slug> listing page. The listing page is the always-
        # available fallback — never sends the user to a wrong business
        # or a generic-building Maps result, and the page itself has cuisine,
        # address, district, breadcrumb back to the cuisine hub. Better than
        # a dead row when external URLs are missing.
        slug = r.get('slug') or ''
        internal_url = f'/r/{slug}' if slug else ''
        link = r.get('website') or r.get('mapsUrl') or internal_url
        name_html = f'<a href="{_esc(link)}" rel="noopener">{name}</a>' if link else name
        multi_attr = ' data-multi' if len(cuisine_keys) > 1 else ''
        thumb = r.get('thumb')
        thumb_target = r.get('website') or r.get('mapsUrl') or internal_url
        thumb_html = (f'<a class="row-pic-link" href="{_esc(thumb_target)}" rel="noopener" aria-label="View {_esc(r["operatingName"])}">'
                      f'<img class="row-pic" src="{_esc(thumb)}" alt="" loading="lazy" decoding="async">'
                      f'</a>'
                      if thumb and thumb_target else
                      f'<img class="row-pic" src="{_esc(thumb)}" alt="" loading="lazy" decoding="async">'
                      if thumb else '')
        out.append(
            f'<div class="open-row{ " has-pic" if thumb else "" }"{multi_attr}>'
            f'{thumb_html}'
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


def build_ld_collectionpage(itemlist, *, url, dateModified):
    """Wrap an ItemList in CollectionPage so it carries url + dateModified
    (ItemList itself has no dateModified property). Boosts the freshness
    signal Google reads — the whole point of the daily refresh."""
    return {
        '@context': 'https://schema.org',
        '@type': 'CollectionPage',
        'url': url,
        'name': itemlist['name'],
        'description': itemlist['description'],
        'inLanguage': 'en-CA',
        'dateModified': dateModified,
        'isPartOf': {'@type': 'WebSite', 'name': 'NowServingTO',
                     'url': 'https://nowservingto.com/'},
        'mainEntity': {k: v for k, v in itemlist.items() if k != '@context'},
    }


def build_ld_breadcrumb(parts):
    """parts: list of (name, url) tuples in trail order. Returns a
    schema.org BreadcrumbList — drives the breadcrumb display Google
    sometimes substitutes for the URL in SERP results, generally lifting CTR."""
    return {
        '@context': 'https://schema.org',
        '@type': 'BreadcrumbList',
        'itemListElement': [
            {'@type': 'ListItem', 'position': i, 'name': name, 'item': url}
            for i, (name, url) in enumerate(parts, 1)
        ],
    }


def build_ld_faq(qa_pairs):
    """qa_pairs: list of (question, answer) tuples. Returns FAQPage schema.
    Google has tightened FAQ rich-result eligibility (mostly gov/health now)
    but the structured data still helps the page rank for the underlying
    'how' / 'what' / 'where' query family."""
    return {
        '@context': 'https://schema.org',
        '@type': 'FAQPage',
        'mainEntity': [
            {'@type': 'Question', 'name': q,
             'acceptedAnswer': {'@type': 'Answer', 'text': a}}
            for q, a in qa_pairs
        ],
    }


def build_xaxis_html(entries, *, axis_label, group_fn, h3_template, anchor_prefix):
    """Build a compound-query SEO block: groups `entries` by `group_fn(entry)`,
    emits one h3 per group. Each h3 targets the `cuisine + district` query
    class directly ("Pakistani restaurants in Etobicoke") without requiring a
    separate URL per combination.

    Wrapped in <details> with the axis_label as the <summary>. Per Google's
    official guidance (2019, reaffirmed since), content inside disclosure
    widgets is indexed at full ranking value — but visually stays out of
    the way until a user expands it. Addresses are omitted from the list
    items because they're already shown in the chronological feed above
    (the duplication looked spammy + cluttered).
    """
    buckets = defaultdict(list)
    for e in entries:
        k = group_fn(e)
        if k: buckets[k].append(e)
    if not buckets: return ''
    sorted_groups = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    def _anchor(label):
        return anchor_prefix + _re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')

    blocks = []
    for label, ents in sorted_groups:
        anchor = _anchor(label)
        n = len(ents)
        ents_sorted = sorted(ents, key=lambda r: r.get('issuedDate', ''), reverse=True)
        items = []
        for r in ents_sorted:
            slug = r.get('slug')
            href = f'/r/{slug}' if slug else (r.get('website') or '#')
            name = _esc(r.get('operatingName') or '')
            items.append(f'<li><a href="{_esc(href)}">{name}</a></li>')
        blocks.append(
            f'<div class="xa-block" id="{anchor}">'
            f'<h3>{_esc(h3_template.format(label=label))} '
            f'<span class="ct">({n})</span></h3>'
            f'<ul class="xa-list">{"".join(items)}</ul>'
            f'</div>'
        )
    return (f'<section class="x-axis">'
            f'<details>'
            f'<summary>{_esc(axis_label)}</summary>'
            f'<div class="x-axis-body">{"".join(blocks)}</div>'
            f'</details>'
            f'</section>')


def build_breadcrumb_html(parts):
    """Visible breadcrumb HTML matching the BreadcrumbList JSON-LD. parts:
    list of (name, url-or-None); the last entry has url=None (current page,
    rendered as text with aria-current)."""
    items = []
    for name, url in parts:
        if url:
            items.append(f'<a href="{_esc(url)}">{_esc(name)}</a>')
        else:
            items.append(f'<span aria-current="page">{_esc(name)}</span>')
    return ('<nav class="breadcrumb" aria-label="Breadcrumb">'
            + '<span class="sep">›</span>'.join(items)
            + '</nav>')


import re

def inject_into_html(html, *, static_block, ld_payloads, breadcrumb_html='', xaxis_html=''):
    """Replace STATIC-FEED, LD-ITEMLIST, and BREADCRUMB marker blocks.

    `ld_payloads` is a list of schema.org dicts (ItemList / CollectionPage /
    BreadcrumbList / FAQPage). Each is emitted as its own <script> tag —
    Google parses them all independently and never penalizes multiple
    JSON-LD blocks on a page.

    Lambda replacements keep backslash sequences in the replacement (e.g.
    \\uXXXX in JSON-LD) from being interpreted as regex backreferences.
    """
    html = re.sub(
        r'(<!-- STATIC-FEED-START[^>]*-->).*?(<!-- STATIC-FEED-END -->)',
        lambda m: m.group(1) + '\n    ' + static_block + '\n    ' + m.group(2),
        html, count=1, flags=re.DOTALL,
    )
    scripts = []
    for i, p in enumerate(ld_payloads):
        ld_json_str = json.dumps(p, separators=(',', ':'))
        sid = ' id="ld-itemlist"' if i == 0 else ''
        scripts.append(f'<script type="application/ld+json"{sid}>{ld_json_str}</script>')
    html = re.sub(
        r'(<!-- LD-ITEMLIST-START -->).*?(<!-- LD-ITEMLIST-END -->)',
        lambda m: m.group(1) + '\n' + '\n'.join(scripts) + '\n' + m.group(2),
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'(<!-- BREADCRUMB-START -->).*?(<!-- BREADCRUMB-END -->)',
        lambda m: m.group(1) + breadcrumb_html + m.group(2),
        html, count=1, flags=re.DOTALL,
    )
    html = re.sub(
        r'(<!-- XAXIS-START -->).*?(<!-- XAXIS-END -->)',
        lambda m: m.group(1) + xaxis_html + m.group(2),
        html, count=1, flags=re.DOTALL,
    )
    return html


# ---------------------------------------------------------------------------
# Inject into the HOMEPAGE (index.html).
# ---------------------------------------------------------------------------
top_for_static = all_recent[:30]
static_block = build_static_rows(top_for_static)
home_url = 'https://nowservingto.com/'
home_itemlist = build_ld_itemlist(
    top_for_static,
    name="Toronto's newest restaurants by cuisine",
    description='Restaurants newly licensed in Toronto in the past 365 days, classified by cuisine.',
)
home_collection = build_ld_collectionpage(
    home_itemlist, url=home_url, dateModified=REFERENCE_DATE.isoformat(),
)
try:
    home_html = open(INDEX_PATH).read()
    # Homepage gets no breadcrumb (it IS the root) — just the CollectionPage
    # wrapper to carry dateModified + url; no extra BreadcrumbList script.
    home_html = inject_into_html(home_html,
        static_block=static_block, ld_payloads=[home_collection], breadcrumb_html='')
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
    all_for_cuisine = opens_365_by_cuisine.get(key, [])
    entries = all_for_cuisine[:30]   # top 30 power the chronological feed + ItemList
    if not entries: continue

    title = f"New {label} restaurants in Toronto — NowServingTO"
    desc = (f"Every newly licensed {label} restaurant in Toronto over the past 365 "
            f"days, updated daily. {n365} entries tracked, {n30} from the last 30 days.")
    canonical = f"https://nowservingto.com/cuisine/{key}"

    page = template
    # Replace meta tags — first occurrence each.
    page = re.sub(r'<title>[^<]*</title>', f'<title>{_esc(title)}</title>', page, count=1)
    for sel, val in [
        (r'(<meta name="description" content=")[^"]*(")',         desc),
        (r'(<meta property="og:title" content=")[^"]*(")',        title),
        (r'(<meta property="og:description" content=")[^"]*(")',  desc),
        (r'(<meta property="og:url" content=")[^"]*(")',          canonical),
        (r'(<meta name="twitter:title" content=")[^"]*(")',       title),
        (r'(<meta name="twitter:description" content=")[^"]*(")', desc),
        (r'(<link rel="canonical" href=")[^"]*(")',               canonical),
    ]:
        page = re.sub(sel, lambda m, v=val: m.group(1) + _esc(v) + m.group(2),
                      page, count=1)

    # Replace the homepage's <h1 class="sub"> with a cuisine-specific
    # one. Format follows the user-specified pattern (2026-05-16):
    # "Toronto's newest <Label> cuisine" — targets the "newest <Label>
    # cuisine Toronto" / "<Label> cuisine Toronto" query family.
    cuisine_h1 = (f'<h1 class="sub">Toronto\'s <span class="hl">newest</span> '
                  f'{_esc(label)} cuisine</h1>')
    page = re.sub(r'<h1 class="sub">[\s\S]*?</h1>',
                  lambda m: cuisine_h1, page, count=1)

    # Replace STATIC-FEED + LD-ITEMLIST with cuisine-scoped versions.
    cuisine_static = build_static_rows(entries)
    cuisine_itemlist = build_ld_itemlist(
        entries,
        name=f"Newest {label} restaurants in Toronto",
        description=desc,
    )
    cuisine_collection = build_ld_collectionpage(
        cuisine_itemlist, url=canonical, dateModified=REFERENCE_DATE.isoformat(),
    )
    cuisine_breadcrumb_parts = [
        ('Home',     'https://nowservingto.com/'),
        (f'{label} restaurants', None),
    ]
    cuisine_breadcrumb_ld = build_ld_breadcrumb([
        ('Home', 'https://nowservingto.com/'),
        (f'{label} restaurants', canonical),
    ])
    cuisine_faq = build_ld_faq([
        (f"How often is the {label} restaurant list updated?",
         f"Daily. Every morning we pull the latest City of Toronto business "
         f"licences open data and re-classify any new entries."),
        (f"Where does the {label} restaurant data come from?",
         f"The City of Toronto's Municipal Licensing and Standards open dataset "
         f"of active business licences, cross-checked against Google Places to "
         f"confirm the business is currently operating."),
        (f"How is a restaurant classified as {label}?",
         f"An AI model (Anthropic Claude) reviews the operating name, website "
         f"content, and Google Places category to determine the cuisine. "
         f"Multi-cuisine spots get tagged with every applicable cuisine."),
    ])
    # Compound-query section: bucket THIS cuisine's full 365d list by district.
    # H3 per district hits the "<Cuisine> restaurants in <District>" query
    # family without us having to spin up cuisine×district URLs.
    cuisine_xaxis = build_xaxis_html(
        all_for_cuisine,
        axis_label=f'Browse {label} restaurants by Toronto district',
        group_fn=lambda e: e.get('district'),
        h3_template=f'{label} restaurants in {{label}}',
        anchor_prefix='in-',
    )
    page = inject_into_html(
        page,
        static_block=cuisine_static,
        ld_payloads=[cuisine_collection, cuisine_breadcrumb_ld, cuisine_faq],
        breadcrumb_html=build_breadcrumb_html(cuisine_breadcrumb_parts),
        xaxis_html=cuisine_xaxis,
    )

    (CUISINE_DIR / f'{key}.html').write_text(page)
    cuisine_pages_written += 1
print(f"  wrote {cuisine_pages_written} per-cuisine SEO landing pages → cuisine/<key>.html")


# ---------------------------------------------------------------------------
# Per-DISTRICT landing pages at district/<slug>.html — parallels the
# /cuisine/ pages but bucketed by Toronto district (Downtown, East Toronto,
# Etobicoke, North York, Scarborough, West Toronto). Targets queries like
# "new restaurants Scarborough" that have real volume and almost no ranked
# competition. Same template + h1/title/og treatment as per-cuisine pages.
DISTRICT_DIR = Path(ROOT) / 'district'
DISTRICT_DIR.mkdir(exist_ok=True)
# Group entries by district from the in-memory feed (no extra inject pass).
by_district = defaultdict(list)
for entry in seen_entries.values():
    d = (entry.get('district') or '').strip()
    if d: by_district[d].append(entry)
# Sort each district's list freshest-first
for d in by_district:
    by_district[d].sort(key=lambda r: r['issuedDate'], reverse=True)

# slugify: "East Toronto" → "east-toronto"
def _district_slug(label):
    return _re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')

district_template = open(INDEX_PATH).read()
district_pages_written = 0
for label, entries in by_district.items():
    if not entries: continue
    slug = _district_slug(label)
    n365 = len(entries)
    n30  = sum(1 for e in entries if e['daysOpen'] <= 30)

    # Use "in Downtown Toronto" when label is "Downtown" — reads better.
    place = f'{label} Toronto' if label == 'Downtown' else label
    title = f"New restaurants in {place} — NowServingTO"
    desc = (f"Every newly licensed restaurant in {place}, by cuisine, updated "
            f"daily. {n365} entries tracked, {n30} from the last 30 days.")
    canonical = f"https://nowservingto.com/district/{slug}"

    page = district_template
    # Replace meta tags
    page = re.sub(r'<title>[^<]*</title>', f'<title>{_esc(title)}</title>', page, count=1)
    for sel, val in [
        (r'(<meta name="description" content=")[^"]*(")',         desc),
        (r'(<meta property="og:title" content=")[^"]*(")',        title),
        (r'(<meta property="og:description" content=")[^"]*(")',  desc),
        (r'(<meta property="og:url" content=")[^"]*(")',          canonical),
        (r'(<meta name="twitter:title" content=")[^"]*(")',       title),
        (r'(<meta name="twitter:description" content=")[^"]*(")', desc),
        (r'(<link rel="canonical" href=")[^"]*(")',               canonical),
    ]:
        page = re.sub(sel, lambda m, v=val: m.group(1) + _esc(v) + m.group(2),
                      page, count=1)

    # District-specific h1
    district_h1 = (f'<h1 class="sub">New <span class="hl">restaurants</span> '
                   f'in {_esc(place)}</h1>')
    page = re.sub(r'<h1 class="sub">[\s\S]*?</h1>',
                  lambda m: district_h1, page, count=1)

    # District-scoped static feed (top 30) + structured data set
    district_static = build_static_rows(entries[:30])
    district_itemlist = build_ld_itemlist(
        entries[:30],
        name=f"Newest restaurants in {place}",
        description=desc,
    )
    district_collection = build_ld_collectionpage(
        district_itemlist, url=canonical, dateModified=REFERENCE_DATE.isoformat(),
    )
    district_breadcrumb_parts = [
        ('Home', 'https://nowservingto.com/'),
        (f'Restaurants in {place}', None),
    ]
    district_breadcrumb_ld = build_ld_breadcrumb([
        ('Home', 'https://nowservingto.com/'),
        (f'Restaurants in {place}', canonical),
    ])
    district_faq = build_ld_faq([
        (f"How often is the {place} restaurant list updated?",
         f"Daily. We pull fresh City of Toronto business-licences data every "
         f"morning and re-classify any new entries."),
        (f"What counts as {place} in this directory?",
         f"We use the postal-code prefix on each business licence (FSA) to "
         f"map every restaurant to one of six Toronto districts: Downtown, "
         f"East Toronto, West Toronto, North York, Scarborough, or Etobicoke."),
        (f"Where does the {place} restaurant data come from?",
         f"The City of Toronto's Municipal Licensing and Standards open "
         f"dataset of active business licences, cross-checked against "
         f"Google Places to confirm operating status."),
    ])
    # Compound-query section: bucket THIS district's 365d list by primary
    # cuisine. H3 per cuisine hits the "<Cuisine> restaurants in <District>"
    # query family — same pattern as the cuisine page's by-district section,
    # so either page can rank for the compound query depending on link weight.
    def _cuisine_label_of(e):
        keys = e.get('cuisines') or ([e.get('cuisine')] if e.get('cuisine') else [])
        return CUISINE_LABEL.get(keys[0], keys[0].replace('_', ' ').title()) if keys else None
    district_xaxis = build_xaxis_html(
        entries,
        axis_label=f'Browse restaurants in {place} by cuisine',
        group_fn=_cuisine_label_of,
        h3_template=f'{{label}} restaurants in {place}',
        anchor_prefix='type-',
    )
    page = inject_into_html(
        page,
        static_block=district_static,
        ld_payloads=[district_collection, district_breadcrumb_ld, district_faq],
        breadcrumb_html=build_breadcrumb_html(district_breadcrumb_parts),
        xaxis_html=district_xaxis,
    )

    (DISTRICT_DIR / f'{slug}.html').write_text(page)
    district_pages_written += 1
print(f"  wrote {district_pages_written} per-district SEO landing pages → district/<slug>.html")


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
from enrich_places import download_place_photo, streetview_metadata, streetview_image
LISTING_DIR = Path(ROOT) / 'r'
OG_DIR      = Path(ROOT) / 'og'
PHOTO_DIR   = Path(ROOT) / 'og' / 'photo'
LISTING_DIR.mkdir(exist_ok=True)
OG_DIR.mkdir(exist_ok=True)
PHOTO_DIR.mkdir(exist_ok=True)

listing_template = open(INDEX_PATH).read()
n_listing_html = 0
n_listing_png  = 0
n_listing_photo = 0
n_listing_streetview = 0
for entry in seen_entries.values():
    slug = entry.get('slug')
    if not slug: continue

    # 1) PNG card → og/<slug>.png — branded fallback
    try:
        _render_og_card(entry, out_path=str(OG_DIR / f'{slug}.png'))
        n_listing_png += 1
    except Exception as ex:
        print(f"  WARN: og card failed for {slug}: {ex}")
        continue

    # Photo file path (downloads happened in the pre-pass above).
    photo_file = PHOTO_DIR / f'{slug}.jpg'

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
    # Prefer the actual restaurant photo (Places) when we have one; falls
    # back to the branded SVG card. Photo gives the X/FB/Slack card a real
    # food/storefront image instead of generic typography.
    og_image  = (f"https://nowservingto.com/og/photo/{slug}.jpg" if photo_file.exists()
                 else f"https://nowservingto.com/og/{slug}.png")

    page = listing_template
    page = re.sub(r'<title>[^<]*</title>',
                  lambda m: f'<title>{_esc(title)}</title>', page, count=1)
    for sel, val in [
        (r'(<meta name="description" content=")[^"]*(")',         desc),
        (r'(<meta property="og:title" content=")[^"]*(")',        title),
        (r'(<meta property="og:description" content=")[^"]*(")',  desc),
        (r'(<meta property="og:url" content=")[^"]*(")',          canonical),
        (r'(<meta property="og:image" content=")[^"]*(")',        og_image),
        # Match the card's actual 1200×675 dimensions (the template defaults
        # to 1200×630 for the homepage og.svg, which is a different image).
        (r'(<meta property="og:image:width" content=")[^"]*(")',  '1200'),
        (r'(<meta property="og:image:height" content=")[^"]*(")', '675'),
        (r'(<meta name="twitter:title" content=")[^"]*(")',       title),
        (r'(<meta name="twitter:description" content=")[^"]*(")', desc),
        (r'(<meta name="twitter:image" content=")[^"]*(")',       og_image),
        (r'(<link rel="canonical" href=")[^"]*(")',               canonical),
    ]:
        page = re.sub(sel, lambda m, v=val: m.group(1) + _esc(v) + m.group(2),
                      page, count=1)

    # Replace the homepage's <h1 class="sub"> with this listing's name.
    listing_h1 = f'<h1 class="sub">{_esc(name)}</h1>'
    page = re.sub(r'<h1 class="sub">[\s\S]*?</h1>',
                  lambda m: listing_h1, page, count=1)

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
    # Breadcrumb: Home → {Cuisine} restaurants → {Name}. Lifts SERP CTR
    # and ties this listing back to its cuisine landing page so Google
    # sees them as a hub + spokes for the cuisine query.
    cuisine_slug = primary_key
    listing_breadcrumb_parts = [('Home', 'https://nowservingto.com/')]
    listing_breadcrumb_ld_parts = [('Home', 'https://nowservingto.com/')]
    if cuisine_slug:
        cu_url = f'https://nowservingto.com/cuisine/{cuisine_slug}'
        listing_breadcrumb_parts.append((f'{primary_lbl} restaurants', cu_url))
        listing_breadcrumb_ld_parts.append((f'{primary_lbl} restaurants', cu_url))
    listing_breadcrumb_parts.append((name, None))
    listing_breadcrumb_ld_parts.append((name, canonical))
    listing_breadcrumb_ld = build_ld_breadcrumb(listing_breadcrumb_ld_parts)
    page = inject_into_html(
        page,
        static_block=one_row,
        ld_payloads=[listing_ld, listing_breadcrumb_ld],
        breadcrumb_html=build_breadcrumb_html(listing_breadcrumb_parts),
    )

    (LISTING_DIR / f'{slug}.html').write_text(page)
    n_listing_html += 1

print(f"  wrote {n_listing_html} per-listing pages → r/<slug>.html")
print(f"  wrote {n_listing_png} per-listing OG cards → og/<slug>.png")

# Persist any photoRef values we backfilled into PLACES_CACHE so the next
# inject doesn't have to re-call place_details for the same entries.
try:
    with open(PLACES_CACHE_PATH, 'w') as f:
        json.dump(PLACES_CACHE, f, separators=(',', ':'))
except Exception as ex:
    print(f"  WARN: places_cache save failed: {ex}")

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
# Per-district landing pages — same priority as cuisines.
for label in by_district:
    if not by_district[label]: continue
    slug = _district_slug(label)
    url_blocks.append(
        f'  <url>\n    <loc>{SITE_BASE}/district/{slug}</loc>\n    <lastmod>{REFERENCE_DATE.isoformat()}</lastmod>\n    <changefreq>daily</changefreq>\n    <priority>0.8</priority>\n  </url>'
    )
# Per-listing pages — every kept entry. Mid-tier priority (0.5) since each
# is thin-ish on its own, but Google needs to see them in the sitemap to
# discover them — they were completely absent before, which is why the
# GSC "Discovered - not indexed" count tracks the cuisine/district pages,
# not the 444 r/<slug> pages Google doesn't even know about.
# lastmod = each entry's actual issued date so Google sees stable URLs
# (revisit only when the listing's own data changes, not on every cron).
for entry in seen_entries.values():
    slug = entry.get('slug')
    if not slug: continue
    iss = entry.get('issuedDate', REFERENCE_DATE.isoformat())
    url_blocks.append(
        f'  <url>\n    <loc>{SITE_BASE}/r/{slug}</loc>\n    <lastmod>{iss}</lastmod>\n    <changefreq>monthly</changefreq>\n    <priority>0.5</priority>\n  </url>'
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

# ---------------------------------------------------------------------------
# Cleanup pass — delete stale generated files for entries that no longer
# exist in the live data. Without this, every dropped restaurant + every
# emptied-out cuisine leaves an orphaned HTML file on disk serving 200 OK
# to Google, polluting the indexing report with "discovered but not
# indexed" URLs that point at content that doesn't reflect current state.
# ---------------------------------------------------------------------------
live_cuisines  = {c['key'] for c in cuisines_out}
live_districts = {_district_slug(d) for d in by_district if by_district[d]}
live_slugs     = {e.get('slug') for e in seen_entries.values() if e.get('slug')}

def _cleanup(directory, live_keys, suffix='.html'):
    if not directory.exists(): return 0
    removed = 0
    for f in directory.iterdir():
        if not f.is_file() or not f.name.endswith(suffix): continue
        key = f.name[:-len(suffix)]
        if key not in live_keys:
            try:
                f.unlink()
                removed += 1
            except Exception as ex:
                print(f"  WARN: failed to remove stale {f}: {ex}")
    return removed

n_cuisine_stale  = _cleanup(CUISINE_DIR,  live_cuisines)
n_district_stale = _cleanup(DISTRICT_DIR, live_districts)
n_listing_stale  = _cleanup(LISTING_DIR,  live_slugs)
# Same for the per-listing OG card PNGs + photo JPGs + thumb JPGs that
# track the listing lifecycle. (og/ also holds non-listing assets like
# /og.svg — only the <slug>.png pattern matters here.)
n_og_card_stale  = _cleanup(OG_DIR,           live_slugs, suffix='.png')
n_og_photo_stale = _cleanup(OG_DIR / 'photo', live_slugs, suffix='.jpg')
n_og_thumb_stale = _cleanup(OG_DIR / 'thumb', live_slugs, suffix='.jpg')
print(f"  cleanup: removed {n_cuisine_stale} stale cuisine pages, "
      f"{n_district_stale} stale district pages, {n_listing_stale} stale listings, "
      f"{n_og_card_stale} cards, {n_og_photo_stale} photos, {n_og_thumb_stale} thumbs")

# Loud sanity check: if any recovery script tagged a cuisine that cuisines.py
# doesn't have a display label for, those entries were silently dropped above.
# Surface it so the fix (add the key to cuisines.py CUISINE_LABEL) is obvious.
if _CUISINE_LABEL_GAP:
    print()
    print(f"!!!!!!  WARNING: {len(_CUISINE_LABEL_GAP)} cuisine key(s) in cache but missing from cuisines.py CUISINE_LABEL:")
    for c in sorted(_CUISINE_LABEL_GAP):
        print(f"          {c!r}")
    print("        These entries were SILENTLY DROPPED from the feed. Add them to tools/cuisines.py.")
