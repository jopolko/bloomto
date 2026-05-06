# BloomTO &mdash; wire consistency audit

_Generated 2026-05-05 22:01 UTC &middot; 3,757 elite + 15,514 broader rows_

## Summary

| Severity | Count |
|---|---:|
| CRITICAL | 0 |
| HIGH | 14 |
| MEDIUM | 0 |
| LOW | 2 |


## HIGH

### 1. `bloom` is the constant `False` on all 3,757 non-null rows in top

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 2. `heritageStatus` is null on all 3,757 rows in top

Column carries no signal — drop from wire or fix ETL.

### 3. `inFloodingStudyArea` is the constant `True` on all 3,757 non-null rows in top

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 4. `inRegulatedArea` is the constant `False` on all 3,757 non-null rows in top

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 5. `outsideTransitBuffer` is the constant `False` on all 3,757 non-null rows in top

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 6. `residential` is the constant `True` on all 3,757 non-null rows in top

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 7. `solarShadowQuality` is the constant `'measured'` on all 3,757 non-null rows in top

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 8. `bloom` is the constant `False` on all 15,514 non-null rows in broader

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 9. `heritageStatus` is null on all 15,514 rows in broader

Column carries no signal — drop from wire or fix ETL.

### 10. `inFloodingStudyArea` is the constant `True` on all 15,514 non-null rows in broader

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 11. `inRegulatedArea` is the constant `False` on all 15,514 non-null rows in broader

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 12. `outsideTransitBuffer` is the constant `False` on all 15,514 non-null rows in broader

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 13. `residential` is the constant `True` on all 15,514 non-null rows in broader

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).

### 14. `solarShadowQuality` is the constant `'measured'` on all 15,514 non-null rows in broader

Either the gate already filters by this value (then drop from wire), or the column never wired through (then fix ETL).


## LOW

### 1. `sixplexEligible=False` but `maxUnits>=6` on 582 rows in top

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
…and 574 more
```

### 2. `sixplexEligible=False` but `maxUnits>=6` on 2,283 rows in broader

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
…and 2,275 more
```
