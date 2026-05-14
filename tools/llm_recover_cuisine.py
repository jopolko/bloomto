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
SECRETS = Path('/var/secrets/nowservingto.env')
MODEL = 'claude-haiku-4-5-20251001'
UA = 'Mozilla/5.0 (compatible; nowservingto-cuisine/1.0)'
SNIFF_BYTES = 32768
WORKERS = 6

# Import the existing HTML stripper to reuse the proven SPA-shell handling
sys.path.insert(0, str(ROOT / 'tools'))
from check_link_health import _strip_html

VALID_CUISINE_KEYS = {
    'italian','chinese','japanese','korean','vietnamese','filipino','thai','indonesian','malaysian','burmese',
    'cambodian','laotian',
    'south_asian','indian','pakistani','afghan','bangladeshi','tamil','tibetan','sri_lankan','nepalese',
    'caribbean','jamaican','trinidadian','guyanese','haitian','cuban','dominican',
    'greek','portuguese','polish','french','irish_uk','german','jewish_deli','spanish',
    'eastern_eu','ukrainian','russian','hungarian',
    'middle_east','lebanese','turkish','syrian','persian','israeli','egyptian','yemeni','armenian','georgian',
    'latin','mexican','salvadoran','peruvian','colombian','brazilian','argentinian','venezuelan',
    'african_horn','ethiopian','eritrean','somali',
    'african_west','nigerian','ghanaian','moroccan','senegalese','unknown',
}

SYSTEM_PROMPT = """You classify a Toronto restaurant by cuisine, using the actual content
of its website. The website may be the original, a redirected target, or a brand site for
a packaged-food line. Use menu words, "we serve…" copy, and About-Us hints to decide.

Return a single JSON object on ONE line, no prose:
{"cuisine":"<key>","evidence":"<one short sentence quoting the menu/about clue>"}

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

def fetch_page_text(url):
    """Fetch homepage text. If the homepage is thin or generic, also try to follow
    a menu/about link and combine its text. Returns up to ~2400 chars of stripped
    text and the final URL (after redirects)."""
    raw, final_url = _fetch_raw(url)
    if not raw: return None, url
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

    # Need at least some real text to send to Haiku
    if len(home_body) + len(menu_text) < 80: return None, final_url

    combined = f"HOMEPAGE: {home_body[:1500]}"
    if menu_text:
        combined += f"\n\nMENU/ABOUT PAGE ({menu_url}): {menu_text}"
    return combined[:3200], final_url

def classify_one(name, address, page_text):
    payload = json.dumps({
        'model': MODEL,
        'max_tokens': 120,
        'system': SYSTEM_PROMPT,
        'messages': [{
            'role': 'user',
            'content': f"Restaurant: {name}\nAddress: {address}\n\n{page_text}",
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
                cuisine = (d.get('cuisine') or '').strip().lower()
                evidence = (d.get('evidence') or '')[:200]
                if cuisine in VALID_CUISINE_KEYS:
                    return cuisine, evidence
            except Exception:
                continue
    return None, None

def needs_recovery(entry):
    if entry.get('status') != 'ok': return False
    if entry.get('operating') != 'yes': return False
    if not entry.get('website'): return False
    if entry.get('cuisine') and entry.get('cuisine') != 'unknown': return False
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
    targets = [(k, e) for k, e in cache.items() if needs_recovery(e)]
    print(f"verify cache entries:        {len(cache)}")
    print(f"needing cuisine recovery:    {len(targets)}")
    if not targets:
        return

    def work(key, e):
        try:
            text, final_url = fetch_page_text(e['website'])
            if not text:
                return key, None, None, 'no usable page content'
            name = key.split('||')[0]
            address = key.split('||')[1] if '||' in key else ''
            cuisine, evidence = classify_one(name, address, text)
            return key, cuisine, evidence, None
        except Exception as ex:
            return key, None, None, f"{type(ex).__name__}: {str(ex)[:60]}"

    now_iso = datetime.now(timezone.utc).isoformat()
    n_recovered = n_unknown = n_failed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(work, k, v) for k, v in targets]
        for i, fut in enumerate(as_completed(futures), 1):
            key, cuisine, evidence, err = fut.result()
            # Always stamp recovered_at so we don't retry the same failure every
            # day — gives the natural 30-day re-attempt cycle via needs_recovery.
            cache[key]['recovered_at'] = now_iso
            if err:
                n_failed += 1
                cache[key]['recovery_note'] = err[:120]
            elif cuisine == 'unknown' or cuisine is None:
                n_unknown += 1
                if cuisine == 'unknown':
                    cache[key]['cuisine'] = 'unknown'
                    cache[key]['evidence'] = (evidence or 'page content recovery — still unknown')[:200]
            else:
                n_recovered += 1
                cache[key]['cuisine'] = cuisine
                cache[key]['evidence'] = (evidence or '')[:200]
            if i % 10 == 0 or i == len(targets):
                el = time.time() - t0
                print(f"  [{i}/{len(targets)}] {el:.0f}s  recovered={n_recovered}  unknown={n_unknown}  failed={n_failed}")
                WEB_VERIFY_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    WEB_VERIFY_PATH.write_text(json.dumps(cache, separators=(',', ':')))
    print(f"\nDone in {time.time()-t0:.0f}s: recovered={n_recovered}  unknown={n_unknown}  failed={n_failed}")

if __name__ == '__main__':
    main()
