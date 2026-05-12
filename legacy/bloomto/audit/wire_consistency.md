# BloomTO &mdash; wire consistency audit

_Generated 2026-05-07 15:17 UTC &middot; 18,854 elite + 20,000 broader rows_

## Summary

| Severity | Count |
|---|---:|
| CRITICAL | 0 |
| HIGH | 2 |
| MEDIUM | 3 |
| LOW | 12 |


## HIGH

### 1. `sixplexEligible=True` on non-residential zone in top: 1 rows

Sixplex as-of-right (June 2025) is for residential zones; CR is a mixed-use carve-out that may be allowed depending on the wording.

**Examples:**
```
parcelId=5303153  zoneClass=CL  sixplexEligible=True
```

### 2. top and broader have partial overlap: 3,321 shared, 15,533 only-top, 16,679 only-broader

Two-tier design is ambiguous. Either top should be a strict subset of broader (magazine cover + back-of-book) or fully disjoint. Partial overlap suggests an ETL ordering bug.


## MEDIUM

### 1. `cornerLot` is 0.3% `False` in top

51/18,854 rows are `False`. Near-constant boolean — verify the gate isn't already excluding the minority.

### 2. `cornerLot` is 0.1% `False` in broader

26/20,000 rows are `False`. Near-constant boolean — verify the gate isn't already excluding the minority.

### 3. `lotAreaM2` outside [50, 20000] on 1 rows in broader

Expected range is [50, 20000].

**Examples:**
```
parcelId=5483768  lotAreaM2=28438
```


## LOW

### 1. `heritageStatus` is null on all 18,854 rows in top (expected — gate-filtered)

Reason: _passes_shared excludes any heritage tier.

### 2. `inFloodingStudyArea` is constant `True` on 18,854 rows in top (expected — gate-filtered)

Reason: basement-flooding-study-areas covers ~all pre-1990 residential Toronto; this dataset is non-discriminating (see memory: project_flood_dataset_choice). Replace with TRCA Reg 41/24 riverine when endpoint is confirmed..

### 3. `inRegulatedArea` is constant `False` on 18,854 rows in top (expected — gate-filtered)

Reason: _passes_shared excludes TRCA-regulated parcels.

### 4. `residential` is constant `True` on 18,854 rows in top (expected — gate-filtered)

Reason: elite gate requires residential zoning.

### 5. `solarShadowQuality` is constant `'measured'` on 18,854 rows in top (expected — gate-filtered)

Reason: elite parcels generally have measured shadow quality (synthetic gate).

### 6. `heritageStatus` is null on all 20,000 rows in broader (expected — gate-filtered)

Reason: _passes_shared excludes any heritage tier.

### 7. `inFloodingStudyArea` is constant `True` on 20,000 rows in broader (expected — gate-filtered)

Reason: basement-flooding-study-areas covers ~all pre-1990 residential Toronto; this dataset is non-discriminating (see memory: project_flood_dataset_choice). Replace with TRCA Reg 41/24 riverine when endpoint is confirmed..

### 8. `inRegulatedArea` is constant `False` on 20,000 rows in broader (expected — gate-filtered)

Reason: _passes_shared excludes TRCA-regulated parcels.

### 9. `residential` is constant `True` on 20,000 rows in broader (expected — gate-filtered)

Reason: elite gate requires residential zoning.

### 10. `solarShadowQuality` is constant `'measured'` on 20,000 rows in broader (expected — gate-filtered)

Reason: elite parcels generally have measured shadow quality (synthetic gate).

### 11. `sixplexEligible=False` but `maxUnits>=6` on 2,890 rows in top

Likely fine if `maxUnits` represents another threshold (e.g. fourplex+laneway, or CR mixed-use cap). Worth confirming the field's documented meaning.

**Examples:**
```
parcelId=10634817  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5143845  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5228106  sixplexEligible=False  maxUnits=8  zoneClass=RM
parcelId=5217609  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5509025  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5516703  sixplexEligible=False  maxUnits=8  zoneClass=RM
parcelId=5140440  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5504996  sixplexEligible=False  maxUnits=8  zoneClass=CR
…and 2,882 more
```

### 12. `sixplexEligible=False` but `maxUnits>=6` on 905 rows in broader

Likely fine if `maxUnits` represents another threshold (e.g. fourplex+laneway, or CR mixed-use cap). Worth confirming the field's documented meaning.

**Examples:**
```
parcelId=10578747  sixplexEligible=False  maxUnits=8  zoneClass=RM
parcelId=10634817  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5143845  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5156878  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5228106  sixplexEligible=False  maxUnits=8  zoneClass=RM
parcelId=5217609  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5501134  sixplexEligible=False  maxUnits=8  zoneClass=CR
parcelId=5509025  sixplexEligible=False  maxUnits=8  zoneClass=CR
…and 897 more
```
