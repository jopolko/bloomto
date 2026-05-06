# Requirements Document

## Introduction

The building-permits-source ETL pulls City of Toronto Building Permits via CKAN, filters to residential new-build / conversion permits, and joins them to BloomTO parcels. Output: per-parcel permit summary (`properties.permits`) and per-neighborhood permit comparison (`properties.neighborhoodPermitComp`) on the existing `data/parcels.geojson` wire format, plus coverage stats in `meta.permits`.

This is an **ETL-only spec** — it adds data fields to the wire format and the per-parcel pipeline. It does not add or change UI. A separate downstream "developer-attractiveness-score" spec will be the consumer that blends permit data into ranking; this spec ships the inputs.

## Alignment with Product Vision

CLAUDE.md frames v1.2 as "a parcel-level Multiplex Readiness view for developers — every Toronto parcel scored against as-of-right zoning, Heritage Register status, and 500m subway/streetcar transit buffer." The Multiplex Readiness score answers "is this lot legally viable?"; recent permit activity answers "are people actually building here, and at what declared cost?" Adding permits to the per-parcel record gives the downstream attractiveness score a market-velocity signal that the as-of-right gates alone cannot provide.

The spec also honors the steering constraint that the workstation ETL may consume rich source data (3D massing, etc.) provided the browser receives only flat 2D + numeric properties. Permits are pure tabular data; nothing about this spec touches the browser-side rendering path.

## Requirements

### Requirement 1: Source Loader

**User Story:** As an ETL maintainer, I want a `tools/sources/building_permits.py` module that pulls and caches Toronto Building Permits from CKAN, so that the build pipeline can read permits the same way it reads heritage records and zoning.

#### Acceptance Criteria

1. WHEN `compute_permits(cache_dir)` is called THEN the loader SHALL ensure the source CSV/JSON is present in `tools/cache/` (download once, reuse on subsequent runs) using the existing `_ensure_cached` pattern from `tools/sources/heritage.py`.
2. WHEN the cached file is read THEN the loader SHALL stream-parse it (no full-file load into memory) following the `ijson` pattern used by `tools/sources/zoning.py:iter_parcels`.
3. WHEN a permit row is encountered THEN the loader SHALL parse it into a `BuildingPermit` dataclass with normalized fields: `permit_id`, `address` (raw), `lat`, `lng` (nullable — spatial-join fallback only fires when present), `permit_type`, `description`, `declared_value_cad` (number), `issued_date` (date), `unit_count` (nullable int), `floor_area_m2` (nullable number).
4. IF the upstream record is missing a required field (id, address, type, value, date) THEN the loader SHALL skip it and increment a per-row reason counter (`skipped_missing_field` / `skipped_bad_geom` / `skipped_bad_value` / `skipped_bad_date`).
5. WHEN the loader finishes THEN it SHALL log a one-line summary `permits: <total> rows seen, <kept> kept (skipped: <reason_breakdown>)` mirroring the format already used by `tools/sources/building_outlines.py`.

### Requirement 2: Residential New-Build / Conversion Filter

**User Story:** As an ETL maintainer, I want only permits relevant to multiplex development (residential new builds and dwelling conversions) to flow through to scoring, so that renovation noise (windows, decks, kitchens) does not pollute the per-neighborhood comparison.

#### Acceptance Criteria

1. WHEN a permit row is parsed THEN the loader SHALL classify it via a closed-set lookup against the upstream `permit_type` and `description` fields.
2. WHEN the classifier returns a category in `{"new_residential", "conversion", "addition_with_units"}` THEN the loader SHALL keep the row.
3. WHEN the classifier returns a category in `{"renovation", "demolition_only", "non_residential", "interior_alteration"}` THEN the loader SHALL drop the row and increment `skipped_non_residential_construction`.
4. WHEN the classifier encounters an `permit_type` value not in the closed-set table THEN the loader SHALL: (a) drop the row, (b) increment `skipped_unclassified_type`, (c) emit a single deduplicated `WARN` log line per unseen value, and (d) raise `ClassifierDriftError` if `skipped_unclassified_type` exceeds `MAX_UNCLASSIFIED = 1000` rows in a single run (this catches upstream schema drift loudly rather than silently dropping a category that should have been kept).
5. WHEN the closed-set classifier table is updated THEN it SHALL be the single tuning surface (mirroring how `tools/sources/zoning.py` exposes `zoning_multipliers.json` as the only knob for zone-class mapping).

### Requirement 3: Sanity Ceiling and Freshness Window

**User Story:** As a downstream consumer of `meta.permits`, I want the per-parcel and per-neighborhood aggregates to exclude obvious outliers (decimal-typo dollar values) and stale activity, so that the medians and totals are defensible without manual cleanup.

#### Acceptance Criteria

1. WHEN a kept permit has `declared_value_cad > SANITY_VALUE_CEILING_CAD` THEN the loader SHALL drop it and increment `skipped_outlier_value`. The ceiling SHALL default to 50 million CAD per single permit. Calibration rationale: typical Toronto multiplex new-build declared values fall under 5 M (empirical, based on a sample of post-2023 permits in the cached pull); the 10× headroom is sized to catch missed-decimal typos (e.g. $1.2M typed as $12M is suspicious; $120M is conclusively wrong) without rejecting legitimate large condo or apartment-building permits in mid-rise neighborhoods.
2. WHEN a kept permit has `declared_value_cad <= 0` THEN the loader SHALL drop it and increment `skipped_bad_value`.
3. WHEN aggregating into `recentCount` / `recentValueTotal` / `recentMostRecentDate` per parcel THEN the loader SHALL only count permits with `issued_date` within the freshness window. The window SHALL default to 5 years (configurable via `--permit-freshness-years` CLI flag and a module constant `DEFAULT_FRESHNESS_YEARS = 5`).
4. WHEN a permit is older than the freshness window THEN it SHALL be excluded from per-parcel aggregates AND excluded from the per-neighborhood `medianCostPerM2` denominator.

### Requirement 4: Address-Join (Primary)

**User Story:** As an ETL maintainer, I want permits matched to parcels by normalized address first, so that the high-precision deterministic join handles the bulk of records and the spatial fallback only runs on the residual.

#### Acceptance Criteria

1. WHEN `compute_permits` builds its index THEN it SHALL produce a `PermitIndex` dataclass with: `address_to_indices: dict[str, list[int]]` (normalized address → list of permit row indices), `permits: list[BuildingPermit]` (the kept records, indexable by row), `spatial_tree: STRtree` of permit centroids, AND `claimed: set[int]` (the set of permit row indices already claimed by a prior parcel, mutated by both join phases — single source of truth for claim-once across address-join and spatial fallback).
2. WHEN a permit address is normalized THEN it SHALL import `normalize_address` from a shared module `tools/sources/_address.py` (refactored out of `heritage.py` as part of this spec — see design Component 1). Both heritage and permits SHALL import from the shared module so a future heritage-specific tweak cannot silently change permit join behavior, and vice versa.
3. WHEN multiple permits share the same normalized address THEN ALL of them SHALL be indexed under that key (1-parcel-to-N-permits is the common case for an active redevelopment lot).
4. WHEN `build_parcels.py` resolves permits for a parcel THEN it SHALL look up `parcel_normalized_address` in `permit_index.address_to_indices` first, mark all matched permits as `claimed`, and return the list of matched permit indices.
5. WHEN the address-join hits for a parcel THEN the spatial fallback SHALL still run for that parcel, but it SHALL only claim STRtree-contained permits that the address-join did NOT already claim (the `claimed: set[int]` is the single source of truth across both phases). This makes `denominatorSource = "mixed"` reachable per Req 6.5 (when a parcel legitimately has one permit that address-joined and a separate permit that only had a centroid). Address-join hits are still authoritative for any individual permit — once claimed by address-join, the spatial fallback cannot re-claim that permit for the same or another parcel.

### Requirement 5: Spatial-Join Fallback

**User Story:** As an ETL maintainer, I want a point-in-parcel spatial fallback for permits whose address didn't normalize to anything a parcel carries, so that the join doesn't silently lose 5–15% of permits to address-quality noise.

#### Acceptance Criteria

1. WHEN a permit row carries valid `lat` AND `lng` THEN the loader SHALL include it in a `STRtree` of permit centroids (as Shapely points).
2. WHEN `build_parcels.py` finishes the address-join for a parcel and the address-join missed (returned no permits) THEN it SHALL query the spatial tree for points within the parcel polygon.
3. WHEN a candidate point is contained by the parcel polygon AND its permit index has not been claimed by an earlier address-join THEN the loader SHALL claim it for this parcel.
4. WHEN a candidate point falls inside two adjacent parcels' boundaries (rare; geocoding noise on shared lot lines) THEN the first parcel processed SHALL claim it (deterministic via parcel-iteration order; the streaming order is stable across runs).
5. WHEN the spatial-join phase ends THEN the loader SHALL log how many permits were claimed via address-join vs spatial fallback vs left unjoined (`permits joined: <addr> by address, <spatial> by spatial fallback, <unjoined> unjoined`).

### Requirement 6: Per-Parcel `permits` Wire Field

**User Story:** As a wire-format consumer, I want each parcel's recent permit activity surfaced as a single nested object so I can render or score it without re-joining the source CSV in the browser.

#### Acceptance Criteria

1. WHEN `build_parcels.py` writes a parcel's properties THEN it SHALL include a `permits` key with the shape `{ recentCount: int, recentValueTotal: int, recentMostRecentDate: string | null, denominatorSource: string }`.
2. WHEN no permits joined to the parcel within the freshness window THEN `permits` SHALL be `{ recentCount: 0, recentValueTotal: 0, recentMostRecentDate: null, denominatorSource: "no_joined_permits" }` (always-present object, never `null`, so the validator can enforce key presence).
3. WHEN a permit was joined via address-only THEN `denominatorSource` SHALL be `"address_join"`.
4. WHEN a permit was joined via spatial fallback only THEN `denominatorSource` SHALL be `"spatial_fallback"`.
5. WHEN both methods contributed permits to the parcel THEN `denominatorSource` SHALL be `"mixed"`.
6. WHEN `recentMostRecentDate` is non-null THEN it SHALL be an ISO 8601 date string `YYYY-MM-DD`.
7. WHEN `recentValueTotal` is computed THEN it SHALL be the sum of `declared_value_cad` across all in-window joined permits, in whole CAD (rounded to int — sub-dollar precision is noise from the upstream).

### Requirement 7: Per-Neighborhood `neighborhoodPermitComp` Wire Field

**User Story:** As a wire-format consumer, I want each parcel to carry the neighborhood-level median cost-per-m² and sample size so a downstream developer-attractiveness ranking can normalize a single parcel's permit value against its neighborhood without re-aggregating.

#### Acceptance Criteria

1. WHEN `build_parcels.py` writes a parcel's properties THEN it SHALL include a `neighborhoodPermitComp` key with the shape `{ medianCostPerM2: number | null, sampleSize: int, freshnessYears: int }`.
2. WHEN the neighborhood's in-window kept permits with `floor_area_m2 > 0` number fewer than `MIN_NEIGHBORHOOD_SAMPLE_SIZE` (default 10) THEN `medianCostPerM2` SHALL be `null` and `sampleSize` SHALL be the actual count (transparency over false precision).
3. WHEN the sample size is at or above the floor THEN `medianCostPerM2` SHALL be the median of `declared_value_cad / floor_area_m2` across the neighborhood's in-window kept permits, in CAD per square meter, rounded to the nearest integer CAD.
4. WHEN a permit is missing or has zero `floor_area_m2` THEN it SHALL be excluded from the median denominator but still counted in `recentCount` for its parcel's `permits` field (unit count is the price-per-m² gate, not the count gate).
4a. WHEN computing the per-record `declared_value_cad / floor_area_m2` ratio for the median THEN no per-m² sanity band SHALL be applied — the median is statistically robust to per-record outliers (a single $50,000/m² typo cannot pull the median far if the sample size is ≥10), so adding a band would only mask schema drift we want to surface via Req 2.4.
5. WHEN `freshnessYears` is reported THEN it SHALL be the configured freshness window value (5 by default), so a downstream consumer can label "median construction cost over the past N years" without inferring N.

### Requirement 8: Honest Framing on the Wire

**User Story:** As a downstream consumer or end user, I want the permit values labeled as declared construction cost rather than market value or assessed value, so I cannot accidentally treat them as comparable to property prices.

#### Acceptance Criteria

1. WHEN `meta` is assembled THEN it SHALL include a `permits` block with `denominatorLabel = "declared_construction_cost_cad"` (machine-readable framing).
2. WHEN `meta.permits` is assembled THEN it SHALL include a human-readable `notes` field with the verbatim text: `"Permit values are the declared construction cost on the building permit application. They are NOT market sale prices, assessed values, or final build costs."`
3. WHEN the wire-format validator runs THEN it SHALL reject payloads where `meta.permits.denominatorLabel` is missing or set to anything other than the canonical string.
4. WHEN the spec adds new permit-related labels in the future (e.g. denominatorVersion) THEN they SHALL be additive — never repurpose the existing label keys.

### Requirement 9: Coverage Stats in `meta.permits`

**User Story:** As a downstream consumer, I want explicit coverage statistics so I can decide how much to trust the per-neighborhood numbers without re-running the ETL.

#### Acceptance Criteria

1. WHEN `meta.permits` is written THEN it SHALL include the following keys with these types: `totalPermitsKept: int`, `joinedByAddress: int`, `joinedBySpatialFallback: int`, `unjoined: int`, `freshnessYears: int`, `sanityCeilingCad: int`, `minNeighborhoodSampleSize: int`, `denominatorLabel: string` (canonical value `"declared_construction_cost_cad"`), `notes: string`.
2. WHEN any of these keys is missing THEN `parcel_io.validate` SHALL raise a `ValueError` (loud-failure pattern; matches how `meta.shadowAnalysis` keys are validated).
3. WHEN `joinedByAddress + joinedBySpatialFallback + unjoined != totalPermitsKept` THEN validation SHALL fail (consistency invariant — every kept permit accounted for).

### Requirement 10: Test Coverage at Every Join Layer

**User Story:** As a maintainer, I want unit tests for the source loader, address-join, spatial fallback, aggregation, and validator additions, so that a future refactor cannot silently regress the join precision.

#### Acceptance Criteria

1. WHEN the test suite runs THEN there SHALL be a `tools/tests/test_building_permits.py` covering: classifier (kept vs dropped vs unclassified), `ClassifierDriftError` raised at the threshold (Req 2.4), sanity ceiling, freshness window, address-join hit/miss, spatial fallback claim, claim-once exclusion (a permit with a valid lat/lng AND an address that hits the address-join must NOT also be claimed by the spatial fallback for a different parcel), neighborhood aggregation below/at/above sample-size floor, summary log format.
2. WHEN the test suite runs THEN `tools/tests/test_parcel_io.py` SHALL cover the new `permits` and `neighborhoodPermitComp` feature properties AND the `meta.permits` keys (presence + label invariant).
3. WHEN the test suite runs THEN `tools/tests/test_e2e_parcels.py` SHALL gain at least one fixture that exercises the permits join end-to-end (one address-join hit, one spatial-fallback hit, one unjoined permit).
4. WHEN any test fixture is created THEN it SHALL NOT call out to the live Toronto CKAN API (tests use synthetic permits the same way `test_heritage.py` uses synthetic heritage records).

## Non-Functional Requirements

### Performance

- **Address-join must be O(1) per parcel.** The reverse index (`address_to_indices`) is pre-built once at load time; the per-parcel lookup is a single dict access. Mirror the heritage join's profile (currently <1s for 135k parcels).
- **Spatial fallback must use STRtree.** Total spatial-fallback cost should not exceed the existing heritage spatial fallback's runtime budget (<10s on the full parcel pass).
- **Wall-clock budget.** The combined permit-source phase (load + classify + index build) SHALL complete in under 2× the heritage-source phase's wall-clock on the same parcel set. The per-parcel join cost (address-join + spatial-fallback combined) SHALL not exceed the per-parcel heritage-join cost by more than 50%.
- **No expansion of the cold-cache footprint by more than 100 MB.** The Building Permits dataset is ~50–80 MB compressed; cache it gzipped if that pushes us over.

### Reliability

- **Loud failure on schema drift.** If CKAN renames or removes a required field, the loader must raise — never silently substitute None and skip.
- **Atomic writes.** No change to the existing atomic-write pattern in `parcel_io.py`. The new fields ride that pattern.
- **Deterministic across runs given the same cache.** Sorted order by permit_id where ambiguity exists in spatial-join attribution.

### Security / Privacy

- **No PII in logs.** Permit applicants and contractor names exist in the source but must not be carried into `BuildingPermit` records or logged. Only the public-facing fields (id, address, type, declared value, date, geometry, floor_area, unit_count) cross into the in-memory model.
- **`description` is classifier-only.** The free-text `description` field on `BuildingPermit` is consumed by the classifier (Req 2.1) and never written to logs, the wire format, or any test fixture beyond synthetic strings — Toronto's permit description field can leak contractor or applicant names that the structured PII fields above don't.

### Maintainability

- **Single classifier table.** The kept-vs-dropped category mapping is one closed-set dict, exported from `tools/sources/building_permits.py` (mirrors the `_HERITAGE_FACTORS` lookup pattern).
- **No new PHP files.** No browser-loaded heavyweight libs. No 3D in browser. Compliance with CLAUDE.md's still-retired list is unconditional.
- **No new top-level dependencies** beyond what `tools/requirements.txt` already provides (`requests`, `shapely`, `pyproj`, `ijson`). Permit data is plain CSV/JSON; the existing toolbelt suffices.
