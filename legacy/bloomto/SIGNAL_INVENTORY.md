# Per-parcel wire signal inventory

Generated 2026-05-07 from `data/parcels-top.json` (n=241 curated) and `data/parcels.geojson` (n=243,798 master).

Every signal BloomTO has on each parcel, organized by domain. Use it to spot
combinations that might separate detached from attached, or surface other patterns.
Distributions are computed across the **241 curated picks** (which already pass the
elite gate, so the ranges are tighter than the full 528K-parcel universe).

## Cross-reference — what we tried and what didn't separate detached vs attached

| Signal | Detached cohort | Attached cohort | Verdict |
|---|---|---|---|
| Cross-building distance | med 1.94m, 36% within 2m | med 0.57m, 80% within 2m | 76% best — too noisy |
| `abutsLaneway` | 0% | 20% | high-precision but only catches 20% of attached |
| `lotShortAxisM` | med 18.9m | med 16.1m | distributions overlap |
| `lotAspectRatio` | med 2.54 | med 3.52 | distributions overlap |
| Composite (any of A/B/C) | — | — | 73% accuracy ceiling |

If you spot a clean separator we missed, that's the signal we want.

## Identity

### `parcelId`
- **Type:** string. **Values:** `5385860`×1, `5403848`×1, `5407976`×1, `5353067`×1, `5356655`×1, `5380842`×1, `5354510`×1, `5392886`×1 + 233 more

### `address`
- **Type:** string. **Values:** `243 Coxwell Ave`×1, `80 Kippendavie Ave`×1, `581 Parliament St`×1, `39 Glen Oak Dr`×1, `2 Bastedo Ave`×1, `109 Beech Ave`×1, `1 Bastedo Ave`×1, `387R Leslie St`×1 + 233 more

### `neighborhood`
- **Type:** string. **Values:** `High Park-Swansea`×27, `South Parkdale`×20, `The Beaches`×15, `Woodbine Corridor`×14, `High Park North`×13, `East End-Danforth`×12, `Dufferin Grove`×10, `Annex`×9 + 48 more

### `lat`
- **Type:** float. **Range:** 43.59222 → 43.78255
- **Distribution:** p10=43.63841, med=43.66597, p90=43.70891

### `lng`
- **Type:** float. **Range:** -79.59865 → -79.28206
- **Distribution:** p10=-79.47556, med=-79.42682, p90=-79.3078

## Geometry

### `lotAreaM2`
- **Type:** int. **Range:** 357 → 2300
- **Distribution:** p10=581, med=843, p90=1293

### `lotShortAxisM`
- **Type:** float. **Range:** 8.9 → 55.0
- **Distribution:** p10=14.3, med=19.5, p90=34.0

### `lotLongAxisM`
- **Type:** float. **Range:** 28.5 → 97.2
- **Distribution:** p10=37.2, med=49.3, p90=68.3

### `lotAspectRatio`
- **Type:** float. **Range:** 1.07 → 11.48
- **Distribution:** p10=1.4, med=2.65, p90=5.46

### `lotOrientationDeg`
- **Type:** float. **Range:** 7.8 → 175.5
- **Distribution:** p10=66.8, med=74.2, p90=164.3

### `cornerLot`
- **Type:** boolean. **True:** 2% (5 of 241)

### `abutsLaneway`
- **Type:** boolean. **True:** 17% (41 of 241)

### `nearRapidToCorridor`
- **Type:** boolean. **True:** 5% (12 of 241)

## Existing structure (the broken classifier zone)

### `existingMaxBuildingHeightM`
- **Type:** float. **Range:** 5.1 → 17.9
- **Distribution:** p10=7.2, med=12.149999999999999, p90=16.6
- **Null:** 5%

### `existingStructureType`
- **Type:** string. **Values:** `detached`×228, `vacant`×13

### `buildingCoverageRatio`
- **Type:** float. **Range:** 0.0 → 0.653
- **Distribution:** p10=0.134, med=0.242, p90=0.423

### `builtYear`
- **Type:** int. **Range:** 1955 → 2013
- **Distribution:** p10=1955, med=1955, p90=1985

### `postwarNeighborhood`
- **Type:** boolean. **True:** 57% (137 of 241)

### `neighborHeightNAvgM`
- **Type:** float. **Range:** 5.3 → 33.1
- **Distribution:** p10=8.7, med=12.2, p90=17.7
- **Null:** 42%

### `neighborHeightSAvgM`
- **Type:** float. **Range:** 4.6 → 59.1
- **Distribution:** p10=8.0, med=12.5, p90=18.5
- **Null:** 43%

### `neighborHeightEAvgM`
- **Type:** float. **Range:** 5.2 → 27.2
- **Distribution:** p10=8.1, med=12.6, p90=17.9
- **Null:** 44%

### `neighborHeightWAvgM`
- **Type:** float. **Range:** 2.7 → 21.8
- **Distribution:** p10=8.1, med=11.9, p90=16.1
- **Null:** 40%

## Zoning

### `zoneClass`
- **Type:** string. **Values:** `R`×145, `RD`×50, `CR`×26, `RM`×20

### `zoneString`
- **Type:** string. **Values:** `R (d0.6) (x737)`×14, `RD (f15.0; a550) (x5)`×13, `R (d0.6) (x735)`×11, `R (d0.6)`×10, `RM (f12.0; u4; d0.8) (x252)`×10, `R (d0.6) (x674)`×7, `R (d0.6) (x575)`×6, `RD (f12.0; a370; d0.35)`×6 + 98 more

### `zoneFsi`
- **Type:** float. **Range:** 1.5 → 4.0
- **Distribution:** p10=2.0, med=2.5, p90=3.0
- **Null:** 91%

### `zoneMinLotFrontageM`
- **Type:** float. **Range:** 4.5 → 18.0
- **Distribution:** p10=7.5, med=12.0, p90=15.0
- **Null:** 72%

### `zoneMinLotAreaM2`
- **Type:** int. **Range:** 185 → 1248
- **Distribution:** p10=300, med=510, p90=550
- **Null:** 85%

### `maxUnits`
- **Type:** int. **Range:** 4 → 54
- **Distribution:** p10=4, med=6, p90=8

### `maxUnitsRationale`
- **Type:** string. **Values:** `sixplex_carveout`×174, `zone_average`×40, `fsi_derived`×21, `by_law_units`×6

### `residential`
- **Type:** boolean. **True:** 100% (241 of 241)

## Heritage / regulatory

### `heritageStatus`
- 100% null

### `inFloodingStudyArea`
- **Type:** boolean. **True:** 100% (241 of 241)

### `inRegulatedArea`
- **Type:** boolean. **True:** 0% (0 of 241)

## Transit

### `distSubwayM`
- **Type:** int. **Range:** 71 → 5000
- **Distribution:** p10=263, med=987, p90=2531

### `distStreetcarM`
- **Type:** int. **Range:** 15 → 5000
- **Distribution:** p10=74, med=282, p90=1481

### `distSubwayStreetcarM`
- **Type:** int. **Range:** 15 → 499
- **Distribution:** p10=72, med=247, p90=432

### `distBusM`
- **Type:** int. **Range:** 16 → 933
- **Distribution:** p10=53, med=166, p90=396

## Sixplex carve-out

### `sixplexEligible`
- **Type:** boolean. **True:** 85% (204 of 241)

### `sixplexBonusValueCad`
- **Type:** int. **Range:** 133158 → 1600000
- **Distribution:** p10=133158, med=500000.0, p90=700000
- **Null:** 25%

## Permits (this lot)

### `permitsRecentCount`
- **Type:** int. **Range:** 0 → 2
- **Distribution:** p10=0, med=0, p90=0

### `permitsRecentValueTotal`
- **Type:** int. **Range:** 0 → 10000000
- **Distribution:** p10=0, med=0, p90=0

### `permitsRecentMostRecentDate`
- **Type:** string. **Values:** `2024-12-13`×1, `2025-05-27`×1, `2021-12-10`×1, `2023-03-20`×1, `2025-08-01`×1, `2022-10-12`×1, `2025-08-11`×1, `2025-09-05`×1 + 8 more
- **Null:** 93%

### `permitsDenominatorSource`
- **Type:** string. **Values:** `no_joined_permits`×225, `address_join`×16

## Permits (neighbourhood)

### `nbPermitMedianCostPerUnit`
- **Type:** int. **Range:** 50000 → 900000
- **Distribution:** p10=100000, med=250000, p90=500000
- **Null:** 11%

### `nbPermitSampleSize`
- **Type:** int. **Range:** 0 → 71
- **Distribution:** p10=9, med=31, p90=41

### `nbPermitsPer1kDwellings`
- **Type:** float. **Range:** 0.0 → 11.05
- **Distribution:** p10=1.42, med=3.34, p90=7.02

## Neighbourhood context (NPP 2021)

### `nbMedHouseholdIncome`
- **Type:** int. **Range:** 57200 → 148000
- **Distribution:** p10=62000, med=87000, p90=103000

### `nbAvgHouseholdIncome`
- **Type:** int. **Range:** 74100 → 284400
- **Distribution:** p10=83400, med=121700, p90=184000

### `nbMedDwellingValue`
- **Type:** int. **Range:** 540000 → 2000000
- **Distribution:** p10=800000, med=1100000, p90=1500000

### `nbAvgDwellingValue`
- **Type:** int. **Range:** 658000 → 2036000
- **Distribution:** p10=960000, med=1216000, p90=1616000

### `neighborhoodCanopyPct`
- **Type:** int. **Range:** 9 → 49
- **Distribution:** p10=15, med=28, p90=49

## Site context

### `streetTreeCount`
- **Type:** int. **Range:** 0 → 18
- **Distribution:** p10=0, med=2, p90=5

### `matureTreeCount`
- **Type:** int. **Range:** 0 → 13
- **Distribution:** p10=0, med=1, p90=2

### `distBikeLaneM`
- **Type:** int. **Range:** 17 → 854
- **Distribution:** p10=30, med=135, p90=414

## Solar (currently hidden in UI)

### `solarScore`
- **Type:** int. **Range:** 0 → 74
- **Distribution:** p10=12, med=28, p90=53

### `solarScoreRaw`
- **Type:** int. **Range:** 0 → 100
- **Distribution:** p10=18, med=47, p90=100

### `solarShadowQuality`
- **Type:** string. **Values:** `measured`×241

### `solarYieldKwhPerYr`
- **Type:** int. **Range:** 0 → 54251
- **Distribution:** p10=4336, med=11440, p90=39646

### `pvCapacityKwEstimate`
- **Type:** float. **Range:** 0.0 → 47.2
- **Distribution:** p10=3.8, med=9.9, p90=34.5

## Sample row — `160 Dowling Ave` (South Parkdale)

Every field on the wire, actual values, for one curated parcel:

```json
{
  "parcelId": "5493429",
  "address": "160 Dowling Ave",
  "neighborhood": "South Parkdale",
  "builtYear": 1970,
  "lat": 43.63841,
  "lng": -79.43967,
  "maxUnits": 6,
  "maxUnitsRationale": "sixplex_carveout",
  "zoneString": "R (d1.0) (x988)",
  "zoneFsi": null,
  "zoneMinLotFrontageM": null,
  "zoneMinLotAreaM2": null,
  "zoneClass": "R",
  "residential": true,
  "lotAreaM2": 725,
  "heritageStatus": null,
  "buildingCoverageRatio": 0.295,
  "cornerLot": false,
  "lotAspectRatio": 3.94,
  "distSubwayM": 2306,
  "distSubwayStreetcarM": 181,
  "distStreetcarM": 181,
  "distBusM": 181,
  "abutsLaneway": false,
  "nearRapidToCorridor": false,
  "inFloodingStudyArea": true,
  "inRegulatedArea": false,
  "permitsRecentCount": 1,
  "permitsRecentValueTotal": 200000,
  "permitsRecentMostRecentDate": "2025-08-01",
  "permitsDenominatorSource": "address_join",
  "nbPermitMedianCostPerUnit": 66579,
  "nbPermitSampleSize": 16,
  "neighborhoodCanopyPct": 19,
  "nbMedHouseholdIncome": 57200,
  "nbAvgHouseholdIncome": 76300,
  "nbMedDwellingValue": 800000,
  "nbAvgDwellingValue": 976000,
  "nbPermitsPer1kDwellings": 1.42,
  "streetTreeCount": 2,
  "matureTreeCount": 2,
  "distBikeLaneM": 38,
  "sixplexEligible": true,
  "solarScore": 48,
  "solarScoreRaw": 89,
  "solarShadowQuality": "measured",
  "postwarNeighborhood": false,
  "lotLongAxisM": 47.8,
  "lotShortAxisM": 16.2,
  "lotOrientationDeg": 74.2,
  "neighborHeightNAvgM": 12.0,
  "neighborHeightSAvgM": null,
  "neighborHeightEAvgM": 11.8,
  "neighborHeightWAvgM": null,
  "existingMaxBuildingHeightM": 11.0,
  "existingStructureType": "detached",
  "solarYieldKwhPerYr": 21648,
  "pvCapacityKwEstimate": 18.8,
  "sixplexBonusValueCad": 133158
}
```

## Fields on `parcels.geojson` (master) but NOT projected to `parcels-top.json`

These are nested objects we currently flatten into the per-parcel rows. If you want a richer signal, we can pull more sub-keys through `tools/parcels_top_io.py`:

- `neighborhoodPermitComp` — dict, keys: ['medianCostPerUnit', 'sampleSize', 'freshnessYears']
  - sample: `{"medianCostPerUnit": null, "sampleSize": 9, "freshnessYears": 5}`
- `neighborHeights` — dict, keys: ['nAvgM', 'sAvgM', 'eAvgM', 'wAvgM']
  - sample: `{"nAvgM": null, "sAvgM": null, "eAvgM": null, "wAvgM": null}`
- `permits` — dict, keys: ['recentCount', 'recentValueTotal', 'recentMostRecentDate', 'denominatorSource']
  - sample: `{"recentCount": 0, "recentValueTotal": 0, "recentMostRecentDate": null, "denominatorSource": "no_joined_permits"}`
- `lotGeometry` — dict, keys: ['longAxisM', 'shortAxisM', 'orientationDeg']
  - sample: `{"longAxisM": 456.9, "shortAxisM": 196.3, "orientationDeg": 72.7}`

## What we *don't* have (and why classification is hard)

- **MPAC structure-type code** — paywalled. Would directly tell us detached/semi/row.
- **Land Registry / Teranet sale prices** — paywalled.
- **Per-parcel dwelling unit count** — Toronto Open Data has it as `DWELLING_UNITS_EXISTING` on building permits, but only for the subset of parcels with permits in the last 5 years (~10K of 528K). Useful as a partial signal.
- **Address Points USE_CODE** — Toronto nullified this field in 2021-07-29 (per project memory).
- **Building Outlines structure-class** — the dataset distinguishes only `house`, `apartment building`, `school`, `garage`, `shed`, `commercial`, `industrial`, etc. — not detached vs semi/row.

---

# Full dataset inventory — every CKAN/OSM/file source BloomTO consumes

Generated 2026-05-07. For each dataset: source file in `tools/cache/`, the
loader module under `tools/sources/`, the fields available in the source
schema, and (where I know) what we currently consume vs what we leave on
the table. Use this to spot signals we're not using yet.

## Parcel-shape datasets

### Property Boundaries
- **File:** `tools/cache/property_boundaries.geojson`
- **Loader:** `tools/sources/zoning.py — Parcel dataclass`
- **Source schema fields:** `name`

### Zoning By-law 569-2013
- **File:** `tools/cache/zoning_area.geojson`
- **Loader:** `tools/sources/zoning.py — ZoneRecord dataclass`
- **Source schema fields:** `name`

### 3D Massing
- **File:** `tools/cache/massing.shp.zip`
- **Loader:** `tools/sources/massing.py`
- **Source schema fields:** `(shapefile zip — see loader for fields)`

### Building Outlines
- **File:** `tools/cache/building_outlines.csv`
- **Loader:** `tools/sources/building_outlines.py`
- **Source schema fields:** `_id`, `SUBTYPE_CODE`, `SUBTYPE_DESC`, `ELEVATION`, `DERIVED_HEIGHT`, `OBJECTID`, `LAST_GEOMETRY_MAINT`, `LAST_ATTRIBUTE_MAINT`, `geometry`

### Toronto Centreline
- **File:** `tools/cache/centreline.geojson`
- **Loader:** `tools/sources/streets.py`
- **Source schema fields:** `name`

### Cycling Network
- **File:** `tools/cache/cycling.geojson`
- **Loader:** `tools/sources/cycling.py`
- **Source schema fields:** `_id`, `SEGMENT_ID`, `INSTALLED`, `UPGRADED`, `PRE_AMALGAMATION`, `STREET_NAME`, `FROM_STREET`, `TO_STREET`, `ROADCLASS`, `CNPCLASS`, `SURFACE`, `OWNER`, `DIR_LOWORDER`, `INFRA_LOWORDER`, `INFRA_HIGHORDER`, `CONVERTED`

### Heritage Register
- **File:** `tools/cache/heritage.shp.zip`
- **Loader:** `tools/sources/heritage.py`
- **Source schema fields:** `(shapefile zip — see loader for fields)`

## Hazard / regulatory layers

### Basement Flooding Study Areas
- **File:** `tools/cache/basement_flooding_study_areas.geojson`
- **Loader:** `tools/sources/flood.py`
- **Source schema fields:** `_id`, `Asset Identification`

### TRCA Regulated Area
- **File:** `tools/cache/trca_regulated_area.geojson`
- **Loader:** `tools/sources/trca_floodplain.py`
- **Source schema fields:** `name`

### Sixplex District (T&EY + Ward 23)
- **File:** `tools/cache/sixplex_district.geojson`
- **Loader:** `tools/sources/sixplex_district.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

## Permit-activity layers

### Building Permits
- **File:** `tools/cache/building_permits.csv`
- **Loader:** `tools/sources/building_permits.py`
- **Source schema fields:** `_id`, `PERMIT_NUM`, `REVISION_NUM`, `PERMIT_TYPE`, `STRUCTURE_TYPE`, `WORK`, `STREET_NUM`, `STREET_NAME`, `STREET_TYPE`, `STREET_DIRECTION`, `POSTAL`, `GEO_ID`, `WARD_GRID`, `APPLICATION_DATE`, `ISSUED_DATE`, `COMPLETED_DATE`, `STATUS`, `DESCRIPTION`, `CURRENT_USE`, `PROPOSED_USE`, `DWELLING_UNITS_CREATED`, `DWELLING_UNITS_LOST`, `EST_CONST_COST`, `ASSEMBLY`, `INSTITUTIONAL`, `RESIDENTIAL`, `BUSINESS_AND_PERSONAL_SERVICES`, `MERCANTILE`, `INDUSTRIAL`, `INTERIOR_ALTERATIONS` (+2 more)

### CKAN signals — severance applications
- **File:** `tools/cache/coa_active.json`
- **Loader:** `tools/sources/coa_applications.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

### CKAN signals — demolition permits
- **File:** `tools/cache/demo_permits.json`
- **Loader:** `tools/sources/demo_permits.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

### CKAN signals — property violations
- **File:** `tools/cache/(streamed live, no cache)`
- **Loader:** `tools/sources/property_violations.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

### CKAN signals — preliminary zoning
- **File:** `tools/cache/(streamed live, no cache)`
- **Loader:** `tools/sources/preliminary_zoning_reviews.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

## Demographics + neighborhood

### NPP 2021 (census profile)
- **File:** `tools/cache/npp_2021.xlsx`
- **Loader:** `tools/sources/census.py`
- **Source schema fields:** `(see census.py loader; rows are census variables, cols are nbhds)`

### Toronto Neighborhoods (158)
- **File:** `tools/cache/neighbourhoods.geojson`
- **Loader:** `tools/sources/neighborhoods.py`
- **Source schema fields:** `_id`, `AREA_ID`, `AREA_ATTR_ID`, `PARENT_AREA_ID`, `AREA_SHORT_CODE`, `AREA_LONG_CODE`, `AREA_NAME`, `AREA_DESC`, `CLASSIFICATION`, `CLASSIFICATION_CODE`, `OBJECTID`

### Community Council Boundaries
- **File:** `tools/cache/community_council_boundaries.geojson`
- **Loader:** `tools/sources/sixplex_district.py`
- **Source schema fields:** `_id`, `AREA_ID`, `AREA_ATTR_ID`, `PARENT_AREA_ID`, `AREA_SHORT_CODE`, `AREA_LONG_CODE`, `AREA_NAME`, `AREA_DESC`, `OBJECTID`

## Site-context layers

### SolarTO
- **File:** `tools/cache/solar_to.csv`
- **Loader:** `tools/sources/solar_to.py`
- **Source schema fields:** `_id`, `objectid`, `structureid`, `roofsize`, `rooftop_sqft`, `roof_size800k`, `annual_electricity_generation_k`, `system_size`, `system_cost`, `first_year_bill_savings`, `f_25_year_bill_savings`, `payback_period`, `annual_ghg_reduction_kg`, `total_ghg_reduction_kg`, `trees_grown_for_10_years`, `cars_off_the_road`, `geometry`

### Forest & Land Cover
- **File:** `tools/cache/canopy_centroids_4326.geojson`
- **Loader:** `tools/sources/canopy.py`
- **Source schema fields:** `gridcode`, `Shape_Area`

### Street Trees
- **File:** `tools/cache/street_trees.csv`
- **Loader:** `tools/sources/street_trees.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

### TTC GTFS — stops.txt
- **File:** `tools/cache/gtfs/stops.txt`
- **Loader:** `tools/sources/ttc.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

### TTC GTFS — routes.txt
- **File:** `tools/cache/gtfs/routes.txt`
- **Loader:** `tools/sources/ttc.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

## Institutional exclusions (12 sources combined)

### OSM Landuse
- **File:** `tools/cache/osm_landuse.json`
- **Loader:** `tools/sources/osm_landuse.py`
- **Source schema fields:** `addr:city`, `addr:housenumber`, `addr:postcode`, `addr:street`, `amenity`, `fax`, `isced:level`, `landuse`, `loc_name`, `mascot`

### OSM TTC Stations
- **File:** `tools/cache/osm_ttc_stations.geojson`
- **Loader:** `tools/sources/osm_ttc_stations.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

### Institutions: schools
- **File:** `tools/cache/institutions_schools.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** `_id`, `OBJECTID`, `GEO_ID`, `NAME`, `SCHOOL_LEVEL`, `SCHOOL_TYPE`, `BOARD_NAME`, `SOURCE_ADDRESS`, `SCHOOL_TYPE_DESC`, `ADDRESS_POINT_ID`, `ADDRESS_NUMBER`, `LINEAR_NAME_FULL`, `ADDRESS_FULL`, `POSTAL_CODE`, `MUNICIPALITY`, `CITY`, `PLACE_NAME`, `GENERAL_USE_CODE`, `CENTRELINE_ID`, `LO_NUM`, `LO_NUM_SUF`, `HI_NUM`, `HI_NUM_SUF`, `LINEAR_NAME_ID`

### Institutions: places-of-worship
- **File:** `tools/cache/institutions_places_of_worship.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** `_id`, `ADDRESS_POINT_ID`, `ADDRESS_NUMBER`, `LINEAR_NAME_FULL`, `ADDRESS_FULL`, `POSTAL_CODE`, `MUNICIPALITY`, `CITY`, `PLACE_NAME`, `GENERAL_USE_CODE`, `CENTRELINE_ID`, `LO_NUM`, `LO_NUM_SUF`, `HI_NUM`, `HI_NUM_SUF`, `LINEAR_NAME_ID`, `X`, `Y`, `LONGITUDE`, `LATITUDE`, `OBJECTID`, `FTH_PRIORITY`, `FTH_ORGANIZATION`, `FTH_FAITH`, `FTH_DENOMINATION`, `FTH_GROUPING`, `FTH_PHONE`, `FTH_EXTENSION`, `FTH_FAX`, `FTH_CELL` (+14 more)

### Institutions: parks
- **File:** `tools/cache/institutions_parks.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

### Institutions: libraries
- **File:** `tools/cache/institutions_libraries.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** `_id`, `BranchCode`, `PhysicalBranch`, `BranchName`, `Address`, `PostalCode`, `Website`, `Telephone`, `SquareFootage`, `PublicParking`, `KidsStop`, `LeadingReading`, `CLC`, `DIH`, `TeenCouncil`, `YouthHub`, `AdultLiteracyProgram`, `Service Tier`, `Lat`, `Long`, `NBHDNo`, `NBHDName`, `TPLNIA`, `WardNo`, `WardName`, `PresentSiteYear`, `PublicWashroom`

### Institutions: fire stations
- **File:** `tools/cache/institutions_fire.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** `_id`, `ADDRESS_POINT_ID`, `ADDRESS_NUMBER`, `LINEAR_NAME_FULL`, `ADDRESS`, `MUNICIPALITY_NAME`, `CENTRELINE_ID`, `OBJECTID`, `ID`, `STATION`, `YEAR_BUILD`, `WARD`, `WARD_NAME`, `TYPE_DESC`, `PUBLIC_ED_OFFICE`, `FIRE_PREV_OFFICE`, `FIRE_OTHER`

### Institutions: police facilities
- **File:** `tools/cache/institutions_police.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** `_id`, `OBJECTID_1`, `FACILITY`, `ORGANIZATION`, `ADDRESS`, `POSTAL_CODE`

### Institutions: ambulance stations
- **File:** `tools/cache/institutions_ambulance.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** `_id`, `ADDRESS_POINT_ID`, `ADDRESS_NUMBER`, `LINEAR_NAME_FULL`, `ADDRESS_FULL`, `POSTAL_CODE`, `MUNICIPALITY`, `CITY`, `PLACE_NAME`, `GENERAL_USE_CODE`, `CENTRELINE_ID`, `LO_NUM`, `LO_NUM_SUF`, `HI_NUM`, `HI_NUM_SUF`, `LINEAR_NAME_ID`, `OBJECTID`, `EMS_ID`, `EMS_NAME`, `EMS_ADDRESS`, `EMS_NOTES`, `EMS_WEBSITE`, `EMS_EXTRA1`, `EMS_EXTRA2`, `EMS_ADDITIONAL_ADDRESS_INFO`

### Institutions: long-term-care
- **File:** `tools/cache/institutions_ltc.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** `_id`, `ADDRESS_POINT_ID`, `ADDRESS_FULL`, `POSTAL_CODE`, `MUNICIPALITY`, `CITY`, `OBJECTID`, `ID`, `NAME`, `BEDS`, `RESPITE`, `CONVALESCE`, `SMOKING`, `CITY_OP`, `TELEPHONE`, `ADULT_DAY_PROGRAM`

### Institutions: parks & rec facilities
- **File:** `tools/cache/institutions_parks_rec.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** (couldn't peek — large file or unsupported format)

### Institutions: child-care
- **File:** `tools/cache/institutions_child_care.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** `_id`, `LOC_ID`, `LOC_NAME`, `AUSPICE`, `ADDRESS`, `PCODE`, `ward`, `PHONE`, `bldg_type`, `BLDGNAME`, `IGSPACE`, `TGSPACE`, `PGSPACE`, `KGSPACE`, `SGSPACE`, `TOTSPACE`, `subsidy`, `run_date`, `cwelcc_flag`

### Institutions: community facilities
- **File:** `tools/cache/institutions_community_facilities.geojson`
- **Loader:** `tools/sources/institutions.py`
- **Source schema fields:** `_id`, `LOCATIONID`, `ASSET_ID`, `ASSET_NAME`, `TYPE`, `AMENITIES`, `ADDRESS`, `PHONE`, `URL`

---

## Bonus: CKAN datasets we are NOT currently loading

Pulled `package_list` from Toronto Open Data CKAN API (538 total packages), filtered to keywords
(`property`, `parcel`, `zoning`, `dwelling`, `tax`, `land`, `rental`, `rooming`, etc.), excluded the ~32 datasets already in our pipeline.
Showing the keyword-relevant subset to scour for unused signal possibilities.

### `credit-balance-property-tax`
- **Title:** Credit Balance - Property Tax
- **Description:** Parcel ID - Assessment Roll Number    Property Address - Property Address     Credit Range - The credit range identifies an approximation of money the City owes a property owner or previous property
- **Tags:** credits, property tax, refund

### `current-value-assessment-cva-tax-impact-residential-properties`
- **Title:** Current Value Assessment (CVA)  Tax Impact Residential Properties
- **Description:** This outlines the current tax year phased-in average Current Value Assessment (CVA) tax impact on residential properties. This is a geographical shape file outlining the polygons by wards and sub-divi
- **Tags:** current value assessment, cva, cva tax

### `municipal-land-transfer-tax-revenue-summary`
- **Title:** Municipal Land Transfer Tax Revenue Summary
- **Description:** See "Municipal Land Transfer Tax Revenue Summary Readme" File   City of Toronto revenues from the Municipal Land Transfer Tax per month beginning in year 2009. The City of Toronto tracks the revenue
- **Tags:** land transfer tax, municipal land trasnfer tax

### `small-business-property-tax-subclass-eligible-properties`
- **Title:** Small Business Property Tax Subclass: Eligible Properties
- **Description:** On November 9, 2021, City Council approved a 15 per cent reduction in the tax rate for small businesses who meet the eligibility criteria for the new small business property tax subclass. This dataset

### `tax-rebates-tax-exemptions`
- **Title:** Tax Rebates - Tax Exemptions
- **Description:** This data is retired and will no longer be updated. For more information, please see [here](https://www.toronto.ca/services-payments/property-taxes-utilities/property-tax/property-tax-water-solid-wast
- **Tags:** charity rebate, ethno-cultural centre rebate, heritage rebate, tax exemption, tax rebate, veteran clubhouse rebate

### `city-initiated-appeals-to-property-assessment`
- **Title:** City Initiated Appeals To Property Assessment
- **Description:** This dataset identifies properties where, as a result of City staff review and analysis, Revenue Services Division has initiated assessment appeals at the Assessment Review Board, under Section 40 of 
- **Tags:** appeal, property assessment, property assessment appeal

### `2013-street-needs-assessment-results`
- **Title:** 2013 Street Needs Assessment Results
- **Description:** The Street Needs Assessment survey was conducted by City staff, community partner agencies and volunteers on April 17th, 2013. Just under two thousand individuals experiencing homelessness provided re
- **Tags:** homeless, housing, point-in time count, street needs assessment

### `2018-street-needs-assessment-results`
- **Title:** 2018 Street Needs Assessment Results
- **Description:** The Street Needs Assessment (SNA) is a survey and point-in-time count of people experiencing homelessness in Toronto on April 26, 2018. The results provide a snapshot of the scope and profile of the C
- **Tags:** homeless, housing, point-in time count, street needs assessment

### `2021-street-needs-assessment-results`
- **Title:** 2021 Street Needs Assessment Results
- **Description:** The Street Needs Assessment (SNA) is a survey and point-in-time count of people experiencing homelessness in Toronto conducted in April, 2021 led by the City’s Toronto Shelter and Support Services (TS

### `demolition-and-replacement-of-rental-housing-units`
- **Title:** Demolition and Replacement of Rental Housing Units
- **Description:** This dataset contains information pertaining to projects that have been approved since January 1, 2018 for the demolition and replacement of six or more rental units, including affordable, mid-range a
- **Tags:** Affordable, Housing, demolition, planning, rental, replacement, severance, units

### `elections-subdivisions`
- **Title:** Elections Subdivisions
- **Description:** Polygon shapefiles that depicts the Election Polling Subdivisions for the 2023 Mayoral By-election, 2022, 2018, 2014, 2010 and 2006 General Municipal Elections for the City of Toronto. The files work 
- **Tags:** elections, voting subdivisions

### `preliminary-zoning-reviews`
- **Title:** Preliminary Zoning Reviews
- **Description:** This data considers 4 different programs supported by Toronto Building: Business Licenses, Liquor Licenses, Zoning use, Zoning Certificates, and preliminary Zoning Reviews for Signs.  **Zoning Certifi
- **Tags:** building application, building permit

### `short-term-rental-program-data`
- **Title:** Short Term Rental Registration & Enforcement overview
- **Description:** In Toronto, short-term rentals are regulated by the Toronto Municipal Code Chapter 547, Licensing and Registration of [Short-Term Rentals](https://www.toronto.ca/community-people/housing-shelter/short
- **Tags:** airbnb

### `short-term-rentals-registration`
- **Title:** Short Term Rentals Registration
- **Description:** The short-term rental regulation is based on Toronto Municipal Code Chapter 547, Licensing and Registration of [Short-Term Rentals](https://www.toronto.ca/community-people/housing-shelter/short-term-r

### `taxicab-stand-locations`
- **Title:** Taxicab Stand Locations
- **Description:** See Readme in Shapefile and Attrbutes in Excel  The taxicab stand locations were verified by Municipal Licensing and Standards Officers. These stands are located within the City of Toronto boundaries.
- **Tags:** taxi, taxicab stand

### `wellbeing-toronto-demographics-taxfiler-indicators`
- **Title:** Wellbeing Toronto - Demographics: TaxFiler Indicators
- **Description:** This data set contains three worksheets. The full description for each column of data is available in the first worksheet called "Legend".  Caveat Emptor: Discrepancies in taxfiler submissions and dis
- **Tags:** demographic, tax filer

### `library-branch-space-rentals`
- **Title:** Library Branch Space Rentals
- **Description:** This dataset lists Toronto Public Library spaces that are available for the public to rent including the size of the spaces and seating capacity. As well as providing names, the corresponding branch 
- **Tags:** library, tpl

### `apartment-building-registration`
- **Title:** Apartment Building Registration
- **Description:** The Apartment Building Standard (ABS) program is based on a new bylaw Chapter 354, which defines formal criteria to identify all rental apartment buildings in the city with 3 or more storeys and 10 or

### `address-points-municipal-toronto-one-address-repository`
- **Title:** Address Points (Municipal) - Toronto One Address Repository
- **Description:** The One Address Repository data set provides a point representation for over 500,000 addresses within the City of Toronto. Each address point is described with a series of attributes including street 
- **Tags:** address, toronto addresses

### `apartment-building-evaluation`
- **Title:** Apartment Building Evaluation
- **Description:** RentSafeTO: Apartment Building Standards is a bylaw enforcement program established in 2017 to ensure that owners and operators of apartment buildings with three or more storeys or 10 or more units co

### `building-construction-demolition-violations`
- **Title:** Building Construction/Demolition Violations
- **Description:** This data set includes all Open and Closed building construction/demolition violation folders.
- **Tags:** building permit, cleared permit

### `building-permits-cleared-permits`
- **Title:** Building Permits - Cleared Permits
- **Description:** Provides information on Building Permits completed/closed.       A building permit is a municipally issued permit, required by the Building Code Act and enforced by the City of Toronto, associated w
- **Tags:** building permit, cleared permit

### `building-permits-green-roofs`
- **Title:** Building Permits - Green Roofs
- **Description:** The Green Roof By-law was adopted by Toronto City Council May 2009 under the authority of section 108 of the City of Toronto Act. Toronto is the first city in North America to have a by-law requirin
- **Tags:** building permit, green roof

### `building-permits-pool-enclosures`
- **Title:** Building Permits - Pool Enclosures
- **Description:** See Toronto Building - Pool Permits readme.xls  Dataset may not be complete because of limitations of selection criteria.
- **Tags:** building permit, pool, pool enclosure, pool permit

### `building-permits-signs`
- **Title:** Building Permits - Signs
- **Description:** Dataset contains information on all Sign Permit applications received on or after September 7th, 2010, including geographical locations of signs and attributes and status of applications.    Dat
- **Tags:** building permit, sign application, signs

### `building-permits-solar-hot-water-heaters`
- **Title:** Building Permits - Solar Hot Water Heaters
- **Description:** See Solar Water Heater Permits readme.xls  Ability to specifically identify this type of application was introduced on May 1, 2009. Earlier applications might also fit into this category, but are 
- **Tags:** permits, solar, water heater

### `chinatown-tomorrow-planning-initiative-consultation`
- **Title:** Chinatown Tomorrow Planning Initiative Consultation
- **Description:** As part of the Chinatown Planning Study (www.toronto.ca/chinatownstudy), extensive community consultation has been conducted over the past 1.5 years. To ensure transparency, accuracy and built relatio

### `city-council-and-committees-meeting-schedule-reports`
- **Title:** City Council and Committees Meeting Schedule Reports
- **Description:** This open data set provides access to meeting schedule data extracted from TMMIS. You can also access previous term meeting schedule data. City Council approves an annual meeting schedule for City Co
- **Tags:** city council, council meeting, council schedule

### `community-planning-boundaries`
- **Title:** Community Planning Boundaries
- **Description:** This dataset contains an ESRI shapefile of all the community planning boundaries of each planning section in the City of Toronto. Manager's name and telephone number is available for each associated p
- **Tags:** city planning, community planning, planners

### `council-and-standing-committee-meeting-statistics`
- **Title:** Council and Standing Committee Meeting Statistics
- **Description:** These datasets contains meeting data for Council, Community Council and Standing Committee meetings for the full 2010-2014 term of Council and the 2014-2018 term of Council to the end of the most rece
- **Tags:** council, standing committee meeting

### `development-applications`
- **Title:** Development Applications
- **Description:** This dataset lists all currently active (open) and inactive (closed) Community Planning applications and Committee of Adjustment applications received by the City between January 1st 2008 till present

### `hr-number-of-job-applications-received-per-external-advertisement`
- **Title:** HR Number of Job Applications Received per External Advertisement
- **Description:** **This data set is no longer maintained. You will find the new data [here](https://open.toronto.ca/dataset/toronto-s-dashboard-key-indicators/).**  See "External Job Postings and Applications Readme

### `hr-total-general-job-applications-submitted-online`
- **Title:** HR Total General Job Applications Submitted Online
- **Description:** **This data set is no longer maintained. You will find the new data [here](https://open.toronto.ca/dataset/toronto-s-dashboard-key-indicators/).**  See "Total General Job Applications Submitted Onli

### `inventory-of-applications`
- **Title:** Inventory of Applications
- **Description:** * Application Name   * Application Description   * Primary Application Business Capability   * Secondary Application Business Capability   * Other Application Business Capabilities    This dataset con
- **Tags:** applications, systems

### `land-ambulance-response-time-standard`
- **Title:** Land Ambulance Response Time Standard
- **Description:** The legislative response time standard submission timeline requirements are established by [Regulation 257/00 Part VIII](https://www.ontario.ca/laws/regulation/000257#BK9) under the Ambulance Act.  
- **Tags:** ambulance, emergency, paramedic

### `municipal-licensing-and-standards-business-licences-and-permits`
- **Title:** Municipal Licensing and Standards - Business Licences and Permits
- **Description:** **Category -** Type of licence or permit    **Licence No -** Number of licence issued by City of Toronto    **Operating Name -** Name that company operates under    **Issued -** Date of issue of
- **Tags:** busincess licence, business permit

### `noise-exemption-permits`
- **Title:** Noise Exemption Permits
- **Description:** The Noise Bylaw provides standards for noise in Toronto. This includes decibel limits and time restrictions for some types of noise. Individuals and organizations can apply for a noise exemption per
- **Tags:** allowed, disturbance, event, exemption, licence, loud, noise, permission

### `on-street-permit-parking-area-maps`
- **Title:** On-Street Permit Parking Area Maps
- **Description:** **FID -** System field  **Feature Id Shape -** Feature Type (polygon)  **Area_id -** Parking permit area identifier  Permit parking areas within the City of Toronto are presented in map format.
- **Tags:** maps, on-street, parking, permit

### `registered-residential-non-residential-condominiums`
- **Title:** Registered Residential & Non-Residential Condominiums
- **Description:** This bulletin reports on Toronto’s condominium development trends since 2002 including units built and proposed through development applications. The dataset provides an inventory of both residential 

### `report-request-log-city-council-and-its-committees`
- **Title:** Report Request Log – City Council and its Committees
- **Description:** The City Clerk's Office maintains a log of report requests made by City Council and its committees. The log is intended for reference and convenience purposes.   **What you'll see in the log**  Re

### `solid-waste-management-services-transfer-station-locations`
- **Title:** Solid Waste Management Services - Transfer Station Locations
- **Description:** **FID -** system field, record identifier    **SHAPE -** system field, file type indentifier    **ADDRESS -** street address of transfer station    **NAME -** name of transfer station  The dat
- **Tags:** garbage depot, transfer station, waste depot

### `special-committee-on-governance-consultation-with-neighbourhood-associations-workshop`
- **Title:** Special Committee on Governance -- Consultation with Neighbourhood Associations  -- Workshop
- **Description:** At their meeting on April 12, 2019, the Special Committee on Governance asked staff to provide targeted engagement opportunities for neighbourhood associations across Toronto to reflect on the impacts

### `sports-recreation-program-enrollment-drop-in-usage-and-permit-activity-summary`
- **Title:** Sports & Recreation - Program Enrollment, Drop-In Usage and Permit Activity Summary
- **Description:** **This dataset has been retired and will not be updated. Please see [Toronto's Dashboard Key Indicators](https://open.toronto.ca/dataset/toronto-s-dashboard-key-indicators/) for more accurate data.**
- **Tags:** drop-in, program enrollment, recreation, sports

### `temporary-extension-and-special-occasion-permit-endorsements-for-liquor-licences`
- **Title:** Temporary Extension and Special Occasion Permit Endorsements for Liquor Licences
- **Description:** All temporary liquor licence applications that have been endorsed as municipally significant by the City of Toronto are shown on the map below.  Search the records by location (premise), applicant, 
- **Tags:** 4am, Extensions, Liquor, Permits, SOP, Temporary

### `toronto-island-ferry-ticket-counts`
- **Title:** Toronto Island Ferry Ticket Counts
- **Description:** Ferries to [Toronto Island Park](https://www.toronto.ca/explore-enjoy/parks-gardens-beaches/toronto-island-park) operate year-round out of Jack Layton Ferry Terminal. Ferries carry passengers to and f
- **Tags:** Ferry

### `utility-cut-permits`
- **Title:** Utility Cut Permits
- **Description:** The addresses and intersections provided in the file are references to the approximate locations of the Utility Cuts and do not represent the actual / specific locations. For more information,  please
- **Tags:** permit, utility cut
