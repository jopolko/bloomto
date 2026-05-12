# democalcto

**Toronto demolition cost benchmarking.** Enter a Toronto address, get the median + quartile demolition cost for that structure type in that neighbourhood, sourced from city building-permit filings and supplemented with public contractor pricing.

---

## What this is

Toronto developers currently spend $2–5K and 1–2 weeks getting demolition quotes from contractors. There's no public, aggregated market data for demolition costs by neighbourhood or structure type. DemoCalcTO aggregates Toronto's public demolition permit data (CKAN) — joined to neighbourhood polygons and validated against published contractor price ranges — so a developer can get a benchmark in seconds instead of placing 3–5 contractor calls.

**Target user:** Toronto property developers (solo to mid-size), demolition contractors, cost estimators.

**Pitch:** "Filed median demolition cost in your neighbourhood, before you call a contractor."

## What this isn't

- Not a contractor quote. Filed permit values are systematically under-reported (15–40% in our calibration sample); the published median is a floor, not a fixed-price quote.
- Not a list of teardown opportunities. (That was BloomTO. Pivoted 2026-05-12; legacy code lives in `legacy/bloomto/`.)
- Not an RSMeans replacement for full construction estimating — just the demolition phase.

## Status

- Pivoted from BloomTO 2026-05-12. MVP target: early June 2026.
- See `PRD_DemoCalc.md` and `pivot_statement.md` (in user's workspace) for the product spec.

## Tech stack

- **ETL:** Python 3.10+ (stdlib + `requests`, `shapely`, `pyproj`)
- **Data source:** Toronto Open Data (CKAN) — Building Permits dataset
- **Serving:** Apache (existing host) + static JSON + light vanilla JS UI
- **Address autocomplete:** `geocode-proxy.php` (Google Geocoding, key on server)
- **Caching:** local file cache under `tools/cache/` (gitignored)

## Layout

```
democalcto/
├── tools/
│   ├── cache/             # Local data cache (gitignored)
│   └── sources/           # CKAN data loaders
│       ├── _address.py    # address normalization
│       ├── _http.py       # retry + cache helpers
│       ├── address_points.py
│       ├── building_permits.py  # core demolition data source
│       ├── demo_permits.py
│       └── neighborhoods.py
├── data/                  # ETL outputs (mostly gitignored)
├── geocode-proxy.php      # Google API proxy (server-side key)
├── requirements.txt
├── CLAUDE.md              # project guidance for Claude Code
├── README.md              # this file
└── legacy/
    └── bloomto/           # archived BloomTO codebase (do not import)
```

## Running locally

```bash
# Set up venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# ETL (to be built — see PRD)
.venv/bin/python -m tools.build_democalc

# Serve locally (no build step)
cd /var/www/html/democalcto && python3 -m http.server 8000
```

## Secrets

`GOOGLE_API_KEY` lives in `/var/secrets/democalcto.env` on the prod host (root:www-data 640). Never inline, never echo, never commit. Used by `geocode-proxy.php` for address autocomplete.

## Hosting

Apache on `joshuaopolko.com`, serving `/var/www/html/democalcto/`. `.htaccess` default-denies everything except explicit allow-list of files needed by the UI.

## License

TBD (private MVP).
