# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Pivoted from BloomTO 2026-05-12.** The previous codebase (Toronto multiplex parcel filtering) is preserved verbatim under `legacy/bloomto/` and should not be imported into active DemoCalcTO code. See `legacy/bloomto/CLAUDE.md` for the historical project doc.

**DemoCalcTO** is a Toronto demolition cost benchmarking tool. Users enter a Toronto address → get the median + quartile demolition cost for that structure type in that neighbourhood, sourced from city building-permit filings (CKAN) and supplemented with published contractor price ranges.

- **MVP target:** early June 2026.
- **Product spec:** `PRD_DemoCalc.md` + `pivot_statement.md` (in user's workspace, not in repo).
- **Architecture:** Python ETL → static JSON → Apache + vanilla JS UI. Same tech as legacy BloomTO; no Node/React/Postgres.

## Architectural Direction (Read Before Suggesting)

- **Wire-only data exchange.** ETL produces flat JSON, served as static files. No backend API, no database, no live queries against permits data. The aggregate table is small (< 100 KB).
- **One PHP file.** `geocode-proxy.php` is the *only* PHP runtime, and is API-only for Google address autocomplete. Do not propose additional PHP files.
- **No build step.** Vanilla HTML/CSS/JS in a single `index.html`, plus the static JSON. No bundler, no npm, no React.
- **CKAN is the authoritative source.** Demolition permits + building permits + neighbourhood polygons all come from Toronto Open Data, cached locally under `tools/cache/`.

If a request reaches for a Node backend, Postgres, React, or anything from BloomTO's retired heavyweight direction, surface the contradiction before implementing.

## Data Honesty (MANDATORY)

The CKAN building-permits `EST_CONST_COST` field is **systematically under-reported** — developers lowball declared values to reduce city application fees. Calibration suggests filed costs run 15–40% below actual contractor invoice prices. Two non-negotiables:

1. **Always disclose the bias.** The UI must label the figure as "**filed median**" — never "actual" or "typical."
2. **Validate against public contractor pricing.** Maintain a small calibration set (~10–20 anchor points from contractor SEO pages, BILD case studies, RedFlagDeals threads). If filed-median diverges from anchors by > 50%, the aggregate is dropping out of the trust zone — investigate.

When the user proposes a specific dollar figure for the page, verify it via WebSearch / WebFetch before writing into code, copy, or memory. (Same rule as legacy BloomTO.)

## Working on the Site

```bash
# Serve locally for dev
cd /var/www/html/DemoCalcTO
python3 -m http.server 8000

# Or hit it through Apache directly at
#   http(s)://joshuaopolko.com/DemoCalcTO/
```

When changing the site:
- Design tokens live in the single `<style>` block at the top of `index.html`.
- App logic + data fetch live in the `<script>` block at the bottom.
- Data is fetched from `data/democalc.json` (or similar single-file aggregate — TBD as MVP lands).

## Hosting

Apache + mod_php (PHP 7.4) on `joshuaopolko.com` shared host. Practical implications:

- Files placed in `/var/www/html/DemoCalcTO/` are served at `http(s)://joshuaopolko.com/DemoCalcTO/`.
- The `.htaccess` default-denies everything except `index.html` — when adding a new file path the browser needs, widen the allow-list explicitly.
- File ownership is `john:www-data` with group-write — keep new files in that group.
- `python3 -m http.server` for local dev avoids `file://` CORS issues with `fetch()`.

## Source layout

Active modules under `tools/sources/` (each an isolated data loader):

- `_address.py` — address normalization (uppercase, strip suffixes, etc.)
- `_http.py` — HTTP retry + cache helpers
- `address_points.py` — Toronto Address Points (for address geocoding)
- `building_permits.py` — Building Permits CSV loader. Demolition Folder (DM) permits are the primary cost signal.
- `demo_permits.py` — Demolition Permits CSV loader (separate from active building permits).
- `neighborhoods.py` — neighbourhood polygon assignment (address → neighbourhood name).

Anything not on that list lives under `legacy/bloomto/tools/sources/` and is not part of the active codebase. The `building_permits.py` module still contains some BloomTO-era code (luxury-suite back-derivation, nearby-multiplex-permit index, builder-activity counter) — usable for analytics, but not load-bearing for DemoCalc.

## Secrets

`/var/secrets/bloomto.env` (root:www-data 640) holds `GOOGLE_API_KEY` and referer/rate-limit knobs. Never inline, never echo, never commit. Filename kept as `bloomto.env` for now; can rename later.

## Data Source Notes

- **Toronto Open Data Demolition Folder (DM) permits**: primary signal. ~67 permits in current cache with declared cost; widening to a 5-year window will improve sample size.
- **Toronto Open Data Building Permits**: contains demolition-related permits with structure-type tagging. Filter to `PERMIT_TYPE = "Demolition Folder (DM)"` for pure-demo signal.
- **Contractor SEO pages** (Wallace Excavation, Super Human Demolition, Almar, etc.): published price ranges, useful for calibration anchor points.
- **Reddit / RedFlagDeals forum threads**: actual contractor quotes shared by consumers, surprisingly good signal.
- **LinkedIn**: not viable. Aggressive anti-scraping. Requires rotating proxies + risk of account bans. Skip.

## Workflow

For changes that affect the wire data shape:
1. Update the ETL (`tools/build_*.py`).
2. Run the ETL → produces fresh `data/*.json`.
3. Update the frontend (`index.html`) to consume the new field.
4. Verify in browser before declaring done.
5. Run any unit tests (`tools/tests/`).
6. Commit + deploy via rsync (see `legacy/bloomto/CLAUDE.md` for the rsync command pattern — preserved for reference).

## What NOT to do

- Don't import from `legacy/bloomto/`. That code is archived; cherry-pick functions into the active tree if needed.
- Don't propose a database, ORM, message queue, or any backend service. Static JSON aggregate is the architecture.
- Don't add a build step.
- Don't claim "actual" or "real" demolition costs anywhere in the UI — only "filed", "declared", or "permitted."
