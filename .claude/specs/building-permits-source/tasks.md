# Implementation Plan

## Task Overview

The work splits into eleven phases, each a sequence of atomic 15–30 min tasks: A) refactor the shared address normalizer out of `heritage.py`; B) scaffold the new `building_permits.py` source loader; C) add the filter pipeline (classifier, sanity ceiling, freshness window); D) test the loader; E) add orchestrator helpers in `build_parcels.py`; F) wire those helpers into the existing parcel loop; G) populate `meta.permits`; H) test the orchestrator helpers; I) extend the wire-format validator; J) extend the slim top-N projection; K) end-to-end fixture + docs update. Phases are roughly sequential — each phase's tests close out before the next phase starts so a regression surfaces immediately.

## Steering Document Compliance

CLAUDE.md is the steering source (no `.claude/steering/*.md` exists). Every task respects:
- **No new PHP files.** All work is Python in `tools/`.
- **No browser-side libraries, no Deck.gl, no 3D-in-browser.** This spec is ETL-only.
- **No new top-level dependencies.** `requests`, `shapely`, `pyproj`, `ijson` already in `tools/requirements.txt` cover everything.
- **`tools/sources/` convention.** Each source-loader task slots in alongside `heritage.py`, `zoning.py`, etc.
- **Loud-failure pattern.** Schema drift surfaces as `ClassifierDriftError`; bad inputs surface as counters; validator regressions block atomic write.

## Atomic Task Requirements
- File scope: 1–3 files per task.
- Time-boxed: 15–30 min per task for an experienced developer.
- Single purpose: one testable outcome per task.
- Each task names exact files to create or modify and references the requirement IDs and existing code it leverages.

## Tasks

### Phase A — Shared address normalizer (refactor)

- [ ] 1. Create `tools/sources/_address.py` with the lifted normalizer
  - File: `tools/sources/_address.py` (new)
  - Move byte-identical from `tools/sources/heritage.py`: the closed-set comment header for `STREET_TYPE_ABBREVIATIONS` (currently around lines 97–102), the `STREET_TYPE_ABBREVIATIONS` dict itself (lines ~103–118), and the `normalize_address` function with its docstring (lines ~121–136). Module docstring should state the cross-source-normalizer purpose and the no-regex constraint.
  - DO NOT move `KNOWN_STATUSES`, `STATUS_PRECEDENCE`, or `more_restrictive` — those stay in `heritage.py` (heritage-specific).
  - Purpose: single source of truth for address normalization across heritage + permits.
  - _Leverage: tools/sources/heritage.py (closed-set header through end of normalize_address)_
  - _Requirements: 4.2_

- [ ] 2. Update `tools/sources/heritage.py` to import from `_address`
  - File: `tools/sources/heritage.py`
  - Replace the inline `STREET_TYPE_ABBREVIATIONS` dict and `normalize_address` function with `from ._address import normalize_address, STREET_TYPE_ABBREVIATIONS`.
  - All other heritage code unchanged.
  - Purpose: heritage now consumes the shared module so a permits-side change to abbreviations cannot diverge.
  - _Leverage: tools/sources/_address.py_
  - _Requirements: 4.2_

- [ ] 3. Add `tools/tests/test_address.py` covering the relocated normalizer
  - File: `tools/tests/test_address.py` (new)
  - Two minimum tests: (a) `normalize_address` uppercases + abbreviates a token in `STREET_TYPE_ABBREVIATIONS` (e.g. `"123 King Street"` → `"123 KING ST"`); (b) tokens outside the abbreviation set pass through unchanged after uppercasing (e.g. `"55 Mews"` → `"55 MEWS"`).
  - Run `python -m unittest tools.tests.test_heritage` after this task and confirm it still passes (existing suite is the regression check that the move was byte-equivalent).
  - Purpose: prove the refactor preserved behavior.
  - _Leverage: tools/tests/test_heritage.py (existing normalize_address coverage as reference)_
  - _Requirements: 4.2, 10.1_

### Phase B — Source loader skeleton

- [ ] 4. Create `tools/sources/building_permits.py` with module skeleton
  - **Pre-flight (do this first):** Inspect the cached file at `tools/cache/building_permits.json` if present, OR run `curl 'https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action/package_show?id=building-permits-active-permits' | jq '.result.resources[] | {format,url,id,name}'` to confirm the canonical resource_id, format (CSV vs JSON), and field names (especially `permit_type`, `description`, declared-value field, issued-date field, and the lat/lng fields). The placeholder `RESOURCE_URL` and field-name strings in this task and tasks 5/7 are filled in from this output.
  - File: `tools/sources/building_permits.py` (new)
  - Module docstring describing the loader's role + privacy-of-`description` invariant. Imports: `csv`, `json`, `ijson`, `logging`, `dataclasses`, `datetime.date`, `pathlib.Path`, `typing.NamedTuple`, `shapely.geometry.Point`, `shapely.strtree.STRtree`, `from . import _http`, `from ._address import normalize_address`.
  - Constants: `CACHE_FILENAME = "building_permits.json"`, `RESOURCE_URL = "<placeholder — to be filled with the CKAN URL during implementation>"`, `DEFAULT_FRESHNESS_YEARS = 5`, `SANITY_VALUE_CEILING_CAD = 50_000_000`, `MAX_UNCLASSIFIED = 1000`, `MIN_NEIGHBORHOOD_SAMPLE_SIZE = 10`.
  - Frozen `@dataclass(frozen=True)` `BuildingPermit` with fields per design (permit_id, address, lat, lng, permit_type, description, declared_value_cad, issued_date, unit_count, floor_area_m2).
  - `class PermitIndex(NamedTuple)` per design (permits, address_to_indices, spatial_tree, centroids, claimed).
  - `class ClassifierDriftError(RuntimeError): pass`.
  - No function bodies yet beyond signatures + `pass`.
  - Purpose: skeleton other phases hang code on.
  - _Leverage: tools/sources/heritage.py (HeritageIndex shape, _http import pattern)_
  - _Requirements: 1.1, 1.3, 5.1_

- [ ] 5. Add classifier table + `classify(permit_type, description)` to `building_permits.py`
  - File: `tools/sources/building_permits.py`
  - Add `KEPT_CATEGORIES: frozenset[str] = frozenset({"new_residential", "conversion", "addition_with_units"})`.
  - Add `PERMIT_CATEGORY_TABLE: dict[str, str]` mapping uppercased upstream `permit_type` strings → category. Seed with the categories from Req 2.2/2.3 (placeholders fine; the actual upstream-string vocabulary will be filled in during implementation against the cached CKAN dump).
  - Add `_DESCRIPTION_KEYWORDS: list[tuple[str, str]]` — ordered substring-match disambiguation rules consulted only for ambiguous coarse buckets (e.g. an "addition" permit type refines to `"addition_with_units"` if description contains "secondary suite" or "new dwelling unit"; otherwise it falls back to `"renovation"`).
  - Add `def classify(permit_type: str, description: str) -> str | None:` returning the category string or `None` for unmatched types. No regex.
  - Purpose: single closed-set tuning surface for keep-vs-drop classification.
  - _Leverage: existing closed-set lookup pattern in tools/parcel_scoring.py:_HERITAGE_FACTORS_
  - _Requirements: 2.1, 2.2, 2.3, 2.5_

- [ ] 6. Add `_ensure_cached(cache_dir)` to `building_permits.py`
  - File: `tools/sources/building_permits.py`
  - Implement `_ensure_cached(cache_dir: Path) -> Path` mirroring `tools/sources/heritage.py:_ensure_cached`: ensure dir exists, return cached path if non-empty, else `_http.download_with_retries(RESOURCE_URL, cached)` and return.
  - Purpose: one-shot cache pattern so workstation re-runs are free.
  - _Leverage: tools/sources/heritage.py:_ensure_cached, tools/sources/_http.py:download_with_retries_
  - _Requirements: 1.1_

- [ ] 7. Add `_iter_records(cache_path)` stream-parser to `building_permits.py`
  - File: `tools/sources/building_permits.py`
  - Detect format from suffix (`.json` → use `ijson.items(fp, "result.records.item")`; `.csv` → `csv.DictReader`). Yield raw record dicts — no parsing, no filtering. The yielding step strips known PII fields (applicant name, contractor name) before the dict is yielded so downstream code cannot accidentally consume them.
  - Purpose: stream-parse without loading the whole dataset into memory; PII never reaches the in-memory model.
  - _Leverage: tools/sources/zoning.py:iter_parcels (ijson streaming)_
  - _Requirements: 1.2, NFR Privacy_

### Phase C — Filter pipeline

- [ ] 8. Add per-row filter pipeline to `building_permits.py`
  - File: `tools/sources/building_permits.py`
  - Add internal helper `_parse_and_filter(raw, counters, seen_unknown_types) -> BuildingPermit | None`. Steps in order: (1) classify via `classify(...)`; (2) if `None` → `counters["skipped_unclassified_type"] += 1`, emit one-shot WARN per unique unseen `permit_type` (using `seen_unknown_types: set[str]` for dedup), return None; (3) if not in `KEPT_CATEGORIES` → `counters["skipped_non_residential_construction"] += 1`, return None; (4) parse and validate `declared_value_cad`, `issued_date`; per-failure counter; (5) sanity-ceiling check; (6) build `BuildingPermit` (geometry parsed; lat/lng `None` if upstream geom missing or out of WGS84 bounds, with `counters["skipped_bad_geom"]` incremented only when geom was claimed-present but unparseable).
  - Purpose: turn raw records into `BuildingPermit` instances or counted skips, in one place.
  - _Leverage: tools/sources/heritage.py classification flow_
  - _Requirements: 1.3, 1.4, 2.2, 2.3, 2.4, 3.1, 3.2_

- [ ] 9. Implement `compute_permits(cache_dir, freshness_years, sanity_ceiling_cad)` in `building_permits.py`
  - File: `tools/sources/building_permits.py`
  - Orchestrate: `_ensure_cached` → iterate `_iter_records` → `_parse_and_filter` → for kept permits, append to `permits: list[BuildingPermit]`, `centroids: list[Point | None]` (with None entries when geom missing), and update `address_to_indices: dict[str, list[int]]` keyed on `normalize_address(raw_address)`.
  - After loop: build `STRtree` over the non-None centroids. Construct `PermitIndex(permits, address_to_indices, spatial_tree, centroids, claimed=set())` and return.
  - Purpose: the source-loader factory function consumed by the orchestrator.
  - _Leverage: tools/sources/heritage.py:compute_heritage (exact orchestration shape)_
  - _Requirements: 1.1, 4.1, 5.1_

- [ ] 10. Add INFO summary log + drift-threshold raise in `compute_permits`
  - File: `tools/sources/building_permits.py`
  - At the end of `compute_permits`, log a single INFO line: `"permits: %d rows seen, %d kept (skipped: missing_field=%d non_residential=%d unclassified=%d outlier=%d bad_value=%d bad_date=%d bad_geom=%d)"` using the counters dict.
  - Before returning, if `counters["skipped_unclassified_type"] > MAX_UNCLASSIFIED`, raise `ClassifierDriftError(f"upstream permit_type vocabulary may have shifted; review tools/sources/building_permits.py:PERMIT_CATEGORY_TABLE — {counters['skipped_unclassified_type']} unclassified rows")`.
  - Purpose: visibility into kept-vs-dropped breakdown + loud-failure on schema drift.
  - _Leverage: tools/sources/building_outlines.py log line format_
  - _Requirements: 1.5, 2.4_

### Phase D — Source loader tests

- [ ] 11. Add `tools/tests/test_building_permits.py` Part 1: classifier coverage
  - File: `tools/tests/test_building_permits.py` (new)
  - Tests: each KEPT_CATEGORIES bucket keeps at least one example row; each dropped category increments the right counter; ambiguous-type rows refine via `_DESCRIPTION_KEYWORDS` correctly; unknown `permit_type` increments `skipped_unclassified_type`, emits WARN-once (assert with `assertLogs`), and raises `ClassifierDriftError` when the count exceeds `MAX_UNCLASSIFIED` (use a temporary monkey-patched threshold of e.g. 3 for the test).
  - Purpose: lock the keep/drop contract and the drift-detection threshold.
  - _Leverage: tools/tests/test_heritage.py classification test pattern_
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 10.1_

- [ ] 12. Add `tools/tests/test_building_permits.py` Part 2: sanity ceiling + freshness + bad-input drops
  - File: `tools/tests/test_building_permits.py` (extend)
  - Tests: declared_value at exactly the ceiling kept, `ceiling+1` dropped to `skipped_outlier_value`; declared_value `0` and negative dropped to `skipped_bad_value`; date precisely at the freshness cutoff kept by the *aggregator* (note: the source loader keeps regardless of freshness; freshness gates aggregation, not load — separate phase); unparseable date dropped to `skipped_bad_date`; missing required field dropped to `skipped_missing_field`; out-of-bounds lat/lng increments `skipped_bad_geom` but the permit is still kept (with `lat=lng=None`).
  - Purpose: lock per-record drop semantics.
  - _Requirements: 1.4, 3.1, 3.2, 10.1_

- [ ] 13. Add `tools/tests/test_building_permits.py` Part 3: PermitIndex shape
  - File: `tools/tests/test_building_permits.py` (extend)
  - Tests: synthetic 5-permit CSV → `compute_permits` returns a `PermitIndex` whose `permits` list has 5 entries; `address_to_indices` has the right keys (normalized) and values (lists of indices including the multi-permits-per-address case); `centroids[i]` is `None` when the i-th permit had missing geom and a `Point` otherwise; `spatial_tree.query(...)` returns the right candidate set for a known polygon; `claimed` is initially an empty set; `claimed.add(0); claimed.add(2)` works (mutability).
  - Purpose: lock the PermitIndex contract that the orchestrator depends on.
  - _Requirements: 4.1, 5.1, 10.1_

### Phase E — Orchestrator helpers

- [ ] 14. Add `_resolve_permits` to `tools/build_parcels.py`
  - File: `tools/build_parcels.py`
  - Function `_resolve_permits(parcel, permit_index) -> tuple[list[int], str]`. Phase 1 (address-join): compute `parcel_norm = _address.normalize_address(parcel.address or "")`; `idxs = permit_index.address_to_indices.get(parcel_norm, [])`; for each idx, if `idx not in permit_index.claimed`, add to `claimed` and append to `addr_hits`. Phase 2 (spatial fallback, runs unconditionally per the amended Req 4.5): `cand = permit_index.spatial_tree.query(parcel.geometry)`; for each candidate centroid index that maps back to a permit index `idx not in permit_index.claimed` AND `parcel.geometry.contains(permit_index.centroids[idx])`, add to `claimed` and append to `spat_hits`. Compute `denom_source` from `(len(addr_hits) > 0, len(spat_hits) > 0)` per design's four-way mapping (`address_join` / `spatial_fallback` / `mixed` / `no_joined_permits`). Return `(addr_hits + spat_hits, denom_source)`.
  - Mirrors `_resolve_heritage_status`'s normalize-then-lookup pattern (heritage at line ~151 calls `heritage_src.normalize_address(parcel.address or "")` inline — no `parcel.normalized_address` attribute exists, and we don't add one).
  - Purpose: the per-parcel join driver.
  - _Leverage: tools/build_parcels.py:_resolve_heritage_status (lines ~127–166)_
  - _Requirements: 4.4, 4.5, 5.2, 5.3, 5.4, 6.3, 6.4, 6.5_

- [ ] 15. Add `_aggregate_parcel_permits` to `tools/build_parcels.py`
  - File: `tools/build_parcels.py`
  - Function `_aggregate_parcel_permits(indices, permits, freshness_cutoff_date, denom_source) -> dict`. Filter `indices` to `[i for i in indices if permits[i].issued_date >= freshness_cutoff_date]`. Compute `recentCount = len(filtered)`, `recentValueTotal = int(sum(permits[i].declared_value_cad for i in filtered))`, `recentMostRecentDate = max(...).isoformat() if filtered else None`. If `recentCount == 0`, force `denominatorSource = "no_joined_permits"`; else use the passed `denom_source`. Return the four-key dict.
  - Purpose: per-parcel aggregation with the recent-count consequent.
  - _Requirements: 6.1, 6.2, 6.6, 6.7_

- [ ] 16. Add `_compute_neighborhood_perm_comp` to `tools/build_parcels.py`
  - File: `tools/build_parcels.py`
  - Function `_compute_neighborhood_perm_comp(claims_by_neighborhood, permits, freshness_cutoff_date, freshness_years, min_sample_size) -> dict[str, dict]`. For each neighborhood: filter joined indices to those with `issued_date >= freshness_cutoff_date` AND `floor_area_m2 > 0`; compute `sampleSize = len(filtered)`; if `sampleSize < min_sample_size`, set `medianCostPerM2 = None`; else compute `int(round(statistics.median(p.declared_value_cad / p.floor_area_m2 for p in filtered)))`. Always include `freshnessYears`.
  - No per-record per-m² sanity band (Req 7.4a — median is robust; a band would mask classifier-drift bugs).
  - Note: the Req 5.5 join-phase summary log (`"permits joined: …"`) does NOT live in this function — it lives in task 19 (post-loop, before the second-pass stamping) where `addr_total`/`spat_total`/`unjoined` are already computed at the orchestrator level.
  - Purpose: per-neighborhood median.
  - _Leverage: statistics.median (stdlib)_
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.4a, 7.5_

### Phase F — Orchestrator wiring

- [ ] 17. Wire `compute_permits` invocation into `build_parcels.py:main`
  - File: `tools/build_parcels.py`
  - Inside `main()` near the existing `compute_heritage(cache)` call, add a `_stage("compute permits")` / `_done(...)` block that calls `compute_permits(cache_dir, freshness_years=args.permit_freshness_years, sanity_ceiling_cad=SANITY_VALUE_CEILING_CAD)` and stores the result as `permit_index`. Compute `freshness_cutoff_date` as `date.today().replace(year=date.today().year - args.permit_freshness_years)`; on `ValueError` (today is Feb 29 and the cutoff year is non-leap), fall back to `date(date.today().year - args.permit_freshness_years, 3, 1)` (advance to March 1 of the cutoff year — the only correct fallback when subtracting years from Feb 29).
  - Add CLI flag `--permit-freshness-years`, type=int, default=`DEFAULT_FRESHNESS_YEARS` (re-exported from `building_permits` for the import).
  - Purpose: source the index + freshness window once per run.
  - _Leverage: existing _stage/_done helpers and CLI argparse pattern_
  - _Requirements: 3.3_

- [ ] 18. Add per-parcel stash inside the existing parcel loop in `build_parcels.py`
  - File: `tools/build_parcels.py`
  - Before the parcel loop, init `parcel_permits_by_feat_idx: dict[int, dict] = {}` and `claims_by_neighborhood: dict[str, list[int]] = {}` plus per-source counters `addr_total = 0`, `spat_total = 0`. Inside the loop, after `feature` is constructed but before `features.append(feature)`: call `permit_indices, denom_source = _resolve_permits(parcel, permit_index)`; tally `addr_total` / `spat_total` from `denom_source`; call `parcel_permits = _aggregate_parcel_permits(permit_indices, permit_index.permits, freshness_cutoff_date, denom_source)`; set `feat_idx = len(features)`; record `parcel_permits_by_feat_idx[feat_idx] = parcel_permits`; extend `claims_by_neighborhood.setdefault(nb.name, []).extend(permit_indices)`.
  - Do NOT yet set `feature["properties"]["permits"]` — that happens in the next task.
  - Purpose: gather everything the second pass needs without changing the existing loop semantics.
  - _Requirements: 4.4, 5.2, 6.1, 7.1_

- [ ] 19. Add post-loop second pass to stamp `permits` and `neighborhoodPermitComp`
  - File: `tools/build_parcels.py`
  - After the parcel loop closes: compute `unjoined = len(permit_index.permits) - len(permit_index.claimed)`. Emit the Req 5.5 summary log line at the orchestrator level: `_log.info("permits joined: %d by address, %d by spatial fallback, %d unjoined", addr_total, spat_total, unjoined)`. Then call `nb_comp = _compute_neighborhood_perm_comp(claims_by_neighborhood, permit_index.permits, freshness_cutoff_date, args.permit_freshness_years, MIN_NEIGHBORHOOD_SAMPLE_SIZE)`. Then iterate `for feat_idx, feat in enumerate(features): feat["properties"]["permits"] = parcel_permits_by_feat_idx[feat_idx]; feat["properties"]["neighborhoodPermitComp"] = nb_comp[feat["properties"]["neighborhood"]]`.
  - Purpose: stamp both new wire fields onto every emitted feature, plus emit the join-phase summary.
  - _Requirements: 5.5, 6.1, 6.2, 7.1, 7.2_

### Phase G — `meta.permits` coverage stats

- [ ] 20. Add `meta.permits` block to the payload in `build_parcels.py`
  - File: `tools/build_parcels.py`
  - In the `meta = {...}` payload assembly (already exists for shadow-analysis etc.), add a `"permits": {...}` key with: `totalPermitsKept = len(permit_index.permits)`, `joinedByAddress = addr_total`, `joinedBySpatialFallback = spat_total`, `unjoined = unjoined`, `freshnessYears = args.permit_freshness_years`, `sanityCeilingCad = SANITY_VALUE_CEILING_CAD`, `minNeighborhoodSampleSize = MIN_NEIGHBORHOOD_SAMPLE_SIZE`, `denominatorLabel = "declared_construction_cost_cad"`, `notes = "Permit values are the declared construction cost on the building permit application. They are NOT market sale prices, assessed values, or final build costs."`.
  - Verify the consistency invariant locally before validate runs: `assert addr_total + spat_total + unjoined == len(permit_index.permits)`.
  - Purpose: coverage stats + canonical label on the wire.
  - _Requirements: 8.1, 8.2, 9.1, 9.3_

### Phase H — Orchestrator-helper tests

- [ ] 21. Test `_resolve_permits`: 4 denominatorSource paths + claim-once invariant
  - File: `tools/tests/test_building_permits.py` (extend with a new TestCase class)
  - Tests: with a synthetic 4-permit `PermitIndex` and 2 synthetic parcels, exercise the four denom_source labels (`"address_join"`, `"spatial_fallback"`, `"mixed"`, `"no_joined_permits"`); assert that a permit claimed by parcel A's address-join cannot be re-claimed by parcel B's spatial fallback (the claim-once invariant per Req 5.3 + Req 6.5).
  - Purpose: lock the join-attribution contract.
  - _Requirements: 4.5, 5.3, 5.4, 6.3, 6.4, 6.5, 10.1_

- [ ] 22. Test `_aggregate_parcel_permits`: in-window / out-of-window / boundary
  - File: `tools/tests/test_building_permits.py` (extend)
  - Tests: in-window permits sum into recentValueTotal; out-of-window permit excluded; mostRecentDate is correct; `recentCount==0` forces `denominatorSource="no_joined_permits"` even when raw `denom_source="mixed"`; permits with `floor_area_m2=0` still contribute to `recentCount` and `recentValueTotal` (those are not gated on floor_area).
  - Purpose: lock the per-parcel aggregator.
  - _Requirements: 6.1, 6.2, 6.6, 6.7, 10.1_

- [ ] 23. Test `_compute_neighborhood_perm_comp`: floor / median / per-record exclusion
  - File: `tools/tests/test_building_permits.py` (extend)
  - Tests: neighborhood with sampleSize < floor → `medianCostPerM2 is None` and `sampleSize` reports the actual count; neighborhood at exactly the floor → median computed; per-record `floor_area_m2 == 0` permit excluded from the median denominator but still contributes to its parcel's `recentCount`.
  - The Req 5.5 join-phase log line `"permits joined: %d by address, %d by spatial fallback, %d unjoined"` is now emitted in task 19's orchestrator code, not in `_compute_neighborhood_perm_comp` — assert it via `assertLogs` in test 29 (the e2e fixture) instead.
  - Purpose: lock the per-neighborhood aggregator.
  - _Requirements: 7.2, 7.3, 7.4, 7.4a, 10.1_

### Phase I — Wire-format validator

- [ ] 24. Extend `tools/parcel_io.py` schema constants
  - File: `tools/parcel_io.py`
  - Append `"permits"` and `"neighborhoodPermitComp"` to `FEATURE_PROPERTIES`. Append `"permits"` to `META_KEYS`. Add new `REQUIRED_PERMITS_META_KEYS = frozenset({"totalPermitsKept", "joinedByAddress", "joinedBySpatialFallback", "unjoined", "freshnessYears", "sanityCeilingCad", "minNeighborhoodSampleSize", "denominatorLabel", "notes"})`. Add `PERMIT_DENOMINATOR_SOURCES = frozenset({"address_join", "spatial_fallback", "mixed", "no_joined_permits"})` and `CANONICAL_DENOMINATOR_LABEL = "declared_construction_cost_cad"`.
  - Update the FEATURE_PROPERTIES count in the module docstring (currently "20", will be "22").
  - Purpose: validator gains the new key-sets to enforce.
  - _Leverage: existing FEATURE_PROPERTIES / META_KEYS / REQUIRED_STATS_KEYS pattern_
  - _Requirements: 6.1, 7.1, 8.1, 9.1_

- [ ] 25. Extend `validate()` in `tools/parcel_io.py` with permit-shape invariants
  - File: `tools/parcel_io.py`
  - Inside `validate(payload)`: add a per-feature loop checking `feat["properties"]["permits"]` is a dict with the four canonical keys; `denominatorSource` is in `PERMIT_DENOMINATOR_SOURCES`; if `recentCount == 0` then `recentValueTotal == 0` AND `recentMostRecentDate is None` AND `denominatorSource == "no_joined_permits"`. Same loop checks `feat["properties"]["neighborhoodPermitComp"]` for the three canonical keys; the floor invariant `medianCostPerM2 is None` ↔ `sampleSize < min_neighborhood_sample_size` (where `min_neighborhood_sample_size` is read from `meta.permits.minNeighborhoodSampleSize` for self-consistency).
  - Add `meta.permits` validation: every `REQUIRED_PERMITS_META_KEYS` present; `denominatorLabel == CANONICAL_DENOMINATOR_LABEL`; `joinedByAddress + joinedBySpatialFallback + unjoined == totalPermitsKept`.
  - Purpose: loud-failure on any wire-format regression.
  - _Requirements: 6.2, 7.2, 8.3, 9.2, 9.3_

- [ ] 26. Test the validator extensions in `tools/tests/test_parcel_io.py`
  - File: `tools/tests/test_parcel_io.py`
  - Update the `_make_props()` fixture to include `permits` and `neighborhoodPermitComp` with valid defaults. Update `_make_stats()` (or a new `_make_meta_permits()` helper) to satisfy the new meta keys.
  - New tests: missing `permits` on a feature → ValueError; bad `denominatorSource` enum → ValueError; `recentCount==0` with `recentValueTotal!=0` → ValueError; `medianCostPerM2 is None` with `sampleSize >= floor` (or vice versa) → ValueError; missing `meta.permits` → ValueError; wrong `denominatorLabel` → ValueError; `joined+joined+unjoined != totalPermitsKept` → ValueError; happy-path payload validates clean.
  - Purpose: lock the validator's invariants in regression tests.
  - _Requirements: 8.3, 9.2, 9.3, 10.2_

### Phase J — Slim top-N projection

- [ ] 27. Extend `tools/parcels_top_io.py` with flattened permit fields
  - File: `tools/parcels_top_io.py`
  - Append the 7 flat keys to `ROW_KEYS` (in this order, near the bottom of the tuple to keep the existing key order stable): `"permitsRecentCount"`, `"permitsRecentValueTotal"`, `"permitsRecentMostRecentDate"`, `"permitsDenominatorSource"`, `"nbPermitMedianCostPerM2"`, `"nbPermitSampleSize"`, `"nbPermitFreshnessYears"`.
  - Update `project_features` to flatten: `row["permitsRecentCount"] = props["permits"]["recentCount"]` (etc.) for each of the 7 flat keys, reading from the nested geojson properties.
  - Update the module docstring to note the flat-vs-nested split (geojson stays nested; parcels-top stays flat).
  - Purpose: surface the new fields to the goldmines/parcels pages without breaking the flat-row contract.
  - _Leverage: existing project_features per-key copy pattern_
  - _Requirements: 6.1, 7.1_

- [ ] 28. Test the projection in `tools/tests/test_parcels_top_io.py`
  - File: `tools/tests/test_parcels_top_io.py`
  - Update `_make_feature` fixture to include nested `permits` and `neighborhoodPermitComp` with valid defaults.
  - New test: all 7 flat keys present in the projected row with correct values; `permitsDenominatorSource` matches the nested input; `nbPermitMedianCostPerM2` is `None` when input is `None`.
  - Purpose: lock the flatten contract.
  - _Requirements: 10.1_

### Phase K — End-to-end fixture + docs

- [ ] 29. Add the permits e2e fixture to `tools/tests/test_e2e_parcels.py`
  - File: `tools/tests/test_e2e_parcels.py`
  - Construct a synthetic CKAN-shaped permits cache with 4 permits: parcel A's address normalizes to permit 1's address (address-join hit); parcel B's address doesn't normalize but permit 2's centroid lies inside parcel B's polygon (spatial-fallback hit); permit 3 is older than the freshness window (still loaded, dropped from aggregates); permit 4 is unjoined (address doesn't normalize and lat/lng outside any parcel polygon). Run `build_parcels` against the synthetic caches.
  - Assertions: `payload["meta"]["permits"]["totalPermitsKept"] == 4` (or 3 if classifier-dropped — match the fixture); `joinedByAddress == 1`; `joinedBySpatialFallback == 1`; `unjoined == 1` (or 2 if 3 ages out — match the fixture). Parcel A's `properties.permits.denominatorSource == "address_join"` and `recentCount == 1`. Parcel B's `denominatorSource == "spatial_fallback"`. The unjoined parcel's `denominatorSource == "no_joined_permits"`.
  - Purpose: end-to-end proof every code path is wired.
  - _Leverage: existing test_e2e_parcels.py three-parcel fixture pattern_
  - _Requirements: 10.3_

- [ ] 30. Update `tools/README.md` with the new source entry + CLI flag
  - File: `tools/README.md`
  - Append a row to the Sources table: `Building Permits | building-permits | <CKAN package id from implementation> | sources/building_permits.py`.
  - Add a short subsection under "Sources" titled "Building Permits Source" describing: dataset purpose, classifier table location, freshness window default, sanity ceiling default, sample-size floor, the joined-by-address vs spatial-fallback story, and the `denominatorLabel = "declared_construction_cost_cad"` framing.
  - Add `--permit-freshness-years` to the Parcel ETL CLI flags list.
  - Purpose: maintainer documentation.
  - _Requirements: 1.1, 3.3, 8.1_
