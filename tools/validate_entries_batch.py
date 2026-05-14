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

You see the City's licence data (operating name + address), the Google Places match
we made (matched name + address + categories + editorial summary + top reviews +
Places-known website), the earlier Haiku web_search verifier's results (website +
evidence), and the name-only LLM's previous cuisine guess.

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

is_same_business — is the Places match referring to the SAME business as the
licence? Compare semantically (not just string overlap):
  - "EASTERN 828 CAFE & GRILL" vs "Eastern-Leslie Car Wash & Express Detail" with
    types=[car_wash,…] → "no" (different businesses sharing only the street name)
  - "KALIMERA FOOD KITCHEN" vs "The Laurel School" with types=[school,…] → "no"
  - "OI BANH MI" vs "Ôi BÁNH MÌ" → "yes" (Unicode variants of same name)
  - "MARY BROWN'S CHICKEN" vs "Mary Brown's Fried Chicken" → "yes"
  - If Places returned NO match at all (place_id null / not_found) → "unclear"

is_restaurant — is this a consumer walk-in restaurant?
  - "yes" — standalone restaurant, cafe, bar, bakery, food truck, hot-counter
    that ordinary people walk into to eat. Even a small ghost kitchen counts
    as long as humans can order food.
  - "no" — institutional caterers (Aramark/Compass Group cafeterias inside
    hospitals/offices/universities), packaged-food brand / factory outlet
    (Shimla Foods, Soma Bone Broth, Bergamos), grocery store / supermarket
    counter that just sells packaged goods, chain (Popeyes/KFC/Mary Brown's/
    Tim Hortons/Pizza Pizza/etc.) — these all return "no".
  - "unclear" — genuinely ambiguous (e.g. coffee + branded retail like Lindt
    Chocolate, deli + grocery combo, retail bakery with mostly-packaged goods).

cuisines — list of 1-3 specific cuisine keys (or ["unknown"]):
  italian, chinese, japanese, korean, vietnamese, filipino, thai, indonesian,
  malaysian, burmese, cambodian, laotian, south_asian, indian, pakistani, afghan,
  bangladeshi, tamil, tibetan, sri_lankan, nepalese, caribbean, jamaican,
  trinidadian, guyanese, haitian, cuban, dominican, greek, portuguese, polish,
  french, irish_uk, german, jewish_deli, spanish, eastern_eu, ukrainian, russian,
  hungarian, middle_east, lebanese, turkish, syrian, persian, israeli, egyptian,
  yemeni, armenian, georgian, latin, mexican, salvadoran, peruvian, colombian,
  brazilian, argentinian, venezuelan, african_horn, ethiopian, eritrean, somali,
  african_west, nigerian, ghanaian, moroccan, senegalese, unknown.

  PREFER specific countries over umbrella ("dominican" not "caribbean";
  "afghan,pakistani,indian" multi-list not "south_asian"). Use umbrella only
  when evidence is genuinely multi-region with no specific country named.
  Pan-Asian / 3+ unrelated regional fusion → ["unknown"].

best_website — the URL we should put on the entry's name link:
  - PREFER the restaurant's own domain (sabordelpacificoon.com)
  - REJECT aggregator wrappers — any URL whose final destination is
    skipthedishes.com / doordash.com / ubereats.com / grubhub.com /
    foodora.ca / menulog.com / seamless.com / chownow.com order portal.
    If a cached URL appears to BE such a wrapper (judged from URL host or
    from review text mentioning "order via SkipTheDishes"), return null.
  - REJECT obvious dead pages, parked domains, generic CMS landing screens.
  - Return null if no good website exists — the entry will fall back to its
    Google Maps URL.

evidence — one short sentence quoting the strongest signal that justified the
above judgments (a review excerpt, an editorial line, a menu phrase)."""

def build_request(entry_key, verify_entry, places_entry, llm_entry=None):
    name, _, addr = entry_key.partition('||')
    lines = [f"LICENCE (City of Toronto):", f"  Operating Name: {name}", f"  Address: {addr}", ""]

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
    force = '--force' in sys.argv
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    if not force:
        target_keys = {k for k in target_keys
                       if not wv[k].get('validated_at') or wv[k]['validated_at'] < cutoff_iso}
    targets = sorted(target_keys)
    print(f"Entries to validate: {len(targets)}")
    print(f"  estimated cost (~$0.001 each): ${len(targets)*0.001:.2f}")
    if not targets: return

    id_to_key = {}
    full_requests = []
    for i, k in enumerate(targets):
        cid = f"v{i:04d}"
        id_to_key[cid] = k
        rec = build_request(k, wv[k], pc.get(k), llm.get(k))
        rec['custom_id'] = cid
        full_requests.append(rec)

    full_id = submit_batch(full_requests, 'VALIDATE')
    info = poll(full_id, 'VALIDATE')
    results = download_results(info)

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
        elif parsed['is_restaurant'] == 'unclear':
            n_isr_unclear += 1
        else:
            wv[key].pop('validator_drop', None)

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

        # 4. Website handling — if validator says null, mark known URLs broken
        cur_website = wv[key].get('website') or ''
        places_website = (pc.get(key) or {}).get('website') if pc.get(key) and pc[key].get('status') == 'ok' else None
        if parsed['best_website'] is None:
            # Mark all known candidate URLs broken so verification_for skips them
            for u in (cur_website, places_website):
                if u and u.startswith(('http://', 'https://')):
                    health[u] = {'status': None, 'checked_at': now_iso,
                                 'ok': False, 'reason': 'validator: aggregator wrapper or dead'}
            if cur_website:
                n_website_dropped += 1
                if len(examples['website_dropped']) < 6:
                    examples['website_dropped'].append(f"{name[:35]:<35}  {cur_website[:50]}")
        elif parsed['best_website'] and parsed['best_website'] != cur_website:
            n_website_changed += 1
            if len(examples['website_changed']) < 6:
                examples['website_changed'].append(f"{name[:35]:<35}  → {parsed['best_website'][:60]}")
            wv[key]['website'] = parsed['best_website']

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
