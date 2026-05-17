#!/usr/bin/env python3
"""
Unified Haiku validator: one batched call per entry that sees ALL the evidence
at once — licence name, Places matched name + types + editorial + reviews,
web-verify website + evidence — and returns four orthogonal judgments:

  is_same_business : did Places match the right business? (catches EASTERN 828
                     CAFE matched to Eastern-Leslie Car Wash; KALIMERA matched
                     to The Laurel School; etc.)
  is_restaurant    : is this a consumer restaurant, vs. an institutional
                     caterer / packaged-food brand / chain / grocery counter?
  cuisines         : list of specific country buckets (multi-cuisine OK)
  best_website     : URL to use for the website link, or null if all candidates
                     are aggregator wrappers (skipthedishes/doordash/etc.) or dead

Replaces 4 separate heuristic checks (regex name-overlap, types whitelist,
URL-host pattern, body-content scan) with one AI judgment that sees the full
context. Cost: ~600 entries × ~$0.001 batch = ~$0.60 total.

Auto-fix loop after results:
  - is_same_business=no → clear places_cache entry (refetched on next cron)
  - is_restaurant=no    → tag with `_validator_drop: not-restaurant`, dropped at inject time
  - cuisines updated    → web_verify_cache.cuisine + .cuisines
  - best_website=null   → url_health_cache marks URL ok=False
  - best_website=new    → update web_verify_cache.website
"""
import json, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cuisines import VALID_CUISINE_KEYS, parse_cuisines_from_llm
from llm_verify_batch import submit_batch, poll, download_results
from llm_search_recover_batch import MODEL, API_KEY, HEADERS, http
from llm_recover_cuisine import fetch_page_text, extract_socials  # fetched text + IG/X/FB handle sweep

WEB_VERIFY_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'
PLACES_PATH = ROOT / 'tools' / 'cache' / 'places_cache.json'
URL_HEALTH_PATH = ROOT / 'tools' / 'cache' / 'url_health_cache.json'

SYSTEM_PROMPT = """You validate a directory entry for NowServingTO — a curated list
of Toronto's NEWLY LICENCED small-scale, independent, immigrant-owned ethnic-cuisine
restaurants (past 12 months).

THE AUDIENCE: Toronto residents seeking specific-country authentic spots opened by
first-generation diaspora operators — a Lebanese family café, a Sri Lankan hopper
kitchen, an Argentine empanada window, an Eritrean injera spot, a Sichuan dumpling
counter. These are the entries we WANT to surface.

We do NOT want to surface — drop with is_restaurant=no:
  - Chain franchises (Popeyes, KFC, Tim Hortons, Pizza Pizza, Subway, Mary Brown's,
    McDonald's, Starbucks, etc. — any business with brand-level multi-location presence).
  - Institutional / B2B food service (Aramark, Compass Group, Sodexo, university or
    college campus food courts, hospital cafeterias, corporate-office contract kitchens).
  - Packaged-food brands / factory outlets / wholesalers (Soma Bone Broth, Shimla
    Foods, Patel Brothers warehouse, Roma Foods factory outlet — places licensed for
    take-out at a warehouse that sell packaged goods, not prepared dishes).
  - Grocery stores / supermarkets with a counter selling packaged products (not a
    hot table or made-to-order kitchen).
  - Pan-Asian fusion blending 3+ unrelated Asian cuisines (Korean + Hawaiian + bao
    + banh mi) — that's not authentic to any one diaspora.
  - American Southern / Cajun / BBQ themed (we have no taxonomy bucket for US South).

You see the City of Toronto licence data — the SOURCE OF TRUTH for what business
should be at what address. That's:
  - Operating Name (the business name on the licence)
  - Licence Address (the legal address of operation)
  - Client Name (the corporation holding the licence — institutional / chain
    operators like ARAMARK CANADA LTD, COMPASS GROUP CANADA, TORONTO METROPOLITAN
    UNIVERSITY are obvious from this field; treat is_restaurant=no for them.
    A franchisee LLC holding 5+ Tim Hortons / McDonald's locations is also a
    chain by Client Name.)
  - Licence Category (always food-related — eating/drinking establishment,
    take-out, retail food, etc. — this is broad "food of some kind", not cuisine)
  - Conditions (free-text field from the City. Phrases like "Located inside
    Loblaws", "Located inside Sobeys", "operates inside [grocery chain]"
    indicate in-grocery-store kiosks — treat is_restaurant=no. A semicolon-
    separated tag soup including "CHAIN;" "SHARED ADDRESS;" "NO SEATING
    ACCOMMODATION;" "COMMON SEATING;" "SEATING CAPACITY UNDER 40;" — the City
    explicitly tags chains with "CHAIN;" — treat that as is_restaurant=no.)
  - Endorsements (food-category tags from the City: "FOODSTUFFS;"
    "xFRESH MEAT DEALER;" "BAKE SHOP;" "REFRESHMENTS;" "CIGARS, CIGARETTES &
    TOBACCO;" etc. A row with only "FOODSTUFFS;" or "xFRESH MEAT DEALER;" is
    likely a grocery counter / butcher / variety store, not a restaurant.)
  - Cancel Date (when populated, the licence is no longer active — operator
    surrendered the licence or the City revoked it. Treat is_restaurant=no
    regardless of other signals; the place is no longer in business under
    this licence.)

You also see supplemental evidence:
  - Google Places match (matched name + address + categories + editorial summary
    + top reviews + Places-known website)
  - The earlier Haiku web_search verifier's website + evidence
  - WEBSITE CONTENT, when available — the page is fetched either statically
    (server-rendered HTML) or, for JS-only SPAs, via a headless render
    (labelled "HOMEPAGE (jina-rendered): ..."). Treat both forms as equally
    authoritative content evidence. A multi-location list like
    "Queen St W / Eaton Centre / Square One / Vaughan Mills" is a strong
    CHAIN signal even when it surfaces only in the rendered text.
  - The name-only LLM's previous cuisine guess
  - WEBSITE CONTENT — when the Places-known website was fetchable, the homepage
    plus the most-promising linked menu / about page have been crawled, HTML
    stripped, and included as raw text. Use this content to (a) confirm the URL
    points to a real, operating restaurant (not parked domain, aggregator-wrapper
    landing page, "we moved" notice, or generic CMS shell with no menu);
    (b) extract menu-word cuisine evidence (specific dish names = strong signal;
    quoted cultural-marker phrases > generic "best food in town" copy).

Judge holistically from all of it. No rule we hardcode in Python should be doing
this work — you have richer context than any regex.

Return a single JSON object, no prose, no markdown code fences:
{
  "is_same_business":"yes"|"no"|"no_match",
  "is_restaurant":"yes"|"no",
  "cuisines":["k1","k2"]|["unknown"],
  "best_website":"<url>"|null,
  "evidence":"<one short sentence>"
}

DECISIVE — DO NOT HEDGE. Pick yes or no based on the evidence.
- "no_match" for is_same_business is ONLY for when Places returned no result
  (the message above will say "GOOGLE PLACES MATCH: (none — ...)"). Otherwise yes/no.
- is_restaurant defaults to "yes" unless evidence CLEARLY shows the business is
  institutional (Aramark/Compass cafeteria), packaged-only retail (Soma Bone Broth,
  Shimla Foods), wholesale/factory, or a major chain franchise (Popeyes/KFC/Tim
  Hortons/Pizza Pizza). Borderline cases (deli+grocery combos, retail bakery with
  hot counter, café-retail like Lindt) → "yes" (they serve walk-in customers).

RULES

is_same_business — apply this test: if a Google Maps user typed the City's
licence NAME + ADDRESS into Maps, would the Places match shown below be a
reasonable top hit — same business operation at the same address? Be
forgiving on name variations (Google's search is forgiving too); be strict
on address agreement and business type.

BE LENIENT on minor name differences — small spelling/punctuation/word-order
variations are SAME business, return "yes":
  - "OI BANH MI" vs "Ôi BÁNH MÌ" — Unicode/accent variants → yes
  - "MARY BROWN'S CHICKEN" vs "Mary Brown's Fried Chicken" → yes
  - "LENA'S ROTI & DOUBLES" vs "Lena's Roti and Doubles" — & vs and → yes
  - "EL SABOR DEL PACIFICO RESTAURANT" vs "Sabor del Pacifico" — extra
    descriptor words trimmed → yes
  - "PIZZA HOUSE INC" vs "Pizza House" — corporate-suffix dropped → yes
  - "SHAKE 'N CHICK" vs "Shake & Chick" — punctuation rendering → yes
  - Same brand transliterated / translated, same address → yes

BE STRICT on address: same brand at a different physical address ≥500m apart
is "no", not "yes". LENA'S ROTI licence 3999 Keele vs Places match LENA'S
ROTI 4207 Keele → no (different physical operation; user would land on the
wrong location).

On business TYPE — use judgment, not a checklist. Restaurant licences cover
cafes, bars, bakeries, food trucks, ghost kitchens, meal-takeaway counters,
ice cream shops — Places' `types` field can flag any of these and they're
all fine. Use the type signal AS PART OF the larger same-business question:
"is the Places match plausibly the consumer-restaurant business named on
the permit?" Cases where the answer is clearly NO (illustrative, not
exhaustive):
  - Permit clearly a cafe/grill, Places match is car_wash with car-wash
    reviews — completely different business at same address
  - Permit a food kitchen, Places match is a school/medical-spa/dentist —
    completely different
  - Permit a restaurant, Places match is a grocery store ONLY (types =
    [grocery_or_supermarket] alone, no food/restaurant) — different
    operation type
The point isn't to require exact type alignment; it's to catch
"completely-different-business-at-same-address" failures. When types are
restaurant-adjacent (bakery, cafe, bar, meal_takeaway, food, point_of_interest,
etc.), trust Haiku judgment from the FULL evidence (name overlap, address
agreement, editorial, reviews, business status).

If Places returned no match at all → "no_match" (distinct from "no"; means
we have no data to compare, not that we have data and it's wrong).

is_restaurant — does this entry belong on a directory of Toronto's NEWLY
LICENCED small-scale, independent, immigrant-owned ETHNIC-CUISINE restaurants?
  - "yes" — standalone restaurant, cafe, bar, bakery, food truck, hot-counter
    that ordinary people walk into to eat AND is recognizably anchored in a
    specific ethnic / national / regional cuisine (Vietnamese pho counter,
    Salvadoran pupuseria, Eritrean injera kitchen, Sichuan dumpling shop,
    Lebanese shawarma, Trinidadian roti, Korean BBQ, Argentine empanada
    window, etc.). Even a tiny ghost kitchen counts if humans can order
    food AND there's a clear single-country / single-diaspora identity.
  - "no" — any of:
      * Institutional caterers (Aramark/Compass cafeterias in hospitals,
        offices, universities)
      * Packaged-food brand / factory outlet (Soma Bone Broth, Shimla Foods,
        Bergamos)
      * Grocery / supermarket counter selling packaged goods, not made-to-
        order food
      * MAJOR chain franchise — household-name brands with national /
        international footprint or recognizable corporate-franchise model.
        Concrete drops: Popeyes, KFC, Mary Brown's, Tim Hortons, Pizza
        Pizza, Papa Johns, Pizzaville, McDonald's, Starbucks, Subway,
        Sushi Shop, BarBurrito, Z-Teca, FreshSlice, A&W, Wendy's, Bento
        Sushi, Sushi Q (Q's), Sushi Stop, Sushi Express franchise models.

        Do NOT drop small TORONTO-ANCHORED family multi-location operators
        (~2-8 GTA-only locations, all under one ownership, all the same
        cuisine, no formal franchising). These are the "family expanded
        and opened a second/third location" pattern — they're still the
        immigrant-owned story this directory surfaces. Concrete keeps:
        - Bamiyan Kabob (Afghan, ~5 GTA locations, one family)
        - Tanghulu Tanghulu (Chinese, 8 Toronto-area mall counters, same
          ownership)
        - Lena's Roti (Trinidadian, multiple Keele-area locations)
        - Han Tai Wan Cafe (Chinese, 2-3 north-Toronto locations)

        The distinguishing test: is the brand a national/international
        franchise where any operator can buy in? → drop. Or is it one
        family running a few branches of one cuisine, all in the GTA?
        → keep.
      * PAN-CUISINE / NON-ETHNIC-ANCHORED joints. Concretely: places whose
        menu spans multiple unrelated cuisines without a single-country
        identity — wings + poutine + Nashville chicken (North American
        comfort), burgers + pizza + shawarma (multi-region grab-bag),
        "150+ wing flavors + burgers + wraps + South Asian items" (no
        anchor), generic "fusion" with no diaspora tie-in. Also: American
        Southern / Cajun / BBQ themed (no taxonomy bucket and not a
        Toronto-immigrant story). If you can't name ONE country or one
        diaspora the menu clearly belongs to, return "no".
  - "unclear" — genuinely ambiguous (coffee + branded retail like Lindt
    Chocolate, deli + grocery combo, retail bakery with mostly-packaged
    goods).

Treat `cuisines=["unknown"]` as a warning sign for is_restaurant: if you
can't identify a cuisine after looking at the evidence, the entry likely
fails the ethnic-anchor test and is_restaurant should be "no" — NOT "yes
with unknown cuisine." A real ethnic restaurant has a recognizable
country/diaspora identity visible somewhere in the evidence.

  AGED-OUT-UNVERIFIABLE RULE (added 2026-05-15, scoped 2026-05-15 v2):
  When ALL of these hold STRICTLY, return is_restaurant="no":
    1) GOOGLE PLACES MATCH line above is literally "(none — Places returned
       no result for this name+address)" — i.e., we have zero Places data
       to compare against.
    2) NO "WEBSITE CONTENT" block appears anywhere in the user message
       above — literally absent. (If a WEBSITE CONTENT block IS shown,
       even with marketing-flavored copy or short menu fragments, this
       rule does NOT fire. Quality of the content is irrelevant to this
       rule — only its presence.)
    3) LICENCE "Days since issued" is at least 30.
  Rationale: zero Places data + zero crawled content + 30d aged means we
  have NO evidence the business exists. A fresh licence (<30d) can
  legitimately lack online presence while the operator opens; we keep
  those.

  DO NOT FIRE THIS RULE when:
    - WEBSITE CONTENT was provided, even if you judge it as thin or
      marketing-heavy. Content presence proves the URL is alive and the
      business has SOMETHING online — that's enough to clear AGED-OUT.
    - Places returned a match, even a weak one.
    - The licence is <30 days old.

  When this rule fires, set evidence to start with "AGED-OUT UNVERIFIABLE:"
  so the pipeline can schedule a monthly recheck.

cuisines — list of 1 to 3 SPECIFIC country / diaspora cuisine labels.

  Return labels in any natural casing — e.g., "Sri Lankan", "Cape Verdean",
  "Uyghur", "Persian", "Trinidadian-Chinese". The system slugifies and
  auto-registers any cuisine it hasn't seen before; no ethnicity goes
  unrecognized.

  GRANULARITY: use the PARENT COUNTRY, never regional sub-cuisines or
  dish-types. A user browsing the cuisine dropdown shouldn't have to
  scroll past five regional Italian buckets to find "Italian".

    DO use the parent country               DO NOT use the sub-region
    -------------------------------------    ----------------------------------
    "Italian"  (any region of Italy)        "Northern Italian", "Sicilian",
                                            "Tuscan", "Neapolitan", "Roman"
    "Chinese"  (any region of China)        "Sichuan", "Cantonese", "Hunan",
                                            "Shanghainese", "Hakka", "Hong Kong"
    "Indian"   (any region of India)        "Punjabi", "South Indian",
                                            "Kerala", "Gujarati", "Mughlai"
    "Mexican"  (any region of Mexico)       "Oaxacan", "Yucatecan", "Baja"
    "Japanese" (any region/style)           "Sushi", "Ramen", "Izakaya"
    "Vietnamese" (any region)               "Pho", "Banh Mi", "Hanoi-style"
    "Korean"   (any style)                  "KBBQ", "Korean BBQ", "Bibimbap"
    "Thai"     (any region)                 "Isaan", "Northern Thai"
    "Middle Eastern" or country (Lebanese,  "Shawarma", "Falafel", "Kebab",
       Syrian, Persian, etc.)               "Mediterranean"

  COUNTRY-LEVEL distinctions ARE granular enough — keep these separate:
    Tamil (Sri Lankan Tamil diaspora, distinct cuisine)
    Sri Lankan (broader, hoppers / kottu)
    Bangladeshi (vs Indian — separate national cuisine)
    Pakistani (vs Indian — separate national cuisine)
    Taiwanese (politically + culturally distinct from mainland Chinese)
    Tibetan (distinct from Chinese)
    Uyghur (distinct from Chinese — Central Asian Turkic Muslim)

  WHEN TO MULTI-LIST: two specific countries blended at the same shop
    (Korean+Japanese izakaya → ["Korean", "Japanese"], not "Asian Fusion").
    Trinidadian-Chinese roti+wonton → ["Trinidadian", "Chinese"].

  WHEN TO USE UMBRELLAS: only when evidence is genuinely multi-region
    with no specific country resolvable. "South Asian", "Caribbean",
    "Middle Eastern", "Latin American", "West African", "East African".

  WHEN TO USE ["unknown"]: pan-cuisine with no single anchor (3+ unrelated
    regions, "fusion" with no diaspora tie, generic North-American
    comfort food). is_restaurant should usually be "no" in those cases.

  Do NOT invent vague descriptors like "Asian Fusion" or "International".

best_website — the URL we should put on the entry's name link.

USER DIRECTIVE (verbatim, 2026-05-15): "Haiku will identify website from
Google Places and ingest review and, if no website link exists in places,
haiku will do a google search using business name, address and category
and retrieve the top match for review. Will review based on same criteria
plus prompt including Toronto's NEWLY LICENCED small-scale, independent,
immigrant-owned ethnic-cuisine. If match then link this to the Business
Name listing. If no relevant site found and no site in places, no link
will be applied to the business name."

Concretely:
  - PLACES PATH — when GOOGLE PLACES MATCH shows a Website per Places URL
    and WEBSITE CONTENT was fetched from it, judge the content directly.
    Return the URL when the page shows real restaurant material.
  - SEARCH-FALLBACK PATH — when Places has NO website but WEB VERIFY shows
    a Website found URL (this came from an earlier Haiku web_search using
    name + address + category — the top search match), judge that URL the
    same way. If WEBSITE CONTENT was fetched for it, evaluate that content.
    Return the URL only when the page clearly belongs to a small-scale,
    independent, ethnic-cuisine restaurant matching the licence — i.e.,
    the audience we surface. Reject if the search-found page looks like a
    chain corporate site, an unrelated business, an aggregator, or a
    different-city operator with the same name.
  - NO SITE PATH — if neither Places nor the search-fallback yields a
    judgeable real restaurant site, return null. The entry will render
    without a name link (clean UX).

When content evaluation says approve:
  - If WEBSITE CONTENT was shown above, JUDGE that content. Approve the
    URL (set best_website to it) whenever the content contains ANY of:
    a dish/menu word (gimbap, biryani, pho, injera, tacos, pupusas, etc.),
    a cuisine descriptor ("Korean", "Afghan", "Lebanese"), a hours/contact
    line, a delivery/ordering button, an "about us" sentence, or a
    location listing. Marketing-heavy copy or thin pages still count as
    long as something concrete identifies a real restaurant.
  - REQUIRE WEBSITE CONTENT for the URL to be approved. If literally no
    WEBSITE CONTENT block was shown to you for this entry's candidate URL
    (the static fetch came up dry AND jina headless render came up dry),
    return best_website=null even when Places provides a URL — Places
    doesn't probe liveness; we do, and without our own content we can't
    confirm the URL works. (Note: this rule is about CONTENT PRESENCE,
    not content quality. If a WEBSITE CONTENT block IS present, evaluate
    it as above — do NOT return null on the basis that the content seems
    sparse.)
  - Return null when the website content is bad:
      * Parked-domain placeholder (Hostinger / Namecheap "this domain is for
        sale" / GoDaddy default landing page)
      * Aggregator-wrapper (page is essentially "Order on SkipTheDishes/
        DoorDash/UberEats" with no own content)
      * "We've moved" / "Permanently closed" notice
      * Generic CMS shell ("Welcome to my new website" Squarespace default)
      * Page is just a single image or social-redirect with no text content
      * Returns the wrong business entirely (different restaurant name)
  - REJECT URLs whose host is itself an aggregator: skipthedishes.com,
    doordash.com, ubereats.com, grubhub.com, foodora.ca, menulog.com,
    seamless.com, tripadvisor.com, yelp.com, chownow.com, toasttab.com.
  - Return null if no good website exists — entry falls back to Places mapsUrl.

evidence — one short sentence quoting the strongest signal that justified the
above judgments (a review excerpt, an editorial line, a menu phrase)."""

def _days_since_issued(s):
    """Parse the Issued date out of the CSV row and return integer days
    between today and that date. None on parse failure."""
    s = (s or '').strip().split(' ')[0]
    if not s: return None
    from datetime import date as _date
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%m/%d/%Y'):
        try:
            d = datetime.strptime(s, fmt).date()
            return (_date.today() - d).days
        except ValueError:
            continue
    return None


def build_request(entry_key, verify_entry, places_entry, llm_entry=None, csv_row=None, website_text=None):
    name, _, addr = entry_key.partition('||')
    lines = [f"LICENCE (City of Toronto — source of truth for name, address, food-category):",
             f"  Operating Name:    {name}",
             f"  Licence Address:   {addr}"]
    if csv_row:
        client_name = (csv_row.get('Client Name') or '').strip()
        category    = (csv_row.get('Category') or '').strip()
        conditions  = (csv_row.get('Conditions') or '').strip()
        endorse     = (csv_row.get('Endorsements') or '').strip()
        cancel_date = (csv_row.get('Cancel Date') or '').strip()
        issued_raw  = (csv_row.get('Issued') or '').strip()
        addr2       = (csv_row.get('Licence Address Line 2') or '').strip()
        addr3       = (csv_row.get('Licence Address Line 3') or '').strip()
        if client_name: lines.append(f"  Client Name:       {client_name}")
        if category:    lines.append(f"  Licence Category:  {category}")
        if addr2:       lines.append(f"  Address Line 2:    {addr2}")
        if addr3:       lines.append(f"  Address Line 3:    {addr3}")
        if conditions:  lines.append(f"  Conditions:        {conditions[:300]}")
        if endorse:     lines.append(f"  Endorsements:      {endorse[:200]}")
        # Issued date + days-since-issued — feeds the aged-out-unverifiable
        # rule in is_restaurant: licences older than 30d with no online
        # presence get dropped (likely ghosts); younger ones stay (operator
        # may still be opening doors).
        if issued_raw:
            lines.append(f"  Issued:            {issued_raw}")
            days_since = _days_since_issued(issued_raw)
            if days_since is not None:
                lines.append(f"  Days since issued: {days_since}")
        # Cancel Date — if populated, the licence is no longer active.
        # is_restaurant should be "no" regardless of other signals.
        if cancel_date: lines.append(f"  Cancel Date:       {cancel_date}  ← LICENCE IS CANCELLED")
    lines.append("")

    if places_entry and places_entry.get('status') == 'ok':
        lines.append("GOOGLE PLACES MATCH:")
        lines.append(f"  Matched Name:    {places_entry.get('matchedName', '?')}")
        lines.append(f"  Matched Address: {places_entry.get('matchedAddress', '?')}")
        if places_entry.get('types'):
            lines.append(f"  Types: {', '.join(places_entry['types'][:6])}")
        if places_entry.get('editorialSummary'):
            lines.append(f"  Editorial: {places_entry['editorialSummary'][:300]}")
        if places_entry.get('reviews'):
            lines.append(f"  Top reviews:")
            for r in places_entry['reviews'][:3]:
                lines.append(f"   - {r[:200]}")
        if places_entry.get('website'):
            lines.append(f"  Website per Places: {places_entry['website']}")
        if places_entry.get('businessStatus'):
            lines.append(f"  Business status: {places_entry['businessStatus']}")
    else:
        lines.append("GOOGLE PLACES MATCH: (none — Places returned no result for this name+address)")
    lines.append("")

    lines.append("WEB VERIFY (earlier Haiku web_search):")
    if verify_entry.get('synthesized_for_validator'):
        lines.append("  (no web_verify entry — this entry surfaced via Places match alone)")
    else:
        vw = verify_entry.get('website')
        if vw: lines.append(f"  Website found: {vw}")
        ev = verify_entry.get('evidence')
        if ev: lines.append(f"  Evidence: {ev[:300]}")
        cur_cuisine = verify_entry.get('cuisines') or [verify_entry.get('cuisine')]
        lines.append(f"  Current cuisine tag(s): {cur_cuisine}")

    # Name-only LLM guess from llm_cuisine_cache — useful for Haiku to see what
    # the name-only-Haiku previously concluded, and to either confirm or override
    # when richer evidence (Places types/editorial/reviews) is also visible above.
    if llm_entry and llm_entry.get('status') == 'ok':
        lc = llm_entry.get('cuisines') or [llm_entry.get('cuisine')]
        lines.append(f"  Name-only LLM previously guessed: {lc}")

    # Website content (folded crawl): homepage + menu/about page stripped text.
    # When present, Haiku can verify the URL points to a real restaurant site
    # (not parked domain, not aggregator-wrapper, not "we moved" notice) AND
    # extract menu-word cuisine evidence directly. When the content is bad/empty,
    # Haiku should set best_website=null.
    if website_text:
        lines.append("")
        lines.append("WEBSITE CONTENT (homepage + menu/about page text, stripped):")
        lines.append(website_text[:3500])

    return {
        'params': {
            'model': MODEL,
            'max_tokens': 300,
            'system': SYSTEM_PROMPT,
            'messages': [{'role': 'user', 'content': '\n'.join(lines)}],
        },
    }

def parse_result(msg):
    import re
    text_blocks = [b.get('text','') for b in msg.get('content', []) if b.get('type') == 'text']
    text = (text_blocks[-1] if text_blocks else '').strip()
    # Strip markdown code fences — Haiku often wraps pretty-printed JSON in ```json ... ```
    text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text)
    parsed = None
    # Try whole text first (handles multi-line JSON)
    try:
        parsed = json.loads(text)
    except Exception:
        # Fallback: extract first {...} block in the text, multi-line greedy
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try: parsed = json.loads(m.group(0))
            except Exception: pass
    if parsed is None:
        return None
    isb = parsed.get('is_same_business')
    isr = parsed.get('is_restaurant')
    cuisines = parse_cuisines_from_llm(parsed)
    bw = parsed.get('best_website')
    if bw and not isinstance(bw, str): bw = None
    if bw and not bw.startswith(('http://', 'https://')): bw = None
    return {
        'is_same_business': isb if isb in ('yes','no','unclear') else 'unclear',
        'is_restaurant':    isr if isr in ('yes','no','unclear') else 'unclear',
        'cuisines':         cuisines,
        'best_website':     bw,
        'evidence':         (parsed.get('evidence') or '')[:300],
    }

def main():
    wv = json.loads(WEB_VERIFY_PATH.read_text())
    pc = json.loads(PLACES_PATH.read_text()) if PLACES_PATH.exists() else {}
    health = json.loads(URL_HEALTH_PATH.read_text()) if URL_HEALTH_PATH.exists() else {}
    llm_cache_path = ROOT / 'tools' / 'cache' / 'llm_cuisine_cache.json'
    llm = json.loads(llm_cache_path.read_text()) if llm_cache_path.exists() else {}

    # Index the City CSV by name||address so Haiku gets the full licence row
    # (Client Name, Category, Conditions) — these encode the institutional /
    # in-store-kiosk / chain-franchisee signals that we previously hard-coded
    # into inject_openings as Python rules. Now Haiku judges from the data.
    import csv as _csv
    csv_index = {}
    csv_path = Path('/tmp/business_licences_alt.csv')
    if csv_path.exists():
        with csv_path.open(encoding='utf-8', errors='replace') as f:
            for row in _csv.DictReader(f):
                # NOTE: index CANCELLED rows too. Previously dropped at this
                # stage; now passed through so the validator sees Cancel Date
                # populated and can mark cached-but-cancelled entries as
                # is_restaurant=no (licence no longer active).
                n = (row.get('Operating Name') or '').strip().upper()
                a = ((row.get('Licence Address Line 1') or '').strip() + ' ' + (row.get('Licence Address Line 3') or '').strip()).strip().upper()
                if n and a:
                    csv_index[f"{n}||{a}"] = row

    # Targets: every entry that the inject pipeline would consider operating —
    # i.e., either Places returned OPERATIONAL or web_verify said operating=yes.
    # This catches Places-only entries (e.g., JOLLOF KING) that never went
    # through web_verify and so were never validated before — they relied on
    # name-only LLM for cuisine without seeing any Places signal in context.
    target_keys = set()
    for k, e in wv.items():
        if e.get('status') == 'ok' and e.get('operating') == 'yes':
            target_keys.add(k)
    for k, p in pc.items():
        if p.get('status') == 'ok' and p.get('businessStatus') == 'OPERATIONAL':
            target_keys.add(k)
    # Ensure every target has SOMETHING in web_verify so the apply loop can
    # write back to it. Synthesize a stub for Places-only entries — same
    # invariant verification_for() relies on.
    for k in target_keys:
        if k not in wv:
            wv[k] = {'status': 'ok', 'operating': 'yes', 'cuisine': None, 'cuisines': None,
                     'synthesized_for_validator': True}
    # Skip entries validated in the last 24h — avoids re-spending on already-
    # judged entries. Pass --force on the command line to re-validate everything.
    # AGED-OUT-UNVERIFIABLE drops carry a validator_recheck_after timestamp
    # 30 days out; honor it so we don't re-judge a ghost entry every day.
    force = '--force' in sys.argv
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    if not force:
        filtered = set()
        for k in target_keys:
            e = wv[k]
            ra = e.get('validator_recheck_after')
            if ra and ra > now_iso:
                continue  # under monthly-recheck embargo
            if not e.get('validated_at') or e['validated_at'] < cutoff_iso:
                filtered.add(k)
        target_keys = filtered
    targets = sorted(target_keys)
    print(f"Entries to validate: {len(targets)}")
    print(f"  estimated cost (~$0.001 each): ${len(targets)*0.001:.2f}")
    if not targets: return

    # --- Folded website crawl ----------------------------------------------
    # For every target with a Places-provided website that isn't an obvious
    # aggregator host, fetch homepage + menu/about page text in parallel and
    # cache it in website_text_cache.json. Haiku then sees the actual page
    # content and can decide:
    #   - is the URL a real live restaurant site?
    #   - does the content match cuisine claims?
    #   - parked domain / "we moved" / wrong-business / aggregator-wrapper?
    # The aggregator host short-circuit avoids spending bandwidth on URLs
    # Haiku will reject from the hostname alone.
    AGG_HOSTS = (
        'skipthedishes.', 'doordash.', 'ubereats.', 'grubhub.', 'foodora.',
        'menulog.', 'seamless.', 'tripadvisor.', 'yelp.', 'chownow.',
        'toasttab.', 'opentable.', 'order.online', 'facebook.', 'instagram.',
    )
    WEBSITE_TEXT_PATH = ROOT / 'tools' / 'cache' / 'website_text_cache.json'
    wt_cache = json.loads(WEBSITE_TEXT_PATH.read_text()) if WEBSITE_TEXT_PATH.exists() else {}
    wt_cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

    # Candidate URL precedence per 2026-05-15 directive:
    #   1) Google Places website  (preferred — most authoritative match)
    #   2) WEB VERIFY website     (top Google-search match found earlier
    #      via Haiku web_search using name+address+category)
    # Either way Haiku gets to JUDGE the actual page content before we keep
    # the URL. Aggregator-host short-circuit applies to both paths.
    fetch_jobs = []  # (key, url)
    for k in targets:
        p = pc.get(k) or {}
        u = ''
        if p.get('status') == 'ok' and p.get('website'):
            u = p['website'].strip()
        else:
            # Search-fallback: use the WV-surfaced URL when Places has none
            wvw = (wv.get(k) or {}).get('website') or ''
            if wvw and wvw.startswith(('http://', 'https://')):
                u = wvw.strip()
        if not u: continue
        host = u.lower().split('//', 1)[-1].split('/', 1)[0]
        if any(a in host for a in AGG_HOSTS):
            continue
        cached = wt_cache.get(u)
        # Skip only when we have USABLE cached text. Entries cached with
        # text=None (static fetch came up dry, no jina path existed yet)
        # should be retried so the new headless-render fallback gets a shot.
        if cached and cached.get('fetched_at', '') > wt_cutoff and cached.get('text'):
            continue
        fetch_jobs.append((k, u))

    if fetch_jobs:
        print(f"Pre-fetching website content for {len(fetch_jobs)} URLs (parallel)...")
        from concurrent.futures import ThreadPoolExecutor, as_completed
        now_fetch = datetime.now(timezone.utc).isoformat()
        def _fetch(job):
            k, u = job
            try:
                text, final_url = fetch_page_text(u)
            except Exception as e:
                return k, u, None, None, str(e)[:120]
            return k, u, text, final_url, None
        n_done = n_ok = 0
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(_fetch, j) for j in fetch_jobs]
            for fut in as_completed(futs):
                k, u, text, final_url, err = fut.result()
                socials = extract_socials(text) if text else {}
                wt_cache[u] = {
                    'fetched_at': now_fetch,
                    'text': text,
                    'final_url': final_url,
                    'error': err,
                    'socials': socials,
                }
                n_done += 1
                if text: n_ok += 1
                if n_done % 25 == 0:
                    print(f"  fetched {n_done}/{len(fetch_jobs)}  (ok: {n_ok})")
        WEBSITE_TEXT_PATH.write_text(json.dumps(wt_cache, indent=2, ensure_ascii=False))
        print(f"  done: {n_done} fetched, {n_ok} returned usable text")

    # Build a per-target website_text lookup from the cache. Same precedence
    # as the fetch loop: Places website first, WV website as fallback.
    website_texts = {}
    for k in targets:
        p = pc.get(k) or {}
        u = ''
        if p.get('status') == 'ok' and p.get('website'):
            u = p['website'].strip()
        else:
            wvw = (wv.get(k) or {}).get('website') or ''
            if wvw and wvw.startswith(('http://', 'https://')):
                u = wvw.strip()
        if not u: continue
        entry = wt_cache.get(u)
        if entry and entry.get('text'):
            website_texts[k] = entry['text']

    id_to_key = {}
    full_requests = []
    for i, k in enumerate(targets):
        cid = f"v{i:04d}"
        id_to_key[cid] = k
        rec = build_request(k, wv[k], pc.get(k), llm.get(k), csv_index.get(k),
                            website_text=website_texts.get(k))
        rec['custom_id'] = cid
        full_requests.append(rec)

    full_id = submit_batch(full_requests, 'VALIDATE')
    info = poll(full_id, 'VALIDATE')
    results = download_results(info)

    # Persist the raw batch results to disk so Haiku's reasoning is auditable
    # after the apply loop processes them. One file per batch, JSONL: each
    # line is the {custom_id, result:{type, message:{...}}} record from the
    # Batch API. Reconstruct an entry's verdict any time via:
    #   grep '"v0042"' tools/cache/validator_runs/<batch_id>.jsonl
    runs_dir = ROOT / 'tools' / 'cache' / 'validator_runs'
    runs_dir.mkdir(parents=True, exist_ok=True)
    runs_path = runs_dir / f"{full_id}.jsonl"
    with runs_path.open('w', encoding='utf-8') as rf:
        for r in results:
            rf.write(json.dumps(r, ensure_ascii=False) + '\n')
    # Sidecar: id_to_key index so we can resolve custom_id → entry key later.
    (runs_dir / f"{full_id}.index.json").write_text(
        json.dumps(id_to_key, indent=2, ensure_ascii=False))
    print(f"  raw results saved to {runs_path}")

    n_total = n_parse_fail = 0
    n_same_no = n_isr_no = n_isr_unclear = 0
    n_cuisine_changed = n_website_dropped = n_website_changed = 0
    examples = {'same_no': [], 'isr_no': [], 'cuisine_changed': [], 'website_dropped': [], 'website_changed': []}
    now_iso = datetime.now(timezone.utc).isoformat()

    for obj in results:
        cid = obj.get('custom_id')
        key = id_to_key.get(cid)
        if not key: continue
        res = obj.get('result', {})
        if res.get('type') != 'succeeded':
            n_parse_fail += 1
            continue
        parsed = parse_result(res['message'])
        if parsed is None:
            n_parse_fail += 1
            continue
        n_total += 1
        name = key.split('||')[0]

        # 1. Bad Places match → clear it; will be refetched on next cron
        if parsed['is_same_business'] == 'no':
            n_same_no += 1
            if len(examples['same_no']) < 6:
                examples['same_no'].append(f"{name[:35]:<35}  → {(pc.get(key) or {}).get('matchedName','?')}")
            if key in pc:
                pc.pop(key)

        # 2. Not a restaurant → mark for drop at inject time
        if parsed['is_restaurant'] == 'no':
            n_isr_no += 1
            if len(examples['isr_no']) < 6:
                examples['isr_no'].append(f"{name[:35]:<35}  | {parsed['evidence'][:60]}")
            wv[key]['validator_drop'] = 'not-restaurant'
            wv[key]['validator_evidence'] = parsed['evidence']
            # Aged-out unverifiable: schedule a monthly recheck instead of
            # the default daily re-validation, since this verdict won't
            # change until the licence holder gains a Places presence or
            # publishes a website. The validator prompt asks Haiku to lead
            # the evidence sentence with "AGED-OUT UNVERIFIABLE:" when the
            # 30d-old + no-Places + no-content rule fires.
            if parsed['evidence'].lstrip().upper().startswith('AGED-OUT UNVERIFIABLE'):
                wv[key]['validator_recheck_after'] = (
                    datetime.now(timezone.utc) + timedelta(days=30)
                ).isoformat()
            else:
                wv[key].pop('validator_recheck_after', None)
        elif parsed['is_restaurant'] == 'unclear':
            n_isr_unclear += 1
        else:
            wv[key].pop('validator_drop', None)
            wv[key].pop('validator_recheck_after', None)

        # 3. Cuisine update — only if it's a real change AND we got real cuisines
        real_cuisines = [c for c in parsed['cuisines'] if c and c != 'unknown']
        current = wv[key].get('cuisines') or ([wv[key].get('cuisine')] if wv[key].get('cuisine') else [])
        current = [c for c in current if c and c != 'unknown']
        if real_cuisines and set(real_cuisines) != set(current):
            n_cuisine_changed += 1
            if len(examples['cuisine_changed']) < 8:
                examples['cuisine_changed'].append(f"{name[:35]:<35}  {current} → {real_cuisines}")
            wv[key]['cuisine'] = real_cuisines[0]
            wv[key]['cuisines'] = real_cuisines
            wv[key]['evidence'] = parsed['evidence']
            wv[key]['recovery_source'] = 'unified_validator'

        # 4. Website handling. The validator is the authoritative source for
        # URL trust — when it approves a URL we MUST clear any stale broken
        # flag in url_health (e.g., from a prior run with a stricter prompt
        # that rejected this URL). When it rejects, mark all candidates
        # broken so the inject pipeline skips them.
        cur_website = wv[key].get('website') or ''
        places_website = (pc.get(key) or {}).get('website') if pc.get(key) and pc[key].get('status') == 'ok' else None
        if parsed['best_website'] is None:
            for u in (cur_website, places_website):
                if u and u.startswith(('http://', 'https://')):
                    health[u] = {'status': None, 'checked_at': now_iso,
                                 'ok': False, 'reason': 'validator: aggregator wrapper or dead'}
            if cur_website:
                n_website_dropped += 1
                if len(examples['website_dropped']) < 6:
                    examples['website_dropped'].append(f"{name[:35]:<35}  {cur_website[:50]}")
        elif parsed['best_website']:
            # Approved — clear any stale broken flag from a prior run.
            health[parsed['best_website']] = {
                'status': 200, 'checked_at': now_iso, 'ok': True,
                'reason': 'validator: approved'
            }
            if parsed['best_website'] != cur_website:
                n_website_changed += 1
                if len(examples['website_changed']) < 6:
                    examples['website_changed'].append(f"{name[:35]:<35}  → {parsed['best_website'][:60]}")
                wv[key]['website'] = parsed['best_website']

        # 5. ALWAYS persist Haiku's full judgment + one-sentence evidence so
        # any entry can be audited later (not just changed/dropped ones).
        # The raw batch result is also saved to validator_runs/<batch_id>.jsonl
        # for full request+response auditability.
        wv[key]['validator_judgment'] = {
            'is_same_business': parsed['is_same_business'],
            'is_restaurant':    parsed['is_restaurant'],
            'cuisines':         parsed['cuisines'],
            'best_website':     parsed['best_website'],
            'evidence':         parsed['evidence'],
        }
        wv[key]['validator_evidence'] = parsed['evidence']
        wv[key]['validated_at'] = now_iso

    WEB_VERIFY_PATH.write_text(json.dumps(wv, separators=(',', ':')))
    PLACES_PATH.write_text(json.dumps(pc, separators=(',', ':')))
    URL_HEALTH_PATH.write_text(json.dumps(health, separators=(',', ':')))

    print(f"\n=== Validation results ({n_total} entries) ===")
    print(f"  parse_fail / errors:           {n_parse_fail}")
    print(f"  is_same_business = no:         {n_same_no}  (Places match cleared, will refetch)")
    print(f"  is_restaurant = no:            {n_isr_no}   (tagged for drop at inject)")
    print(f"  is_restaurant = unclear:       {n_isr_unclear}")
    print(f"  cuisine changed:               {n_cuisine_changed}")
    print(f"  website dropped (aggregator):  {n_website_dropped}")
    print(f"  website changed (better URL):  {n_website_changed}")
    print()
    for cat, items in examples.items():
        if not items: continue
        print(f"--- {cat} samples ---")
        for x in items: print(f"  {x}")
        print()

if __name__ == '__main__':
    main()
