# BloomTO &mdash; wire consistency audit

_Generated 2026-05-06 20:46 UTC &middot; 3,778 elite + 15,624 broader rows_

## Summary

| Severity | Count |
|---|---:|
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 0 |
| LOW | 14 |


## LOW

### 1. `heritageStatus` is null on all 3,778 rows in top (expected — gate-filtered)

Reason: _passes_shared excludes any heritage tier.

### 2. `inFloodingStudyArea` is constant `True` on 3,778 rows in top (expected — gate-filtered)

Reason: basement-flooding-study-areas covers ~all pre-1990 residential Toronto; this dataset is non-discriminating (see memory: project_flood_dataset_choice). Replace with TRCA Reg 41/24 riverine when endpoint is confirmed..

### 3. `inRegulatedArea` is constant `False` on 3,778 rows in top (expected — gate-filtered)

Reason: _passes_shared excludes TRCA-regulated parcels.

### 4. `outsideTransitBuffer` is constant `False` on 3,778 rows in top (expected — gate-filtered)

Reason: score>0 requires distSubwayStreetcarM<500.

### 5. `residential` is constant `True` on 3,778 rows in top (expected — gate-filtered)

Reason: score>0 requires residential zoning.

### 6. `solarShadowQuality` is constant `'measured'` on 3,778 rows in top (expected — gate-filtered)

Reason: score>0 requires positive solar.

### 7. `heritageStatus` is null on all 15,624 rows in broader (expected — gate-filtered)

Reason: _passes_shared excludes any heritage tier.

### 8. `inFloodingStudyArea` is constant `True` on 15,624 rows in broader (expected — gate-filtered)

Reason: basement-flooding-study-areas covers ~all pre-1990 residential Toronto; this dataset is non-discriminating (see memory: project_flood_dataset_choice). Replace with TRCA Reg 41/24 riverine when endpoint is confirmed..

### 9. `inRegulatedArea` is constant `False` on 15,624 rows in broader (expected — gate-filtered)

Reason: _passes_shared excludes TRCA-regulated parcels.

### 10. `outsideTransitBuffer` is constant `False` on 15,624 rows in broader (expected — gate-filtered)

Reason: score>0 requires distSubwayStreetcarM<500.

### 11. `residential` is constant `True` on 15,624 rows in broader (expected — gate-filtered)

Reason: score>0 requires residential zoning.

### 12. `solarShadowQuality` is constant `'measured'` on 15,624 rows in broader (expected — gate-filtered)

Reason: score>0 requires positive solar.

### 13. `sixplexEligible=False` but `maxUnits>=6` on 588 rows in top

Likely fine if `maxUnits` represents another threshold (e.g. fourplex+laneway, or CR mixed-use cap). Worth confirming the field's documented meaning.

**Examples:**
```
parcelId=5266679  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5248274  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=10650316  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5517404  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5514753  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5242194  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5510889  sixplexEligible=False  maxUnits=8  zoneClass=RM
parcelId=5272203  sixplexEligible=False  maxUnits=8  zoneClass=CR
…and 580 more
```

### 14. `sixplexEligible=False` but `maxUnits>=6` on 2,302 rows in broader

Likely fine if `maxUnits` represents another threshold (e.g. fourplex+laneway, or CR mixed-use cap). Worth confirming the field's documented meaning.

**Examples:**
```
parcelId=5287080  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5311910  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5515482  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5266679  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5248274  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5287022  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=10650316  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5517404  sixplexEligible=False  maxUnits=8  zoneClass=CR
…and 2,294 more
```
