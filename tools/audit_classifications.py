#!/usr/bin/env python3
"""
Audit current classifications for likely-wrong tags. Surfaces three kinds of
suspicion so they can be hand-corrected or pushed through re-verification:

  1. Chain-substring matches that escaped the denylist
  2. Generic name + specific cuisine (e.g., "EXPRESS CHICKEN" tagged jamaican)
  3. Conflict between name-only LLM tag and web_search tag
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LLM = json.loads((ROOT / 'tools' / 'cache' / 'llm_cuisine_cache.json').read_text())
WEB = json.loads((ROOT / 'tools' / 'cache' / 'web_verify_cache.json').read_text())

# Match patterns of low-information names that probably shouldn't be ethnically tagged
GENERIC_NAME_TOKENS = (
    'KITCHEN', 'EXPRESS', 'GRILL', 'HOUSE', 'RESTAURANT', 'CAFE', 'FOODS',
    'PLACE', 'STATION', 'CORNER', 'PALACE', 'STAR', 'KING', 'QUEEN',
    'DELIGHT', 'FLAVOURS', 'FLAVORS', 'TASTE', 'BISTRO', 'DINER',
)

# Names that suggest a chain we haven't denylisted yet
SUSPECT_CHAIN_HINTS = (
    'KITCHEN INC', 'FRANCHISE', 'LTD', 'CORP', 'CO.', '#1', '#2', '#3', '#4',
)

def report():
    chain_hint = []
    conflicts = []
    generic_tagged = []

    for k, v in LLM.items():
        if v.get('status') != 'ok': continue
        cuisine = v.get('cuisine')
        if cuisine in (None, 'unknown'): continue
        name = k.split('||')[0]
        nu = name.upper()

        # 1. Possibly a chain we missed (commercial-name hints)
        for h in SUSPECT_CHAIN_HINTS:
            if h in nu and cuisine not in ('italian', 'chinese', 'japanese'):  # mainstream cuisines OK
                chain_hint.append((name, cuisine, h))
                break

        # 2. Generic-name + ethnic-specific tag
        if any(t in nu for t in GENERIC_NAME_TOKENS):
            # And a SPECIFIC (not umbrella) cuisine
            if cuisine in ('jamaican','trinidadian','guyanese','haitian','ethiopian',
                           'eritrean','somali','nigerian','ghanaian','moroccan',
                           'salvadoran','peruvian','colombian','brazilian',
                           'pakistani','afghan','bangladeshi',
                           'ukrainian','russian','hungarian','syrian','turkish',
                           'lebanese','indonesian','malaysian','burmese'):
                generic_tagged.append((name, cuisine))

        # 3. LLM-vs-web disagreement
        w = WEB.get(k)
        if w and w.get('status') == 'ok' and w.get('cuisine'):
            if w['cuisine'] != cuisine and w['cuisine'] != 'unknown':
                conflicts.append((name, cuisine, w['cuisine']))

    if chain_hint:
        print(f"== POSSIBLE UNDENYLISTED CHAINS ({len(chain_hint)}) ==")
        for n, c, h in chain_hint[:30]:
            print(f"  [hit: {h}]  {n[:48]:48s} → {c}")
        print()
    if generic_tagged:
        print(f"== GENERIC NAME + SPECIFIC ETHNIC TAG ({len(generic_tagged)}) — high false-positive risk ==")
        for n, c in generic_tagged[:30]:
            print(f"  {n[:48]:48s} → {c}")
        print()
    if conflicts:
        print(f"== LLM-NAME vs WEB-SEARCH DISAGREEMENTS ({len(conflicts)}) ==")
        for n, llm_c, web_c in conflicts[:30]:
            print(f"  {n[:42]:42s}   name-only={llm_c:14s}  web={web_c}")
        print()
    if not (chain_hint or generic_tagged or conflicts):
        print("clean.")

if __name__ == '__main__':
    report()
