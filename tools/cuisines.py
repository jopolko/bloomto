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

# Used by recovery scripts (llm_verify_batch, llm_classify_batch,
# llm_recover_cuisine, llm_search_recover_cuisine) to gate Haiku output.
# Includes 'unknown' as a valid response for "can't determine" cases.
VALID_CUISINE_KEYS = set(CUISINE_LABEL.keys()) | {'unknown'}
