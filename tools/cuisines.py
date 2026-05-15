"""
Canonical cuisine taxonomy — single source of truth for the entire pipeline.

Why this exists:
  Before this module, every recovery script defined its own VALID_CUISINE_KEYS
  set, and inject_openings.py had its own CUISINE_LABEL dict. They drifted.
  Recovery scripts would tag entries with cuisines like `armenian` that the
  inject step didn't have a label for, so those entries silently disappeared
  from the feed. (Lost 15 cuisines this way on 2026-05-14 — armenian,
  argentinian, spanish, egyptian, yemeni, georgian, cuban, dominican,
  venezuelan, sri_lankan, nepalese, senegalese, israeli, cambodian, laotian.)

Now CUISINE_LABEL is defined ONCE here. Adding a new cuisine bucket means:
  1. Add a line to CUISINE_LABEL below.
  2. Everything else — recovery scripts, the inject step, the front-end
     dropdown — picks it up automatically on next cron.
"""

# key -> human display label (used in the cuisine dropdown, /cuisine/<key>
# routes, JSON-LD output, and as the canonical set of allowed cuisine keys
# across the entire pipeline). Ordering here is the rough display order;
# keep regional groupings together for readability.
CUISINE_LABEL = {
    # East / Southeast Asia
    'italian':'Italian','chinese':'Chinese','japanese':'Japanese','korean':'Korean',
    'vietnamese':'Vietnamese','filipino':'Filipino','thai':'Thai',
    'indonesian':'Indonesian','malaysian':'Malaysian','burmese':'Burmese',
    'cambodian':'Cambodian','laotian':'Laotian',
    # South Asia
    'south_asian':'South Asian','indian':'Indian','pakistani':'Pakistani','afghan':'Afghan',
    'bangladeshi':'Bangladeshi','tamil':'Tamil','tibetan':'Tibetan',
    'sri_lankan':'Sri Lankan','nepalese':'Nepalese',
    # Caribbean
    'caribbean':'Caribbean','jamaican':'Jamaican','trinidadian':'Trinidadian',
    'guyanese':'Guyanese','haitian':'Haitian','cuban':'Cuban','dominican':'Dominican',
    # Europe
    'greek':'Greek','portuguese':'Portuguese','polish':'Polish','french':'French','spanish':'Spanish',
    'irish_uk':'Irish/UK','german':'German','jewish_deli':'Jewish deli',
    'eastern_eu':'Eastern European','ukrainian':'Ukrainian','russian':'Russian',
    'hungarian':'Hungarian','georgian':'Georgian',
    # Middle East
    'middle_east':'Middle Eastern','lebanese':'Lebanese','turkish':'Turkish','syrian':'Syrian',
    'persian':'Persian','armenian':'Armenian','egyptian':'Egyptian','yemeni':'Yemeni','israeli':'Israeli',
    # Latin America
    'latin':'Latin American','mexican':'Mexican','salvadoran':'Salvadoran','peruvian':'Peruvian',
    'colombian':'Colombian','brazilian':'Brazilian','argentinian':'Argentinian','venezuelan':'Venezuelan',
    # Africa
    'african_horn':'East African','ethiopian':'Ethiopian','eritrean':'Eritrean','somali':'Somali',
    'african_west':'West African','nigerian':'Nigerian','ghanaian':'Ghanaian',
    'moroccan':'Moroccan','senegalese':'Senegalese',
}

# ---------------------------------------------------------------------------
# DYNAMIC TAXONOMY (added 2026-05-15)
# ---------------------------------------------------------------------------
# Haiku is now free to return any country/diaspora cuisine label it likes.
# When the validator reports a label we don't already know — e.g. "Cape
# Verdean", "Hakka", "Uyghur" — we slugify it, generate a deterministic
# color, and persist the new key to cuisines_dynamic.json. Next inject
# picks it up and the frontend renders it like any built-in cuisine.
#
# This file ships a hand-curated SEED set (the CUISINE_LABEL dict above)
# with bespoke labels and CSS-var palette assignments in index.html. New
# cuisines surfaced at runtime live in the dynamic dict and get a hash-
# derived hex color that ships through corridors.json so the frontend
# can render them without any hardcoded palette entry.

import hashlib as _hashlib
import json as _json
import re as _re
from pathlib import Path as _Path

_DYNAMIC_PATH = _Path(__file__).resolve().parent / 'cache' / 'cuisines_dynamic.json'

def _load_dynamic():
    """Read the persisted dynamic cuisine dict. Shape: {key: label}."""
    try:
        return _json.loads(_DYNAMIC_PATH.read_text())
    except Exception:
        return {}

def _save_dynamic(d):
    try:
        _DYNAMIC_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DYNAMIC_PATH.write_text(_json.dumps(d, indent=2, ensure_ascii=False, sort_keys=True))
    except Exception:
        pass

# Merge dynamic additions into the in-memory CUISINE_LABEL at import time.
# Seed keys win on conflict (we curate those by hand).
for _k, _v in _load_dynamic().items():
    if _k not in CUISINE_LABEL:
        CUISINE_LABEL[_k] = _v

# Used by recovery scripts (llm_verify_batch, llm_classify_batch,
# llm_recover_cuisine, llm_search_recover_cuisine) to gate Haiku output.
# Includes 'unknown' as a valid response for "can't determine" cases.
VALID_CUISINE_KEYS = set(CUISINE_LABEL.keys()) | {'unknown'}


def _slugify_cuisine(label):
    """Free-form label → canonical key. "Sri Lankan" → "sri_lankan",
    "Cape Verdean" → "cape_verdean", "Hakka-Chinese" → "hakka_chinese"."""
    s = (label or '').strip().lower()
    if not s: return ''
    # Strip common umbrella suffixes that aren't part of the diaspora name
    s = _re.sub(r'\s+(cuisine|food|restaurant|kitchen)$', '', s)
    # Replace non-alnum with underscore, collapse repeats
    s = _re.sub(r'[^a-z0-9]+', '_', s).strip('_')
    return s


def cuisine_color(key):
    """Deterministic hex color for a cuisine key. Used for cuisines NOT
    in the curated index.html palette — gives them a stable, distinct
    chip color without manual assignment. HSL hue derived from a hash,
    fixed saturation + lightness for visual cohesion with the existing
    palette (saturated, mid-dark)."""
    if not key: return '#888888'
    h = int(_hashlib.md5(key.encode('utf-8')).hexdigest()[:8], 16)
    hue = h % 360
    # HSL → RGB at S=58%, L=42% (same neighborhood as our curated palette).
    s, l = 0.58, 0.42
    c = (1 - abs(2*l - 1)) * s
    x = c * (1 - abs(((hue/60) % 2) - 1))
    m = l - c/2
    r, g, b = ((c,x,0) if hue<60 else (x,c,0) if hue<120 else (0,c,x) if hue<180
               else (0,x,c) if hue<240 else (x,0,c) if hue<300 else (c,0,x))
    R = int((r+m)*255); G = int((g+m)*255); B = int((b+m)*255)
    return f'#{R:02x}{G:02x}{B:02x}'


def register_cuisine(label):
    """Given a free-form cuisine label from Haiku, return its canonical
    slug. Auto-registers (and persists) novel cuisines so the next cron
    run knows the key. Returns '' if the label can't be slugified."""
    key = _slugify_cuisine(label)
    if not key: return ''
    if key in CUISINE_LABEL or key == 'unknown':
        return key
    # Novel cuisine — title-case the human label and persist.
    pretty = _re.sub(r'\s+', ' ', str(label).strip()).title()
    CUISINE_LABEL[key] = pretty
    VALID_CUISINE_KEYS.add(key)
    dyn = _load_dynamic()
    dyn[key] = pretty
    _save_dynamic(dyn)
    return key


def normalize_cuisines(entry):
    """Return a list of valid cuisine keys for a cache entry, handling both
    the new `cuisines: [str, ...]` format AND the old `cuisine: str` format.
    Slugifies + auto-registers novel labels so nothing falls off the edge
    of a fixed taxonomy.
    """
    if not entry: return []
    out = []
    cs = entry.get('cuisines')
    if isinstance(cs, list):
        for c in cs:
            if not isinstance(c, str): continue
            k = register_cuisine(c)
            if k and k != 'unknown' and k not in out:
                out.append(k)
        return out
    c = entry.get('cuisine')
    if isinstance(c, str):
        k = register_cuisine(c)
        if k and k != 'unknown':
            return [k]
    return []


def parse_cuisines_from_llm(parsed):
    """Read the cuisines field from an LLM response dict. Accepts both:
       - `{"cuisines": ["Korean", "Japanese"]}`  (new format, up to 3 entries — free-form)
       - `{"cuisine": "Sri Lankan"}`              (old single-cuisine format)
    Returns a deduplicated list of canonical keys; novel cuisine labels
    are auto-registered (added to CUISINE_LABEL and persisted to
    cuisines_dynamic.json). Returns ['unknown'] only when Haiku explicitly
    said unknown; returns [] when nothing parseable."""
    if not isinstance(parsed, dict): return []
    out = []
    cs = parsed.get('cuisines')
    if isinstance(cs, list):
        for c in cs[:3]:  # cap at 3 — anything more is fusion and we abstain
            if not isinstance(c, str): continue
            raw = c.strip().lower()
            if raw == 'unknown':
                if 'unknown' not in out: out.append('unknown')
                continue
            k = register_cuisine(c)
            if k and k != 'unknown' and k not in out:
                out.append(k)
        return out
    c = parsed.get('cuisine')
    if isinstance(c, str):
        raw = c.strip().lower()
        if raw == 'unknown':
            return ['unknown']
        k = register_cuisine(c)
        if k and k != 'unknown':
            return [k]
    return []
