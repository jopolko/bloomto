# CLAUDE.md

Guidance for Claude Code working in this repository.

## Project

**RootedTO** — a Toronto map of cultural commercial corridors, surfacing legacy storefronts, heritage designation gaps, and the "developer math" (zoning unused density vs. land value) for racialized commercial districts at risk of displacement.

**Pivoted to RootedTO 2026-05-12.** Two prior codebases are archived under `legacy/` (`bloomto/` — multiplex parcel filtering; the abandoned `democalcto/` pivot — demolition cost benchmarking, never shipped). Do not import from `legacy/`.

## Architecture

Same stack as predecessors: Python ETL → static JSON → Apache + vanilla HTML/JS. No backend, no DB, no build step, no React, no Node.

## Launch corridors (v1)

12 Toronto ethnic commercial corridors:
- Eglinton W / Little Jamaica · Spadina/Dundas / West Chinatown · Gerrard/Broadview / East Chinatown · Danforth / Greektown · College W / Little Italy · Dundas W / Little Portugal · Bloor / Koreatown · Gerrard E / Little India · Roncesvalles · St. Clair W / Corso Italia · Kensington Market · Queen W / Parkdale

Inner-suburb communities (v2+, residential tower / strip-mall mode):
- Thorncliffe Park · Crescent Town · Albion · Markham/Lawrence · Eglinton/Brimley · Jane/Finch

## Validated CKAN data layers

| Layer | Dataset | Refresh |
|---|---|---|
| Legacy storefronts + closure feed | `municipal-licensing-and-standards-business-licences-and-permits` | Daily |
| Heritage designations | `heritage-register` (zipped SHP, ~12.3k properties) | Quarterly |
| Heritage Conservation Districts | `heritage-conservation-districts` (32 polygons) | Quarterly |
| Corridor polygons | `business-improvement-areas` (86 BIAs) | Weekly |
| Pending pressure | `development-applications` + `committee-of-adjustment-applications` | Daily |
| Unused density math | `zoning-by-law` overlays + `3d-massing` | As-available |
| Tenant displacement (v3) | `apartment-building-registration` + `apartment-building-evaluation` | Monthly / Daily |
| Cultural attribution | StatCan Census 2021, DA-level (ethnic origin, mother tongue, place of birth) | — |
| Lobbyist activity (v4) | `lobbyist-registry` | Daily |

## NOT a usable cultural-asset source

- `cultural-hotspot-points-of-interest` — branded as a community asset map; on inspection it's a **City tourism walking-tour catalogue** (Art / History / Mural points). Zero POIs in 8 of 12 launch corridors. **Do not use.**

## Cultural attribution policy (non-negotiable)

- **Corridor-level attribution only** from sourced data (StatCan Census DA demographics, partner-org member lists, cuisine inference on business names).
- **No user-facing ethnicity submission form.** Do not ask Torontonians to fill in racial score cards.
- **No surname-based ethnicity classifiers in public output.** Internal analytics fine; never publish inferred ethnicity from a person's legal name.
- Every cultural label on a parcel card must be sourced ("per Census" / "per BBPA directory" / "cuisine inferred from name").

## Heritage asymmetry is a feature, not a bug

The Heritage Register designates ~288 properties in Kensington Market vs. **3** in Little Jamaica. Zero of the 12 ethnic commercial corridors sit inside a confirmed-designated Heritage Conservation District. Surface this asymmetry as a homepage metric — it IS the press hook.

## Survey reference

`data/ckan_survey.md` (CSV/JSON datasets, 556 KB, 312 ok) and `data/ckan_survey_supplement.md` (non-CSV formats: XLSX/XLS/ZIP/PDF/DOCX/KML/XML/SPSS/etc., 1.9 MB, 181 datasets) are the structural inventory of every Toronto CKAN dataset. **They verify file integrity only — not semantic fitness.** Always run a manual spot-check (pull the data, look at the actual values) before trusting a dataset for a new use case.

## Secrets

`/var/secrets/democalcto.env` (filename retained, not renamed alongside the project) holds `GITHUB_TOKEN` + `GOOGLE_API_KEY`. Never inline, never echo, never commit.

## Hosting

Apache on `joshuaopolko.com`. Prod folder at `/var/www/html/rootedto/`. `.htaccess` default-denies everything except the explicit allow-list. File ownership `john:www-data`.

## What NOT to do

- Don't import from `legacy/`.
- Don't use `cultural-hotspot-points-of-interest` as a community asset map.
- Don't add user-facing ethnicity tagging UI.
- Don't add a backend, DB, build step, or framework.
- Don't claim a business is ethnically X without a sourced attribution (Census DA / partner-org list / cuisine inference).
