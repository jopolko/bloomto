# rootedto

**Toronto cultural commercial corridor map.** Legacy storefronts, heritage designation gaps, and the developer math (zoning unused density × land value) for ethnically rooted neighbourhoods at risk of displacement.

## Why

Toronto's heritage system protects Cabbagetown Victorians and Rosedale mansions with **288 designated properties in Kensington Market** — and just **3** in Little Jamaica, 1 in Corso Italia, 1 in Little India. Zero of the 12 ethnic commercial corridors sit inside a confirmed-designated Heritage Conservation District. The math of demolition is buried in zoning documents; the history of who's been there longest is buried in business licence dates. RootedTO puts both in one place, parcel by parcel.

## How

Python ETL → static JSON → vanilla HTML/JS map. Daily-refreshed CKAN business licences + heritage register + BIA polygons + zoning + 3D Massing, joined at the parcel level. StatCan Census DA demographics underpin corridor-level cultural attribution. No backend, no build step, no framework.

## Launch corridors (v1)

Eglinton W / Little Jamaica · Spadina/Dundas (West Chinatown) · Gerrard/Broadview (East Chinatown) · Danforth (Greektown) · College W (Little Italy) · Dundas W (Little Portugal) · Bloor (Koreatown) · Gerrard E (Little India) · Roncesvalles · St. Clair W (Corso Italia) · Kensington Market · Queen W (Parkdale)

## Inner-suburb communities (v2+)

Thorncliffe Park · Crescent Town · Albion · Markham/Lawrence · Eglinton/Brimley · Jane/Finch

## Status

- Pivoted from DemoCalcTO → RootedTO on 2026-05-12.
- Data layer validated across all v1 corridors. v1 spec in progress.
- Two prior codebases archived under `legacy/` (BloomTO, DemoCalcTO).

## Layout

```
rootedto/
├── tools/
│   ├── cache/                # gitignored local cache
│   └── sources/              # CKAN loaders
├── data/
│   ├── ckan_survey.md        # CSV/JSON catalogue survey (538 packages)
│   ├── ckan_survey_supplement.md  # non-CSV format catalogue
│   └── (etl outputs)         # mostly gitignored
├── CLAUDE.md
├── README.md
└── legacy/
    ├── bloomto/              # multiplex parcel filtering (archived)
    └── democalcto/           # demolition cost benchmarking pivot (archived, never shipped)
```

## Cultural attribution policy

Corridor-level only, always sourced. **No user-facing ethnicity submission form.** No surname-based ethnicity classifiers in public output. Every cultural claim on a parcel card cites its source (Census DA, partner-org directory, cuisine inference).

## License

TBD.
