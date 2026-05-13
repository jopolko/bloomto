# NowServingTO

**Toronto's newly-opened restaurants, by cuisine.** A daily-fresh directory of every restaurant licensed in the past 365 days, classified into 40+ cuisine buckets via Claude Haiku + web search, verified against Google Places and Bing, link-health-checked, and served as a static page.

Live at **https://nowservingto.com/**.

## What it does

Every morning the cron pulls the City of Toronto business-licence feed from CKAN, finds the food businesses issued in the last 365 days, asks Claude Haiku 4.5 to classify each by cuisine (using `web_search` to read actual menus, reviews, owner bios — not just the operating name), verifies each is actually operating, and serves a single-page directory with a dropdown of cuisines and the newest entries first. Links go to the restaurant's website when one exists; otherwise to its Google Maps profile.

The point: an **immigrant-first** discovery feed. The newest Ethiopian, Tamil, Bangladeshi, Persian, Filipino, Salvadoran, Jamaican, etc. spots get surfaced the day their licence is approved, with the dropdown surfacing the cuisine you're actually looking for — not "ethnic" lumped together.

## How it works

```
Toronto Open Data CSV (CKAN)
  ↓ tools/cron_daily_openings.sh
  ↓ tools/llm_classify.py / llm_classify_batch.py   (name-only cuisine, cheap fallback)
  ↓ tools/llm_verify.py / llm_verify_batch.py       (web_search → cuisine + operating + website)
  ↓ tools/check_link_health.py                       (HEAD-probe every URL, drop 4xx/5xx)
  ↓ tools/inject_openings.py                         (apply chain denylist, gate to verified-open)
  ↓ data/corridors.json                              (~340 KB, daily-refreshed)
  ↓ index.html                                       (vanilla JS, no build step, no framework)
```

No backend. No database. No framework. The page is a single HTML file that fetches one JSON file. Apache serves both.

### Tagging hierarchy

1. **Chain denylist** (code-level, deterministic) → known American/Canadian chains (Popeyes, KFC, Tim Hortons, etc.) always return `unknown` regardless of theme.
2. **web_search-informed cuisine** → Claude Haiku reads menus, owner bios, blogTO articles, Instagram pages, and decides cuisine from the evidence. Strongest signal.
3. **Name-only LLM** → cheap classification from operating name + address. Used for entries the verifier hasn't reached yet.
4. **Keyword fallback** → substring pattern match. Last resort.

### Self-healing

Each verification result is cached with a `verified_at` timestamp. Tier-based re-checks: own-website verdicts re-checked every 180 days; Instagram/social every 30 days; blogTO/Yelp/Maps every 14 days; no-link entries every 14 days; "unclear" verdicts every 7 days. When a brand-new place finally gets indexed by Google or covered by blogTO, the next cron cycle picks it up and the link silently upgrades.

## Coverage

- ~2,000 food licences issued in any rolling 365-day window
- ~50% successfully classified to a specific cuisine (the rest are generic "Jim's Snack Bar"-style names that don't carry an ethnic signal)
- ~900 of those verified-open via web evidence at any given moment
- 40+ populated cuisine buckets, dropdown sorted alphabetically with "All cuisines" pinned to the top

Cuisines split out where meaningfully different: Italian, Chinese, Japanese, Korean, Vietnamese, Filipino, Thai, Indonesian, Malaysian, Burmese, South Asian (other), Pakistani, Afghan, Bangladeshi, Tamil, Tibetan, Caribbean (other), Jamaican, Trinidadian, Guyanese, Haitian, Greek, Portuguese, Polish, French, Irish/UK, German, Jewish deli, Eastern European (other), Ukrainian, Russian, Hungarian, Middle Eastern (other), Lebanese, Turkish, Syrian, Persian, Latin American (other), Mexican, Salvadoran, Peruvian, Colombian, Brazilian, East African (other), Ethiopian, Eritrean, Somali, West African (other), Nigerian, Ghanaian, Moroccan.

## Cost

- Daily steady-state: **~$0.30/day** of Anthropic API credit (Haiku tokens + web_search calls for the 5–15 new licences per day, plus tier-aware re-checks)
- ~$10/month total
- Zero ongoing Google API spend (Places enrichment was a one-time $6 cold-start; tier-based recheck via Bing-backed web_search is plenty)

## Setup

```bash
# clone
git clone https://github.com/jopolko/nowservingto.git
cd nowservingto

# venv + deps
python3 -m venv .venv
.venv/bin/pip install -r tools/requirements.txt

# put credentials at /var/secrets/nowservingto.env (chmod 600)
# required: ANTHROPIC_API_KEY=sk-ant-...
# optional: GOOGLE_API_KEY (only needed if you want to re-enrich via Places)

# run once manually to verify
tools/cron_daily_openings.sh

# install daily cron (Toronto morning)
crontab -e
# add:
#   17 5 * * * /path/to/nowservingto/tools/cron_daily_openings.sh
```

The four small caches under `tools/cache/*.json` are committed, so a fresh clone has ~900 verified entries to start with. Only the daily delta hits the API.

## Layout

```
nowservingto/
├── index.html                          # the page; reads data/corridors.json
├── data/corridors.json                 # daily-refreshed wire file
├── tools/
│   ├── cron_daily_openings.sh          # the actual cron entry point
│   ├── inject_openings.py              # builds the wire file from caches + CSV
│   ├── llm_classify.py                 # sync name-only cuisine classifier
│   ├── llm_classify_batch.py           # batch version (50% off, async)
│   ├── llm_verify.py                   # sync web_search verifier (cuisine + operating + website)
│   ├── llm_verify_batch.py             # batch version
│   ├── enrich_places.py                # one-time Google Places enrichment (frozen cache)
│   ├── check_link_health.py            # HEAD-probe cached URLs, mark broken
│   ├── audit_classifications.py        # periodic suspect-classification report
│   ├── deploy_to_vps.sh                # one-shot rsync + bootstrap to prod
│   ├── build_corridors.py              # heavier weekly ETL (parcels, dev apps, etc.)
│   └── cache/                          # JSON caches (4 small active ones committed)
└── legacy/                             # archived BloomTO and DemoCalcTO codebases
```

## What it's not

- Not a Yelp clone. No user-submitted content, no review system, no claim flow.
- Not a curated editorial list like blogTO. Entries come from the City's licence feed, not editorial taste.
- Not a complete restaurant directory. Only places licensed in the last 365 days.
- Not a historical record. As licences age past 365 days they fall out of the feed.

## Status

- 2026-05-13 — pivoted from "cultural corridor displacement map" to "now open by cuisine"
- 2026-05-12 — pivoted from DemoCalcTO (demolition cost benchmarking) to corridor map
- BloomTO (multiplex parcel filtering) archived under `legacy/bloomto/`
- DemoCalcTO archived under `legacy/democalcto/`

## License

Code: MIT. Data: from the [City of Toronto Open Data Portal](https://open.toronto.ca/) under its Open Data Licence. Cuisine classifications are generated and may be wrong — flag issues via the repo's GitHub issues.
