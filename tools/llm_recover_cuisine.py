#!/usr/bin/env python3
"""
Recovery pass for entries the verifier couldn't classify.

When `llm_verify_batch.py` runs web_search but the search results only surface
generic operating signals (delivery-platform listings, Instagram, "we're open"
press), Haiku often returns no cuisine. Falling back to the name-only LLM is
how we got "Tumi Dumpling House → tibetan" (the name happens to read Tibetan,
but the actual menu is Chinese xiao long bao).

This script does the obvious extra step: for each such entry, fetch the
restaurant's website homepage, strip HTML, and feed the actual page text into
Haiku alongside the name+address. Menu words ("xiao long bao", "Korean BBQ",
"pho", "injera") are highly diagnostic — much stronger than search snippets.

Idempotent: only revisits entries with status=ok, operating=yes, cuisine
null/unknown, and website set. Cost ~$0.001 per recovery call on Haiku sync.
"""
import os, re, sys, json, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
WEB_VERIFY_PATH = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'
PLACES_CACHE_PATH = ROOT / 'tools' / 'cache' / 'places_cache.json'
SECRETS = Path('/var/secrets/nowservingto.env')
MODEL = 'claude-haiku-4-5-20251001'
UA = 'Mozilla/5.0 (compatible; nowservingto-cuisine/1.0)'
SNIFF_BYTES = 32768
WORKERS = 6
SOCIAL_DOMAINS = ('instagram.com', 'facebook.com', 'tiktok.com')

# Import the existing HTML stripper to reuse the proven SPA-shell handling
sys.path.insert(0, str(ROOT / 'tools'))
from check_link_health import _strip_html

# Cuisine taxonomy is the canonical one from cuisines.py — keeps this script
# in lockstep with inject_openings' display labels so we can't silently tag
# entries with a cuisine that has no label.
from cuisines import VALID_CUISINE_KEYS, parse_cuisines_from_llm

SYSTEM_PROMPT = """You classify a Toronto restaurant by cuisine using the evidence provided.
The evidence may include any subset of:
  • WEBSITE CONTENT — the restaurant's own homepage and/or menu page (HTML stripped)
  • GOOGLE PLACES EDITORIAL SUMMARY — Google's curated one-line description of the place
  • RECENT GOOGLE REVIEWS — up to 5 customer reviews

Use ALL provided sections to decide. Reviews often carry the strongest cultural-marker
signals: "their kunafa is amazing", "best biryani in Scarborough", "the pupusas are
authentic Salvadoran" — these dish-and-country mentions in reviews disambiguate cases
where the website is generic or a JS shell. The editorial summary is Google's own
classification ("Lebanese restaurant serving...") and is usually accurate.

Return a single JSON object on ONE line, no prose:
{"cuisines":["<key1>","<key2>"],"evidence":"<one short sentence quoting the strongest clue>"}

`cuisines` is a LIST of 1-3 specific cuisine keys. List multiple when the place
explicitly serves cuisines from different countries (not blended fusion):
- "Authentic Afghan, Pakistani & Indian Flavors" → ["afghan","pakistani","indian"]
- "Lebanese & Syrian kitchen" → ["lebanese","syrian"]
- Single cuisine: ["italian"]
- Can't classify: ["unknown"]
PREFER specific country buckets. Use the umbrella (["south_asian"], ["middle_east"],
["caribbean"], ["latin"]) only when no specific country is stated in the evidence.

Valid cuisine keys: italian, chinese, japanese, korean, vietnamese, filipino, thai,
indonesian, malaysian, burmese, cambodian, laotian, south_asian, indian, pakistani,
afghan, bangladeshi, tamil, tibetan, sri_lankan, nepalese, caribbean, jamaican,
trinidadian, guyanese, haitian, cuban, dominican, greek, portuguese, polish, french,
irish_uk, german, jewish_deli, spanish, eastern_eu, ukrainian, russian, hungarian,
middle_east, lebanese, turkish, syrian, persian, israeli, egyptian, yemeni, armenian,
georgian, latin, mexican, salvadoran, peruvian, colombian, brazilian, argentinian,
venezuelan, african_horn, ethiopian, eritrean, somali, african_west, nigerian, ghanaian,
moroccan, senegalese, unknown.

CRITICAL: Pan-Asian / fusion (3+ regional cuisines as equal billing) → unknown.
CRITICAL: Packaged-food brand / grocery-counter / factory outlet → unknown.
CRITICAL: American Southern (Cajun, Creole, New Orleans, Memphis BBQ, soul) → unknown.
CRITICAL: If the page is a redirect target, a parked-domain placeholder, or content
unrelated to the restaurant name (different business, no menu, only CSS/JS) → unknown.

The cuisine pick must be supported by content from the page (menu words count most;
About-Us cuisine claims are second; address + name alone never justify a non-unknown
choice in this recovery pass)."""

def load_api_key():
    if not SECRETS.exists(): sys.exit(f"{SECRETS} missing")
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if line.startswith('ANTHROPIC_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    sys.exit("ANTHROPIC_API_KEY not in secrets")

API_KEY = load_api_key()

MENU_HINTS = ('menu', 'food', 'dishes', 'lunch', 'dinner', 'order', 'about')

def _fetch_raw(url, byte_cap=SNIFF_BYTES):
    """Fetch first chunk of URL, follow redirects. Returns (raw_bytes, final_url) or (None, url)."""
    try:
        req = Request(url, headers={'User-Agent': UA, 'Range': f'bytes=0-{byte_cap}'}, method='GET')
        with urlopen(req, timeout=10) as r:
            return r.read(byte_cap), r.geturl()
    except Exception:
        return None, url

def _find_menu_link(html_text, base_url):
    """Scan HTML for an internal anchor pointing to a menu/about page. Returns
    absolute URL or None."""
    try:
        s = html_text.decode('utf-8', errors='replace') if isinstance(html_text, bytes) else html_text
    except Exception:
        return None
    from urllib.parse import urljoin, urlparse
    base_host = urlparse(base_url).netloc
    # Score anchors; prefer "menu" > "food" > "about"
    candidates = []
    for m in re.finditer(r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>([^<]*)</a>', s, re.IGNORECASE):
        href, inner = m.group(1), m.group(2).lower()
        full = urljoin(base_url, href)
        if urlparse(full).netloc not in (base_host, ''): continue  # off-site
        haystack = (href + ' ' + inner).lower()
        for i, hint in enumerate(MENU_HINTS):
            if hint in haystack:
                candidates.append((i, full))
                break
    if not candidates: return None
    candidates.sort()
    return candidates[0][1]

_SOCIAL_NON_HANDLES = {
    'instagram': {'p','explore','reel','reels','tags','stories','tv','about','directory','developer','accounts','sharer','share'},
    'twitter':   {'home','share','intent','i','settings','login','signup','search','hashtag','share-ui','status'},
    'facebook':  {'sharer','plugins','tr','help','login','business','reg','people','pages','dialog','share','recommend'},
}

def extract_socials(text):
    """Pull Instagram / X (Twitter) / Facebook handles out of fetched
    page text. Returns dict like {'instagram': 'hopperslk_to', 'x': 'hopperslk'}.
    Keys only present when a real-looking handle is found. Filters out the
    common non-handle paths like instagram.com/p/..., twitter.com/intent/...,
    facebook.com/sharer.php that show up in share-button links."""
    if not text: return {}
    import re as _re
    out = {}
    for m in _re.finditer(r'instagram\.com/([A-Za-z0-9_.]{2,30})(?:[/?#]|$)', text):
        h = m.group(1).strip('.')
        if h.lower() not in _SOCIAL_NON_HANDLES['instagram']:
            out['instagram'] = h
            break
    for m in _re.finditer(r'(?:x|twitter)\.com/([A-Za-z0-9_]{2,15})(?:[/?#]|$)', text):
        h = m.group(1)
        if h.lower() not in _SOCIAL_NON_HANDLES['twitter']:
            out['x'] = h
            break
    for m in _re.finditer(r'facebook\.com/([A-Za-z0-9.\-_]{2,50})(?:[/?#]|$)', text):
        h = m.group(1).strip('.')
        if h.lower() not in _SOCIAL_NON_HANDLES['facebook']:
            out['facebook'] = h
            break
    return out


import threading as _threading
# Jina free tier caps at 2 concurrent connections per IP. With the keyed
# trial allowance (10M tokens) we still respect this — both for politeness
# and because exceeding it just yields 429s.
_JINA_SEM = _threading.Semaphore(2)

def _load_jina_key():
    """Pull JINA_API_KEY from /var/secrets/nowservingto.env if present.
    Without a key we fall back to the keyless public endpoint (rate-limited
    per IP)."""
    try:
        for line in Path('/var/secrets/nowservingto.env').read_text().splitlines():
            if line.startswith('JINA_API_KEY='):
                return line.split('=', 1)[1].strip()
    except Exception:
        pass
    return None

_JINA_KEY = _load_jina_key()


def _fetch_jina(url):
    """Fetch a page through r.jina.ai — Jina's Reader service runs the URL in a
    headless browser, lets JS hydrate, and returns the rendered DOM as plain
    text. Used as a fallback for SPAs whose static HTML has no body text.

    Keyed mode (JINA_API_KEY set) draws from the account's token budget;
    keyless mode is subject to the public per-IP daily limit. Either way we
    cap concurrency at 2 to stay inside Jina's connection-count policy.
    """
    with _JINA_SEM:
        try:
            headers = {
                'User-Agent': UA,
                'Accept': 'text/plain',
                'X-Return-Format': 'text',
                'X-Timeout': '20',
            }
            if _JINA_KEY:
                headers['Authorization'] = f'Bearer {_JINA_KEY}'
            req = Request(f"https://r.jina.ai/{url}", headers=headers)
            with urlopen(req, timeout=30) as r:
                txt = r.read(16000).decode('utf-8', errors='replace')
        except Exception:
            return None
    txt = (txt or '').strip()
    try:
        from usage_log import log_usage
        # Estimate tokens from rendered char count (4 chars/token rough avg).
        log_usage('jina.reader', units=max(1, len(txt) // 4),
                  meta={'url': url[:120]})
    except Exception: pass
    return txt if len(txt) >= 80 else None


def fetch_page_text(url):
    """Fetch homepage text. If the homepage is thin or generic, also try to follow
    a menu/about link and combine its text. Returns up to ~3200 chars of stripped
    text and the final URL (after redirects).

    Two-stage fetch:
      1. Static GET → strip HTML. Works for ~70% of restaurant sites that render
         body content server-side.
      2. r.jina.ai fallback → only when the URL is alive (raw HTML downloaded)
         but the stripped text is < 80 chars (typical JS-only SPA shell). Jina
         renders headless, gives us the post-hydration text. Used to catch
         multi-location chain signals on React/Vue/Webflow sites whose
         "Locations" page or footer only exists after hydration.
    """
    raw, final_url = _fetch_raw(url)
    if not raw: return None, url   # dead URL — don't waste jina budget
    home_text = _strip_html(raw)
    home_body = home_text.split('TEXT:', 1)[1].strip() if home_text and 'TEXT:' in home_text else ''

    # Try to follow a menu link from the homepage HTML
    menu_url = _find_menu_link(raw, final_url)
    menu_text = ''
    if menu_url and menu_url != final_url:
        menu_raw, _ = _fetch_raw(menu_url)
        if menu_raw:
            mt = _strip_html(menu_raw)
            menu_text = (mt.split('TEXT:', 1)[1].strip() if mt and 'TEXT:' in mt else '')[:1500]

    static_total = len(home_body) + len(menu_text)
    if static_total >= 80:
        combined = f"HOMEPAGE: {home_body[:1500]}"
        if menu_text:
            combined += f"\n\nMENU/ABOUT PAGE ({menu_url}): {menu_text}"
        return combined[:3200], final_url

    # Static fetch came up empty but the URL is alive — likely a JS-only SPA.
    # Fall through to jina's headless-rendered text.
    rendered = _fetch_jina(url)
    if rendered:
        return f"HOMEPAGE (jina-rendered): {rendered[:3200]}", final_url
    return None, final_url

def _build_evidence_payload(page_text, places_reviews, places_editorial):
    """Combine whatever evidence we have into a single user-message body.
    Any source can be None/empty; at least one must be non-empty for this
    to return non-None."""
    sections = []
    if page_text:
        sections.append(f"WEBSITE CONTENT:\n{page_text}")
    if places_editorial:
        sections.append(f"GOOGLE PLACES EDITORIAL SUMMARY:\n{places_editorial}")
    if places_reviews:
        review_lines = '\n'.join(f"- {r[:400]}" for r in places_reviews[:5] if r)
        if review_lines:
            sections.append(f"RECENT GOOGLE REVIEWS:\n{review_lines}")
    return '\n\n'.join(sections) if sections else None

def classify_one(name, address, page_text=None, places_reviews=None, places_editorial=None):
    body = _build_evidence_payload(page_text, places_reviews, places_editorial)
    if not body:
        return None, None
    payload = json.dumps({
        'model': MODEL,
        'max_tokens': 120,
        'system': SYSTEM_PROMPT,
        'messages': [{
            'role': 'user',
            'content': f"Restaurant: {name}\nAddress: {address}\n\n{body}",
        }],
    }).encode('utf-8')
    req = Request('https://api.anthropic.com/v1/messages', data=payload, headers={
        'x-api-key': API_KEY,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
    }, method='POST')
    with urlopen(req, timeout=30) as r:
        msg = json.loads(r.read())
    blocks = [b.get('text', '') for b in msg.get('content', []) if b.get('type') == 'text']
    text = (blocks[-1] if blocks else '').strip()
    for line in text.split('\n'):
        s = line.strip().lstrip('`').strip()
        if s.startswith('{') and s.endswith('}'):
            try:
                d = json.loads(s)
                cuisines = parse_cuisines_from_llm(d)
                evidence = (d.get('evidence') or '')[:200]
                if cuisines:
                    return cuisines, evidence
            except Exception:
                continue
    return None, None

def _is_social(url):
    return bool(url) and any(d in url.lower() for d in SOCIAL_DOMAINS)

def _is_maps_url(url):
    if not url: return False
    l = url.lower()
    return 'maps.google.' in l or 'goo.gl/maps' in l

def best_website(verify_entry, places_entry):
    """Pick the most fetchable website for cuisine recovery, in priority order:
       1. Places' own-website (non-social, non-maps) — Google's authoritative pick
       2. verify_cache's website if non-social
       3. anything social — last resort; immigrant-run spots may live entirely on IG
       Returns (url, source) or (None, None)."""
    p_web = (places_entry or {}).get('website') if places_entry and places_entry.get('status') == 'ok' else None
    v_web = verify_entry.get('website')
    if p_web and not _is_social(p_web) and not _is_maps_url(p_web):
        return p_web, 'places'
    if v_web and not _is_social(v_web) and not _is_maps_url(v_web):
        return v_web, 'verify'
    # Fall back to social — Instagram bios often carry "Authentic Sichuan",
    # "Halal Turkish bakery", etc. in the first line of accessible HTML.
    if p_web and _is_social(p_web): return p_web, 'places-social'
    if v_web and _is_social(v_web): return v_web, 'verify-social'
    return None, None

def _has_places_extras(places_entry):
    if not places_entry or places_entry.get('status') != 'ok': return False
    if places_entry.get('reviews'): return True
    if places_entry.get('editorialSummary'): return True
    return False

def needs_recovery(entry, places_entry=None):
    if entry.get('status') != 'ok': return False
    if entry.get('operating') != 'yes': return False
    if entry.get('cuisine') and entry.get('cuisine') != 'unknown': return False
    # We can recover if EITHER (a) there's a fetchable website OR (b) Places
    # has rich extras (reviews / editorial summary). Adding the Places-extras
    # path unblocks the entries whose website is a JS shell but whose Google
    # Maps profile has substantive review content.
    url, _ = best_website(entry, places_entry)
    if not url and not _has_places_extras(places_entry): return False
    # Skip entries we already tried in the last 30 days — they'll be re-attempted
    # naturally as the website-content situation evolves (e.g. brand-new sites
    # may have a fuller menu page after a month).
    ra = entry.get('recovered_at')
    if ra:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ra)).days
            if age < 30: return False
        except Exception:
            pass
    return True

def main():
    cache = json.loads(WEB_VERIFY_PATH.read_text())
    places = json.loads(PLACES_CACHE_PATH.read_text()) if PLACES_CACHE_PATH.exists() else {}
    targets = [(k, e) for k, e in cache.items() if needs_recovery(e, places.get(k))]
    # Tally what URL source each target will use, so we know what we're sending out
    by_source = {}
    for k, e in targets:
        _, src = best_website(e, places.get(k))
        by_source[src] = by_source.get(src, 0) + 1
    print(f"verify cache entries:        {len(cache)}")
    print(f"needing cuisine recovery:    {len(targets)}")
    print(f"  fetch sources: {by_source}")
    if not targets:
        return

    def work(key, e):
        try:
            p = places.get(key) or {}
            p_reviews = p.get('reviews') if p.get('status') == 'ok' else None
            p_editorial = p.get('editorialSummary') if p.get('status') == 'ok' else None

            url, src = best_website(e, p if p.get('status') == 'ok' else None)
            text = None
            if url:
                text, _ = fetch_page_text(url)

            # If website yielded no text AND Places has nothing either, give up.
            if not text and not p_reviews and not p_editorial:
                if url:
                    return key, None, None, f'no usable page content (source={src})', src
                return key, None, None, 'no website + no Places extras', None

            name = key.split('||')[0]
            address = key.split('||')[1] if '||' in key else ''
            cuisines, evidence = classify_one(name, address, text, p_reviews, p_editorial)
            # If we ended up classifying from Places-extras only, mark source accordingly
            effective_src = src if text else 'places_extras'
            return key, cuisines, evidence, None, effective_src
        except Exception as ex:
            return key, None, None, f"{type(ex).__name__}: {str(ex)[:60]}", None

    now_iso = datetime.now(timezone.utc).isoformat()
    n_recovered = n_unknown = n_failed = 0
    recovered_by_source = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(work, k, v) for k, v in targets]
        for i, fut in enumerate(as_completed(futures), 1):
            key, cuisines, evidence, err, src = fut.result()
            # Always stamp recovered_at so we don't retry the same failure every
            # day — gives the natural 30-day re-attempt cycle via needs_recovery.
            cache[key]['recovered_at'] = now_iso
            if err:
                n_failed += 1
                cache[key]['recovery_note'] = err[:120]
            elif not cuisines or cuisines == ['unknown']:
                n_unknown += 1
                if cuisines == ['unknown']:
                    cache[key]['cuisine'] = 'unknown'
                    cache[key]['cuisines'] = ['unknown']
                    cache[key]['evidence'] = (evidence or 'page content recovery — still unknown')[:200]
            else:
                # Strip 'unknown' if mixed with real cuisines (defensive — shouldn't happen)
                real = [c for c in cuisines if c != 'unknown']
                if not real:
                    n_unknown += 1
                    continue
                n_recovered += 1
                recovered_by_source[src] = recovered_by_source.get(src, 0) + 1
                cache[key]['cuisine'] = real[0]          # primary — backwards compat
                cache[key]['cuisines'] = real             # full list — new multi-cuisine
                cache[key]['evidence'] = (evidence or '')[:200]
                cache[key]['recovery_source'] = src
            if i % 10 == 0 or i == len(targets):
                el = time.time() - t0
                print(f"  [{i}/{len(targets)}] {el:.0f}s  recovered={n_recovered}  unknown={n_unknown}  failed={n_failed}")
                WEB_VERIFY_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    WEB_VERIFY_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    print(f"\nDone in {time.time()-t0:.0f}s: recovered={n_recovered}  unknown={n_unknown}  failed={n_failed}")
    if recovered_by_source:
        print(f"recovered by source: {recovered_by_source}")

if __name__ == '__main__':
    main()
