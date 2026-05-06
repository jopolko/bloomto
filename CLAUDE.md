# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**v1.1 shipped, v1.2 in progress.** BloomTO is a single-page web app that ranks Toronto's neighborhoods by a composite Net-Zero Score (energy retrofit potential + transit & walkability + tree canopy + Missing Middle housing capacity), surfaces them via search, "Locate me," and a Top N list, and shows a per-neighborhood spotlight with charts. The v1.2 expansion adds a **parcel-level Multiplex Readiness view** for developers — every Toronto parcel scored against as-of-right zoning, Heritage Register status, and 500m subway/streetcar transit buffer, with a flat 2D map and a sortable Top-N gold-mines list.

- **Runtime lives in this directory.** `index.html` is the site (HTML + inline CSS + inline JS, served as a static file by Apache). `geocode-proxy.php` is the *only* PHP runtime — a thin Google Places / Geocoding proxy that holds the API key server-side. No build step, no WordPress block, no plugin code anywhere.
- **Secrets**: `/var/secrets/bloomto.env` (root:www-data 640) holds `GOOGLE_API_KEY` and the same referer/rate-limit knobs as hometurf. Never inline, never echo, never commit.
- **Steering docs**: `.claude/steering/{product,tech,structure}.md` are filled in. Read them before scoping new features.
- **Data**: v1.1 ETL produces `data/neighborhoods.json` from Toronto Open Data (8 sources: SolarTO, Forest/Land Cover, NPP 2021, TTC GTFS, Cycling Network, Centreline, Zoning By-law, Property Boundaries). v1.2 adds Heritage Register and a per-parcel pipeline emitting `data/parcels.geojson`. ETL lives under `tools/`, runs on a workstation only.
- `toronto_datasets.txt` — 630-line snapshot of dataset names + first-line descriptions from the Toronto Open Data portal (https://open.toronto.ca/). Wrap-truncated; not machine-parseable as-is.

## Architectural Direction (Read Before Suggesting)

BloomTO previously scaffolded a WordPress block with Deck.gl + MapLibre + DuckDB-Wasm + Parquet for an interactive 3D map. **That heavyweight direction stays retired.** The plugin at `/var/www/html/wp-content/plugins/bloomto/` was deleted on 2026-05-01 and is not coming back. Do not propose:

- A WordPress block, Gutenberg integration, or React
- Additional PHP files. There is *exactly one* allowed PHP file (`geocode-proxy.php`) and it is API-only — do not grow it into a templating layer or add siblings
- Heavyweight WebGL map libraries: MapLibre, Deck.gl, Mapbox GL, Google Maps JS API
- 3D extrusion, building massing, or photoreal rendering
- Browser-side Parquet, DuckDB-Wasm, GeoArrow, hyparquet, or tile pipelines
- A bundler, npm install, or anything that requires a build before deploy

**Carve-out for v1.2** (added 2026-05-01): a **2D map view via Leaflet** is allowed, scoped to the parcel-level Multiplex Readiness feature. Leaflet is loaded from a CDN (`unpkg.com/leaflet`), no build step, no plugin ecosystem, no 3D in the browser. Per-parcel point/polygon rendering is allowed for this view only — the neighborhoods Top N + spotlight pages stay map-free. If a request reaches for any of the heavyweight libraries above, push back and use Leaflet + a static GeoJSON fetch instead.

**ETL-input vs render distinction** (added 2026-05-01): the "no 3D" rule is about the **wire format and the browser**, not the workstation ETL. 3D building data (e.g., the Toronto 3D Massing dataset's footprint × height records) MAY be consumed in `tools/` for shadow, solar-irradiance, or building-envelope calculations whose outputs are reduced to flat 2D geometry + numeric properties before serialization. The browser never receives 3D geometry, never extrudes, never renders heights. If you find yourself wanting to ship vertices with z-values, stop — recompute the answer in the ETL and ship a number.

If a request seems to ask for one of the still-retired items, surface the contradiction with the steering docs before implementing.

## Working on the Site

```bash
# Serve locally for dev (avoids file:// CORS issues, e.g. with fetch + Geolocation)
cd /var/www/html/bloomto
python3 -m http.server 8000
# open http://localhost:8000

# Or hit it through Apache directly at
#   http://<host>/bloomto/
```

The dev loop is: edit `index.html` → refresh browser. There is nothing to install or compile.

When changing the site:
- Custom design tokens (palette, glass cards, score pills, etc.) live in the single `<style>` block at the top.
- Layout uses Tailwind utility classes (loaded from `cdn.tailwindcss.com`).
- App logic + mock data live in two `<script>` blocks at the bottom: AOS init, then the main app.
- The `NEIGHBORHOODS` array is the source of truth for everything the UI displays. Updating mock numbers there is the fastest way to A/B copy or layouts.

## Spec-Driven Workflow

This project uses the `claude-code-spec-workflow` toolchain. The intended development sequence:

1. **`/spec-steering-setup`** — first step on a fresh project. Generates `.claude/steering/{product,tech,structure}.md` from analysis + user Q&A. Already done; re-run only if the project's direction shifts again.
2. **`/spec-create <feature> [description]`** — runs the four-phase spec workflow: Requirements → Design → Tasks → (optionally) generated per-task slash commands. Each phase requires explicit user approval before the next; do not skip ahead. Output lives in `.claude/specs/<feature>/`.
3. **`/spec-execute <task-id> <feature>`** — implement an individual task from a spec.
4. **`/spec-list`, `/spec-status`** — inspect spec progress.

Bug workflow (parallel set): `/bug-create` → `/bug-analyze` → `/bug-fix` → `/bug-verify`, with `/bug-status` for inspection. Reports land in `.claude/bugs/<bug>/`.

Validator subagents in `.claude/agents/` (`spec-requirements-validator`, `spec-design-validator`, `spec-task-validator`, `spec-task-executor`) are invoked by the slash commands; you generally won't call them directly.

The slash commands shell out to a `claude-code-spec-workflow` CLI (e.g. `claude-code-spec-workflow get-steering-context`, `... get-template-context spec`, `... generate-task-commands {spec-name}`). If those binaries aren't on PATH, the commands will still work by reading templates directly from `.claude/templates/`.

## Hosting Context

This directory lives under `/var/www/html/` on an Apache + mod_php (PHP 7.4) WordPress host shared with several other projects (see `/var/www/html/CLAUDE.md` for the broader picture). Practical implications:

- The parent dir is *not* a git repo; if version control is wanted for bloomto, initialize it inside `bloomto/`.
- Apache serves files placed here at `http(s)://<host>/bloomto/`. The `.htaccess` here default-denies everything except `index.html` — when adding a new file path that the browser needs (e.g. `data/neighborhoods.json`), widen the allow-list explicitly.
- File ownership is `john:www-data` with group-write — keep new files in that group so Apache can read them.
- For local dev, `python3 -m http.server` from inside `bloomto/` avoids `file://` CORS issues for `fetch()` and Geolocation prompts.

## Working with `toronto_datasets.txt`

The file is human-readable text scraped from dataset descriptions, with hard line wraps mid-paragraph (each dataset's description is truncated to roughly the first wrapped line). It is *not* line-per-dataset and *not* machine-parseable as-is. Before relying on it programmatically, expect to either re-fetch the canonical list from the Toronto Open Data CKAN API (`https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action/package_list`) or write a tolerant parser.

## MANDATORY: Verify dollar figures, program names, and dates before surfacing

When the user (or you) propose a specific dollar amount, program name,
deadline date, or regulatory citation for the page or any product
artifact, you MUST verify it via WebSearch / WebFetch BEFORE writing it
into code, copy, or memory. No exceptions.

This includes:
- Government program names and amounts (DC waivers, HST rebates, GST
  rebates, Toronto Hydro grants, NRRPR, HRSP, HELP, Greener Homes, etc.)
- Bill numbers, regulation numbers, council item numbers (e.g., Bill 185,
  EX24.2, O. Reg 462/24)
- Application/registration deadlines
- Per-unit / per-project caps and ceilings
- Eligibility criteria

If a value comes from a third-party suggestion (Gemini, ChatGPT, a memo,
a forum post): treat it as unverified until you've grounded it in a
primary source (Government of Ontario / Toronto.ca / canada.ca / a
program's own application page). Flag any discrepancies you find.

When the user notices an unverified figure on the page, they lose trust
in every other figure on the page. The cost of one wrong number cascades.

## MANDATORY: Wire-field audit when adding/changing per-parcel data

When you add, rename, or remove a field on `data/parcels.geojson` (or its
`parcels-top.json` projection), you MUST audit every downstream consumer
BEFORE declaring the work done. Missing one of these is the #1 source of
"why the fuck do I have to keep catching these things?" frustration.

**Checklist — every new wire field needs to be considered in ALL of:**

ETL / wire contract (Python):
- [ ] `tools/parcel_io.py` → `FEATURE_PROPERTIES` (validator) + `META_KEYS` if meta
- [ ] `tools/parcel_io.py` → `REQUIRED_STATS_KEYS` if a per-build counter is added
- [ ] `tools/parcels_top_io.py` → `ROW_KEYS` + `project_features()` projection
- [ ] `tools/build_parcels.py` → emit on each Feature + meta.stats counter
- [ ] `tools/tests/test_parcel_io.py` fixtures
- [ ] `tools/tests/test_parcels_top_io.py` fixtures
- [ ] `tools/tests/test_e2e_parcels.py` synthetic-fixture index/tree

Frontend (`goldmines.html`):
- [ ] `popupHtml(r)` — map marker popup table
- [ ] `detailHtml(r)` — slide-in detail panel (Lot / Transit / Regulatory sections)
- [ ] `pickRowHtml(r, i)` — list row inline indicators / badges
- [ ] `attractivenessScore(r)` — Developer Attractiveness formula (positive or negative weight?)
- [ ] `consider(r)` — Considerations narrative (good / caution / neutral)
- [ ] `badgesFor(r)` and `badgeReasons(r)` — map / list badges
- [ ] `gateRows(r)` — "How the score was built" panel
- [ ] `incentivesStackedFor(r)` — per-parcel incentive list
- [ ] `SORT_COMPARATORS` + sort dropdown — does it warrant a sort option?
- [ ] `PRESETS` — does it warrant a filter chip?
- [ ] Marker visualization (`renderMarkers`) — should it change pin appearance?
- [ ] `meta.solarMethodology` / `meta.scoreFormula` / `meta.bloomFormula` —
      do the disclosure strings need updating?

After editing, run all three of these before reporting done:
1. `node -e "<script syntax check, see end of section>"` for goldmines.html
2. `.venv/bin/python -m unittest discover -s tools/tests` (must show "OK")
3. Open the page locally; verify the new field is *visible* somewhere — not just
   present on the wire. Wire-only adds are not "shipped."

When the user notices a missing consumer, do the FULL audit before responding,
not just the specific gap they mentioned. Pattern is: if you missed one, you
likely missed others. Surface them proactively in the same reply.

**Do not declare a wire change "done" if any of the above are skipped.** It
shifts the burden of verification to the user, which is the explicit
anti-pattern this checklist exists to prevent.

JS syntax check one-liner (run after every goldmines.html edit):
```
node -e "const fs=require('fs');const h=fs.readFileSync('/home/josh/bloomto_work/goldmines.html','utf8');const m=h.match(/<script>([\s\S]*?)<\/script>/g);let ok=true;for(const b of m){try{new Function(b.replace(/^<script[^>]*>/,'').replace(/<\/script>$/,''))}catch(e){ok=false;console.log('ERR:',e.message)}}console.log(ok?'syntax OK':'SYNTAX ERRORS')"
```
