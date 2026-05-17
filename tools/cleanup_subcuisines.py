#!/usr/bin/env python3
"""
One-shot cleanup for cuisine keys Haiku missed collapsing under the
parent-country rule. Walks web_verify_cache and rewrites specific
leaker keys to their parents. Idempotent — safe to run repeatedly.

Run on the VPS where the active wv cache lives:
  cd /var/www/html/nowservingto && python3 tools/cleanup_subcuisines.py
Then re-run inject_openings.py to regenerate corridors.json + per-listing
HTML with the cleaned cuisine assignments.

Bounded scope: only the specific leaker keys listed below are rewritten.
Other dynamically-registered cuisines (Uyghur, Tibetan, Cape Verdean,
etc.) are left untouched — those are legit country-level distinctions.
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WV   = ROOT / 'tools' / 'cache' / 'web_verify_cache.json'

# Specific known-leaker rewrites. Each entry is a one-time correction to
# fix Haiku's prior judgments that the new prompt now forbids. Not a
# general-purpose mapping — only here so we don't have to burn another
# $2 batch to re-judge entries Haiku will now classify correctly anyway.
REWRITES = {
    # Sub-cuisines → parent country
    'hakka':           'chinese',
    'cantonese':       'chinese',
    'shanghainese':    'chinese',
    'sichuan':         'chinese',
    'hong_kong':       'chinese',
    'yunnan_chinese':  'chinese',
    'south_indian':    'indian',
    'north_indian':    'indian',
    'maharashtrian':   'indian',
    'gujarati':        'indian',
    'hyderabadi':      'indian',
    'rajasthani':      'indian',
    'indian_chinese':  'indian',   # fusion — fold to dominant tradition
    'indian_hakka':    'indian',
    'southern_italian':'italian',
    'sicilian':        'italian',
    # Duplicate slugs of seed cuisines (canonical taxonomy uses LHS)
    'middle_eastern':  'middle_east',
    'nepali':          'nepalese',
    # Overly umbrella labels — fold to the closest seed cuisine
    'mediterranean':   'middle_east',
    # Audience-misfits (not the diaspora ethnic-cuisine focus). Mark as
    # validator_drop so inject removes them from the feed.
    'canadian':        '__DROP__',
    'american':        '__DROP__',
    'hawaiian':        '__DROP__',
    'european':        '__DROP__',
}

def main():
    wv = json.loads(WV.read_text())
    n_rewritten = 0
    n_dropped   = 0
    for k, e in wv.items():
        cs = e.get('cuisines') or ([e['cuisine']] if e.get('cuisine') else [])
        if not cs: continue
        new_cs = []
        should_drop = False
        for c in cs:
            target = REWRITES.get(c)
            if target == '__DROP__':
                should_drop = True
            elif target:
                new_cs.append(target)
            else:
                new_cs.append(c)
        # Dedupe preserving order
        seen = set(); new_cs = [x for x in new_cs if x not in seen and not seen.add(x)]
        if should_drop:
            if e.get('validator_drop') != 'not-ethnic-cuisine-fit':
                e['validator_drop'] = 'not-ethnic-cuisine-fit'
                e['validator_evidence'] = (e.get('validator_evidence') or '') + ' [cleanup: audience-misfit cuisine label]'
                n_dropped += 1
        if new_cs != cs:
            e['cuisine']  = new_cs[0] if new_cs else None
            e['cuisines'] = new_cs or None
            n_rewritten += 1
    WV.write_text(json.dumps(wv, separators=(',', ':')))
    print(f"rewrote cuisine on {n_rewritten} entries; flagged {n_dropped} for drop")

if __name__ == '__main__':
    main()
