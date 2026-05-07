# BloomTO

A static web app that ranks Toronto parcels for as-of-right multiplex development against current zoning, Heritage Register status, transit proximity, and friction signals (Tree Bylaw protection, TRCA flood-regulation, TTC station overlap). Audience is multiplex developers — most parcels in the list are off-market, so it functions as a *target list*, not a *buy list*: filter, sort, run owner-of-record lookup, do direct outreach.

## Pages

| URL | What it is |
|---|---|
| `/` (`index.html`) | **Today's Top Multiplex Sites** — the active product. 528K Toronto parcels filtered to a ranked elite set with per-parcel deal context (lot size, footprint, transit distance, sixplex eligibility, neighborhood permit velocity, Bill 185 incentive total). Detail panel surfaces lot geometry, Underwriting snapshot KPIs, regulatory citations, daily-refreshed owner-activity signals (severance / demo / violation), and a print-as-PDF deal-doc. |
| `/neighborhoods.html` | v1.1 neighborhood Net-Zero scorer — every Toronto neighborhood ranked by retrofit potential + transit + tree canopy + Missing Middle capacity. Older product, kept for archival access; not actively developed. |
| `/parcels.html` | Older parcels view; superseded by `index.html`. |

## Stack

Deliberately boring:

- Static HTML + inline CSS + inline JS, served by Apache.
- One PHP file: [`geocode-proxy.php`](geocode-proxy.php) — thin Google Places / Geocoding proxy that keeps the API key server-side.
- No build step. No bundler. No npm install. No WordPress plugin. No 3D in the browser.

The "no heavy frontend" decision is load-bearing — see [`CLAUDE.md`](CLAUDE.md) for the architectural guardrails (and the list of libraries that are explicitly off the table).

## Local development

```bash
cd /path/to/bloomto
python3 -m http.server 8000
# open http://localhost:8000
```

The `python3 -m http.server` step matters: `file://` breaks `fetch()` for the JSON data files and breaks the Geolocation prompt. Edit `index.html` (the canonical file), refresh the browser — that's the entire dev loop.

## Data pipeline

Browser reads two static JSON files:

- `data/parcels-top.json` (~4 MB, the elite set served on first load)
- `data/parcels-broader.json` (~18 MB, lazy-loaded via the "Browse all candidates" toggle)

Both are projections of the master `data/parcels.geojson` (~310 MB, gitignored — regenerable from ETL). The neighborhoods view uses `data/neighborhoods.json` (committed).

ETL lives under [`tools/`](tools/) and runs on a workstation, not on the server. Python, **17 Toronto Open Data sources** (Property Boundaries, Zoning By-law 569-2013, Heritage Register, SolarTO, 3D Massing, Building Outlines, Toronto Centreline, TTC GTFS, Forest & Land Cover, NPP 2021, Cycling Network, Basement Flooding Study Areas, TRCA Reg 41/24, RapidTO Corridors, Building Permits, Street Tree Data, Community Council Boundaries) plus **OpenStreetMap via Overpass API** (TTC + GO + LRT station polygons; subway-entrance nodes with 30m exclusion buffer; ~50K parking / industrial / construction / brownfield exclusion polygons).

```bash
# Sequential rebuild (~2.5 hours, single core):
.venv/bin/python3 tools/build_parcels.py

# Parallel rebuild (~30 min on an 8-core box):
.venv/bin/python3 tools/build_parcels.py --workers 8

# Project the master GeoJSON to the two JSON files the browser reads:
.venv/bin/python3 tools/build_parcels_top.py

# Audits (consistency + external cross-check + permits backtest):
.venv/bin/python3 tools/audit_wire_consistency.py
.venv/bin/python3 tools/audit_external_crosscheck.py
.venv/bin/python3 tools/audit_permits_backtest.py

# Tests:
.venv/bin/python3 -m unittest discover -s tools/tests
```

Full source map and rebuild internals are in [`tools/README.md`](tools/README.md).

## Scoring

Every number on the page comes from a city-published dataset — no proprietary composite scores. The headline `score` is `100 × residential × heritage_factor × transit_factor × multiplier_factor`, all four inputs from city sources (Zoning By-law 569-2013, Heritage Register, TTC GTFS, Property Boundaries). The Bill 185 stacked-incentive total (`$1.0B+` across the elite set) is `$200K DC waiver + $25K × 0.5 × max-target-units (parking minimums eliminated)` — straight arithmetic on the 2025 EX24.2 + Bill 185 amounts.

What's *not* shown: Net-Zero composite score, Bloom tier number, Deal score, Solar 0–100 — all BloomTO synthesised values that earlier versions surfaced. Stripped 2026-05-06 because synthesised scores erode trust faster than they help; the underlying signals (subway distance, sixplex eligibility, mature tree count, TRCA-regulated, etc.) are now visible directly so devs can compose their own ranking.

## Secrets

Nothing committed. `geocode-proxy.php` reads `GOOGLE_API_KEY` from `/var/secrets/bloomto.env` (mode `640`, owner `root:www-data`) at request time. The same file holds the GitHub token used for repo automation. `.env` and `*.env` are gitignored as a defensive guard.

## Project status

- **v1.1 shipped**: neighborhood Net-Zero scoring (now at `/neighborhoods.html`).
- **v1.2 active**: parcel-level multiplex finder at `/`. Live and evolving.
- **Queued for next ETL rebuild**: existing-unit-count gate (apartment exclusion via 3D Massing height × footprint), Address Points USE_CODE filter (parking-lot exclusion), point-only OSM stations matched to Building Outlines polygons, per-parcel CR/RM/RAC zoning cap from FSI envelope, TRCA riverine flood polygon overlay refinement.
- **v1.3 candidate**: parcel-aware feasibility / pro-forma calculator. Biggest competitive whitespace; queued after v1.2 data fixes ship.

Spec-driven workflow lives under `.claude/specs/` — see [`CLAUDE.md`](CLAUDE.md) for the slash-command sequence.
