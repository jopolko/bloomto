# BloomTO

A static web app that ranks Toronto for low-carbon redevelopment, scored against open city data.

Two views, same data foundation:

- **Neighborhood Net-Zero Score** ([`index.html`](index.html)) — every Toronto neighborhood ranked by a composite of energy-retrofit potential, transit & walkability, tree canopy, and Missing Middle housing capacity. Search, "Locate me," Top-N list, per-neighborhood spotlight with charts.
- **Multiplex Parcel Finder** ([`goldmines.html`](goldmines.html)) — every Toronto parcel scored for as-of-right multiplex feasibility against current zoning, Heritage Register status, and 500m subway/streetcar buffers. Flat 2D Leaflet map + sortable Top-N list with developer-attractiveness ranking.

Audience is developers and city-builders, not homeowners.

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

The `python3 -m http.server` step matters: `file://` breaks `fetch()` for the JSON data files and breaks the Geolocation prompt. Edit the HTML, refresh the browser — that's the entire dev loop.

## Data pipeline

Site reads two static JSON/GeoJSON files:

- `data/neighborhoods.json` (committed)
- `data/parcels.geojson` + `data/parcels-top.json` (~250 MB, regenerable, **gitignored**)

ETL lives under [`tools/`](tools/) and runs on a workstation, not on the server. Python, twelve Toronto Open Data sources (Solar­TO, forest/land cover, TTC GTFS, zoning by-law, property boundaries, Heritage Register, 3D massing, …). Full source map and rebuild instructions are in [`tools/README.md`](tools/README.md).

```bash
cd tools
python3 build_neighborhoods.py     # → data/neighborhoods.json
python3 build_parcels.py           # → data/parcels.geojson
python3 build_parcels_top.py       # → data/parcels-top.json
python3 -m unittest discover -s tests
```

## Secrets

Nothing committed. `geocode-proxy.php` reads `GOOGLE_API_KEY` from `/var/secrets/bloomto.env` (mode `640`, owner `root:www-data`) at request time. The same file holds the GitHub token used for repo automation. `.env` and `*.env` are gitignored as a defensive guard.

## Project status

v1.1 shipped (neighborhood scoring). v1.2 in progress (parcel-level Multiplex Readiness). v1.3 candidate is a parcel-aware feasibility calculator. Spec-driven workflow lives under `.claude/specs/` — see [`CLAUDE.md`](CLAUDE.md) for the slash-command sequence.
