# 2026-05-08 final state — afternoon update

## Curated cohort: 244 picks, 94% ground-truth-grade structure type

| Source | Count | % | Confidence |
|---|---:|---:|---|
| Toronto Building Permit (`permit`) | 165 | 68% | ✓ city-recorded |
| OpenStreetMap (`osm`) | 65 | 27% | ✓ volunteer-mapped, 96% agreement w/ permits on overlap |
| Vacant lot (no structure) | 14 | 6% | ✓ deterministic absence |
| Classifier heuristic | 0 | 0% | — (downgraded to broader) |

**Every elite pick has either a Toronto building-permit STRUCTURE_TYPE record on its address, an OpenStreetMap `building=*` tag, or is a vacant lot.** No cross-boundary classifier guesses leak into curated.

## Today's stack of changes (since you went to bed last night)

| SHA | Title |
|---|---|
| `eb9d911` | Permit-derived structure type ground truth (active + cleared + demo permits = 126K addresses) |
| `3815036` | Frontend ✓-city-record / est. tags |
| `5327358` | Elite gate requires `existingStructureSource = "permit"` |
| `4e3c433` | OSM building-tag source: 3rd ground-truth path, lifts coverage 43% → 58% |
| `51dff81` | Tax-exempt institutional address gate (catches 243 Coxwell + 676 others) |

## Headline catches

- **243 Coxwell Ave** (Royal Canadian Legion Branch #1, Baron Byng) — flagged by the user; was being curated as a fat detached candidate. Now correctly excluded via the tax-exempt gate.
- **859 Dundas St E**, **195 Chatham Ave**, **440 Queen St E** — three other tax-exempt institutional parcels that had been leaking into curated; all now excluded.
- **160 Dowling Ave** (caught yesterday) — the cross-boundary classifier improvement removed it; permit data confirms it's a row, so it would have been excluded by the new gate anyway.

## What we don't have (free-data ceiling, honestly)

- **MPAC bulk roll data** — paywalled, only legit way to get 100% per-parcel structure type coverage citywide. Province itself is an MPAC customer; they don't redistribute. Confirmed via data.ontario.ca metadata: "This data is not and will not be made available."
- **Teranet / OnLand bulk extracts** — paywalled. Per-parcel manual lookups remain a legitimate dev workflow (link out from the panel for the 3-5 parcels they're seriously pursuing).
- **HouseSigma / TREB MLS** — ToS prohibits scraping; underlying MLS data has CREA/RECO licensing constraints. No legal path to ingest at scale.
- **Toronto Property Tax Lookup tool** — gated by roll-number + customer-number from the property's tax bill. Only works for parcels you already own. Useless for arbitrary parcel lookups.

For everything else (the 6% of curated that's vacant, the broader cohort that's mostly classifier-derived), the cross-boundary classifier remains the residual fallback — UI marks those with "est." so the dev knows to verify via Street View.

## ETL skip stats (this build)

| Reason | Parcels skipped |
|---|---:|
| Tall existing building (≥18m, 3D Massing) | 23,787 |
| OSM landuse (parking / industrial / etc.) | 13,642 |
| Non-buildable | 7,458 |
| TTC station infra | 1,759 |
| **Tax-exempt institutional (NEW)** | **347** |
| No neighborhood polygon | 300 |

## Build / commit / push state

- 33 commits ahead of `origin/main` → all pushed (last push: `4e3c433..51dff81`)
- Tree clean
- Build runtime: 31:55 (vs ~30 min baseline; +6 % from the new tax-exempt + OSM lookups, acceptable)
- 165 unittest, 0 failures

## Audit hooks (paste into terminal to verify)

```bash
cd /home/josh/bloomto_work
python3 -c "
import json; from collections import Counter
d = json.load(open('data/parcels-top.json'))
print('curated:', len(d['rows']))
print('source:', dict(Counter(r['existingStructureSource'] for r in d['rows'])))
print('Legion-check:', [r['address'] for r in d['rows'] if r.get('address','').startswith('243 Coxwell')])
"
```

Expected: 244 curated, source `{'permit': 165, 'osm': 65, 'vacant': 14}`, Legion-check empty list.

## Deploy commands (NOT executed)

```bash
sudo cp -p /home/josh/bloomto_work/index.html                /var/www/html/bloomto/index.html
sudo cp -p /home/josh/bloomto_work/data/parcels.geojson      /var/www/html/bloomto/data/parcels.geojson
sudo cp -p /home/josh/bloomto_work/data/parcels-top.json     /var/www/html/bloomto/data/parcels-top.json
sudo cp -p /home/josh/bloomto_work/data/parcels-broader.json /var/www/html/bloomto/data/parcels-broader.json
sudo cp -p /home/josh/bloomto_work/data/neighborhoods.json   /var/www/html/bloomto/data/neighborhoods.json
sudo chown -R john:www-data /var/www/html/bloomto/
```
