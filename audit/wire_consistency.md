# BloomTO &mdash; wire consistency audit

_Generated 2026-05-07 02:18 UTC &middot; 3,544 elite + 15,239 broader rows_

## Summary

| Severity | Count |
|---|---:|
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 0 |
| LOW | 14 |


## LOW

### 1. `heritageStatus` is null on all 3,544 rows in top (expected — gate-filtered)

Reason: _passes_shared excludes any heritage tier.

### 2. `inFloodingStudyArea` is constant `True` on 3,544 rows in top (expected — gate-filtered)

Reason: basement-flooding-study-areas covers ~all pre-1990 residential Toronto; this dataset is non-discriminating (see memory: project_flood_dataset_choice). Replace with TRCA Reg 41/24 riverine when endpoint is confirmed..

### 3. `inRegulatedArea` is constant `False` on 3,544 rows in top (expected — gate-filtered)

Reason: _passes_shared excludes TRCA-regulated parcels.

### 4. `outsideTransitBuffer` is constant `False` on 3,544 rows in top (expected — gate-filtered)

Reason: score>0 requires distSubwayStreetcarM<500.

### 5. `residential` is constant `True` on 3,544 rows in top (expected — gate-filtered)

Reason: score>0 requires residential zoning.

### 6. `solarShadowQuality` is constant `'measured'` on 3,544 rows in top (expected — gate-filtered)

Reason: score>0 requires positive solar.

### 7. `heritageStatus` is null on all 15,239 rows in broader (expected — gate-filtered)

Reason: _passes_shared excludes any heritage tier.

### 8. `inFloodingStudyArea` is constant `True` on 15,239 rows in broader (expected — gate-filtered)

Reason: basement-flooding-study-areas covers ~all pre-1990 residential Toronto; this dataset is non-discriminating (see memory: project_flood_dataset_choice). Replace with TRCA Reg 41/24 riverine when endpoint is confirmed..

### 9. `inRegulatedArea` is constant `False` on 15,239 rows in broader (expected — gate-filtered)

Reason: _passes_shared excludes TRCA-regulated parcels.

### 10. `outsideTransitBuffer` is constant `False` on 15,239 rows in broader (expected — gate-filtered)

Reason: score>0 requires distSubwayStreetcarM<500.

### 11. `residential` is constant `True` on 15,239 rows in broader (expected — gate-filtered)

Reason: score>0 requires residential zoning.

### 12. `solarShadowQuality` is constant `'measured'` on 15,239 rows in broader (expected — gate-filtered)

Reason: score>0 requires positive solar.

### 13. `sixplexEligible=False` but `maxUnits>=6` on 519 rows in top

Likely fine if `maxUnits` represents another threshold (e.g. fourplex+laneway, or CR mixed-use cap). Worth confirming the field's documented meaning.

**Examples:**
```
parcelId=5517404  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5514753  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5242194  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5510889  sixplexEligible=False  maxUnits=8  zoneClass=RM
parcelId=5516779  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5514733  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5507000  sixplexEligible=False  maxUnits=8  zoneClass=RM
parcelId=5515165  sixplexEligible=False  maxUnits=8  zoneClass=CR
…and 511 more
```

### 14. `sixplexEligible=False` but `maxUnits>=6` on 2,186 rows in broader

Likely fine if `maxUnits` represents another threshold (e.g. fourplex+laneway, or CR mixed-use cap). Worth confirming the field's documented meaning.

**Examples:**
```
parcelId=5311910  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5515482  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5517404  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5514753  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5509672  sixplexEligible=False  maxUnits=8  zoneClass=RM
parcelId=5507071  sixplexEligible=False  maxUnits=8  zoneClass=RM
parcelId=5508880  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5514962  sixplexEligible=False  maxUnits=8  zoneClass=CR
…and 2,178 more
```
