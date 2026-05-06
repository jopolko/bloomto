# BloomTO ETL — `tools/`

Offline Python ETL that produces `data/neighborhoods.json` from Toronto Open Data.
**This script runs on a workstation, not on the VPS.** See `## Do Not Run On VPS` (added in Task 19).

The runtime site (`index.html`, `geocode-proxy.php`) is unaffected by this directory — it only reads the JSON the ETL produces.

## Sources

| Source              | CKAN package                | Resource id                            | Module                  |
|---------------------|-----------------------------|----------------------------------------|-------------------------|
| Neighborhoods       | `neighbourhoods`            | `0719053b-28b7-48ea-b863-068823a93aaa` | `sources/neighborhoods.py` |
| Tree canopy         | `forest-and-land-cover`     | `69419e11-2dfa-4bcc-bed0-43a9dd2d0973` | `sources/canopy.py`     |
| SolarTO             | `solarto`                   | `f5f37d23-85c9-4af8-b8a5-369523778f93` | `sources/solar_to.py`   |
| Census (NPP 2021)   | `neighbourhood-profiles`    | `19d4a806-7385-4889-acf2-256f1e079060` | `sources/census.py`     |
| TTC GTFS            | `ttc-routes-and-schedules`  | `cfb6b2b8-6191-41e3-bda1-b175c51148cb` | `sources/ttc.py`        |
| Cycling network     | `cycling-network`           | `023da9a2-8848-4e10-9cad-e7f9119cd874` | `sources/cycling.py`    |
| Centreline (streets)| `toronto-centreline-tcl`    | `7bc94ccf-7bcf-4a7d-88b1-bdfc8ec5aaf1` | `sources/streets.py`    |
| Zoning by-law       | `zoning-by-law`             | `d75fa1ed-cd04-4a0b-bb6d-2b928ffffa6e` | `sources/zoning.py`     |
| Property boundaries | `property-boundaries`       | `4d4943a6-98ec-4442-9ced-f600f5bc8d27` | `sources/zoning.py`     |
| Heritage Register   | `heritage-register`         | `108b1080-d048-439f-a9e8-e8d6cd81bddb` | `sources/heritage.py`   |
| Building Outlines   | `topographic-mapping-building-outlines` | `41372651-b2eb-4f1e-91d9-b5280b2f0ccd` | `sources/building_outlines.py` |
| 3D Massing          | `3d-massing`                | `667237d6-4d3c-4cf3-8cb7-e91c48d59375` | `sources/massing.py`    |

### Canopy Source

- **Dataset:** [Forest and Land Cover](https://open.toronto.ca/dataset/forest-and-land-cover/) (CKAN slug `forest-and-land-cover`)
- **Package id:** `61642048-56bb-4050-b7c3-f569fcf94527`
- **Resource used:** `2018 Tree Canopy Study Geodatabase`
  - **Resource id:** `69419e11-2dfa-4bcc-bed0-43a9dd2d0973`
  - **Format:** ZIP archive containing an Esri File Geodatabase (`landcover2018_gdb.zip` → `*.gdb/`)
  - **Size:** ~436 MB compressed
  - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/61642048-56bb-4050-b7c3-f569fcf94527/resource/69419e11-2dfa-4bcc-bed0-43a9dd2d0973/download/landcover2018_gdb.zip`
- **Locked format decision: VECTOR (Path A in design Component 4).** The GDB ships as a single vector layer `LandCover2018` of 1,812 multipolygons in NAD83(CSRS) / MTM zone 10 (EPSG:2952), pre-dissolved by (source-hood × land-cover class). Each polygon carries `Shape_Area` in source-CRS m² — exact area for the patch — so the canopy share is a SUM/SUM, not a pixel count. The dataset is *not* distributed as a raster despite the name "Tree Canopy Study"; rasterizing it would only introduce edge-aliasing error against an already-exact representation.
- **Schema (relevant fields):** `gridcode` (Integer, land-cover class), `Desc` (String, class name), `Shape_Area` (Real, m² in MTM zone 10). Class table:

  | gridcode | Desc     |
  |----------|----------|
  | 0        | (unclassified / no data) |
  | 1        | tree     |
  | 2        | grass    |
  | 3        | bare     |
  | 4        | water    |
  | 5        | building |
  | 6        | road     |
  | 7        | other    |
  | 8        | shrub    |

  Tree canopy = `gridcode == 1`. (Shrub `gridcode == 8` is *not* counted as canopy.)
- **Aggregation approach (Task 13.2):**
  1. Download + unzip the GDB once into `tools/cache/`.
  2. Run `ogr2ogr -t_srs EPSG:4326 -dialect SQLite -sql "SELECT gridcode, Shape_Area, ST_PointOnSurface(Shape) AS geom FROM LandCover2018" -nlt POINT -f GeoJSON ...` to extract a small WGS84 GeoJSON of one representative point per source polygon plus its `gridcode` and `Shape_Area`. Cached as `canopy_centroids_4326.geojson` (~330 KB). One-time cost ~1–2 min on the first run; subsequent runs reuse the cache.
  3. Build an `STRtree` over the 158 canonical neighborhood polygons; for each centroid, find the canonical hood that contains it, attribute `Shape_Area` to that hood (and to its `tree_m²` if `gridcode == 1`).
  4. Per hood: `canopy = round(100 × tree_m² / total_landcover_m²)`, clamped to [0, 100]. Hoods with zero attributed area fall back to `canopy = 30` (≈ Toronto-wide average); in practice every canonical hood gets non-zero coverage.
- **Why centroids, not full geometries:** dumping all 1,812 multi-part polygons as GeoJSON balloons to ~4 GB (vertex counts are very high) and `json.load` blows past available memory. We don't need the geometries — `Shape_Area` is precomputed in the source and a representative point is sufficient for hood attribution (same approach as `solar_to.py` / `zoning.py`). `ST_PointOnSurface` is preferred over `ST_Centroid` because the centroid of a multi-part polygon may fall outside all parts.
- **Why no `gdal_translate` to GeoTIFF:** an earlier plan was to convert the GDB to a raster GeoTIFF and zonal-stat it with `rasterio.mask.mask`. The dataset turned out to be vector, so that path was retired before any code shipped — vector aggregation is both cheaper (~330 KB intermediate vs. multi-GB raster, seconds of compute vs. minutes) and strictly more accurate (no resampling artifacts).
- **Operator dependency: GDAL ≥ 3.7 on PATH.** The ETL invokes `ogr2ogr` once per cold cache via `subprocess`. Ubuntu 22.04's stock package is GDAL 3.4.1, which is too old for `ST_PointOnSurface` to behave correctly with FileGDB inputs in the SQLite dialect. On the dev machine GDAL 3.12 is installed via conda-forge under `/opt/micromamba/envs/gdal/`, with `ogr2ogr` symlinked into `/usr/local/bin/`; if you're running the ETL elsewhere, `conda install -c conda-forge gdal` (or any GDAL ≥ 3.7) is the path.
- **Other notes:**
  1. **Disk + bandwidth:** 436 MB compressed (~621 MB unzipped GDB, plus ~330 KB extracted GeoJSON). Cached under `tools/cache/`; subsequent runs reuse everything. The cache is gitignored.
  2. **Coverage:** ~98% of source polygons attribute to a canonical hood by representative point. The ~2% loss is shoreline/inlet edge cases or polygons whose source-hood scheme straddles the 158-hood boundaries; in tests this is invisible to rounded percentages. If a future audit needs it, swap rep-point attribution for an intersect-and-area-allocate pass.

### SolarTO Source

- **Dataset:** [SolarTO](https://open.toronto.ca/dataset/solarto/) (CKAN slug `solarto`)
- **Package id:** `304aaee3-11cb-41c4-96a3-adef31f63ed5`
- **Resource used:** `solarto-map - 4326.csv` (WGS84 / EPSG:4326)
  - **Resource id:** `f5f37d23-85c9-4af8-b8a5-369523778f93`
  - **Format:** CSV with embedded GeoJSON `MultiPolygon` per row in the `geometry` column
  - **Size:** ~293 MB
  - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/304aaee3-11cb-41c4-96a3-adef31f63ed5/resource/f5f37d23-85c9-4af8-b8a5-369523778f93/download/solarto-map-4326.csv`
- **Why CSV (not GeoJSON or GPKG):** the GeoJSON resource is 566 MB; the GPKG is 214 MB but requires `fiona`. The CSV is 293 MB, parses with stdlib `csv` streaming, embeds geometry as a GeoJSON `MultiPolygon` string per row that `json.loads` + `shapely.geometry.shape` can decode without any extra dependency. Memory stays bounded because the ETL processes row-by-row.
- **Schema (relevant columns):** `objectid`, `structureid` (building address; often `Building Address Not Found`), `rooftop_sqft`, **`annual_electricity_generation_k`** (annual kWh; this is the magnitude field used for ranking), `system_size`, `system_cost`, `payback_period`, `annual_ghg_reduction_kg`, `geometry` (GeoJSON `MultiPolygon` of the roof footprint).
- **No categorical "high solar potential" classification field exists.** Per the design's fallback rule (Req 3.3), the aggregation uses the share of rooftops above the **75th percentile of `annual_electricity_generation_k`** (computed once across the full city dataset, then applied per-neighborhood).
- **Aggregation approach (Task 12):**
  1. Stream the CSV row by row; skip rows where `annual_electricity_generation_k` is empty (those rows have no system-size estimate and contribute nothing).
  2. Compute the 75th-percentile threshold for `annual_electricity_generation_k` across all valid rows.
  3. For each row, decode the `geometry` column with `json.loads` + `shapely.geometry.shape`, take the polygon's representative point (`shape.representative_point()` is cheaper and topology-safe vs `centroid` for irregular roof shapes).
  4. Run point-in-polygon against the neighborhood `STRtree` (built once) → assign each rooftop to a neighborhood.
  5. Per neighborhood: `heat_pump = round(100 * count_above_threshold / count_total)`. Neighborhoods with zero rooftops fall back to 50 (neutral) and are listed in `meta.solarToFallbacks`.
- **Risks to flag during Task 12:**
  1. **Missing addresses (`Building Address Not Found`):** common; does not affect the spatial join, since geometry is intact. No action needed.
  2. **Multi-polygon roofs:** `representative_point()` handles `MultiPolygon` correctly; `centroid` can fall outside the polygon for L-shaped roofs.
  3. **293 MB stream:** download once, cache under `tools/cache/solar_to.csv`. Don't materialize the whole CSV in memory; iterate with `csv.DictReader`.

### Census Source

- **Dataset:** [Neighbourhood Profiles](https://open.toronto.ca/dataset/neighbourhood-profiles/) (CKAN slug `neighbourhood-profiles`)
- **Package id:** `6e19a90f-971c-46b3-852c-0c48c436d1fc`
- **Resource used:** `neighbourhood-profiles-2021-158-model`
  - **Resource id:** `19d4a806-7385-4889-acf2-256f1e079060`
  - **Format:** XLSX
  - **Size:** ~1.7 MB
  - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/6e19a90f-971c-46b3-852c-0c48c436d1fc/resource/19d4a806-7385-4889-acf2-256f1e079060/download/nbhd_2021_census_profile_full_158model.xlsx`
- **Sheet:** `hd2021_census_profile` (the workbook also contains a 159-row `Nbhdmetadata` sheet that the ETL ignores).
- **Layout:** row 1 = header with 158 neighborhood names in columns B–OF; row 2 = neighborhood number; row 3 = TSNS designation; rows 4+ = data rows whose label sits in column A and whose 158 values fill columns B–OF. Sheet dimensions: 2604 rows × 159 columns. Look up data rows **by row label**, not row index — labels are stable across vintages, indices are not.

**Row labels used by `tools/sources/census.py`:**

| Field | Row label (column A) | Row index in this vintage |
|---|---|---|
| `existing` (Total occupied private dwellings) | `Total - Occupied private dwellings by structural type of dwelling - 25% sample data` | 217 |
| Period-of-construction universe (denominator) | `Total - Occupied private dwellings by period of construction - 25% sample data` | 326 |
| Period bracket 1 | `  1960 or before` (note 2-space indent) | 327 |
| Period bracket 2 | `  1961 to 1980` | 328 |
| Period bracket 3 | `  1981 to 1990` | 329 |
| Period bracket 4 | `  1991 to 2000` | 330 |
| Period bracket 5 | `  2001 to 2005` | 331 |
| Period bracket 6 | `  2006 to 2010` | 332 |
| Period bracket 7 | `  2011 to 2015` | 333 |
| Period bracket 8 | `  2016 to 2021` | 334 |

The leading whitespace is part of the label as stored in the XLSX (the workbook uses indentation to express the period-of-construction sub-tree). The source module should match labels via `.strip()` on column A, not by literal equality, to avoid breakage if Statistics Canada changes the indentation in a future republish.

**Bracket → midpoint year:**

| Bracket | Midpoint year |
|---|---|
| `1960 or before` | 1955 |
| `1961 to 1980` | 1970 |
| `1981 to 1990` | 1985 |
| `1991 to 2000` | 1995 |
| `2001 to 2005` | 2003 |
| `2006 to 2010` | 2008 |
| `2011 to 2015` | 2013 |
| `2016 to 2021` | 2018 |

The first bracket is open-ended; 1955 is a pragmatic stand-in given the bulk of Toronto's pre-1961 housing stock dates from 1920–1960. The aggregation is robust to this choice — `builtYear` is reported per neighborhood as the bracket midpoint where the cumulative dwelling share first crosses 50%, so the 1955 value is only used by neighborhoods whose median dwelling predates 1961 (a small minority).

**Aggregation approach (Task 31):** for each neighborhood column, read counts from rows 327–334, divide by row 326's value to get the bracket distribution as percentages, accumulate from oldest to newest, and pick the bracket where cumulative ≥ 50% — return its midpoint year as `builtYear`. Read `existing` directly from row 217. Note row 217 (structural type) and row 326 (period of construction) report nearly identical totals because both are 25% sample universes; row 217 is the chosen single-source-of-truth for `existing`.

**Name normalization vs. AREA_NAME (Task 11):** the XLSX header row uses neighborhood names that differ cosmetically from the GeoJSON `AREA_NAME` field for **7 of 158** neighborhoods. These are punctuation/whitespace differences only — same neighborhoods — so the source module resolves them via an alias map rather than treating them as fallbacks. Names not covered by the alias map and not equal verbatim to an AREA_NAME *are* real fallback candidates and contribute to `meta.fallbacks["census"]`.

| XLSX header | GeoJSON `AREA_NAME` (canonical) |
|---|---|
| `Cabbagetown-South St. James Town` | `Cabbagetown-South St.James Town` |
| `Danforth-East York` | `Danforth East York` |
| `East End Danforth` | `East End-Danforth` |
| `North St. James Town` | `North St.James Town` |
| `` O`Connor Parkview `` | `O'Connor-Parkview` |
| `Taylor Massey` | `Taylor-Massey` |
| `Yonge-St. Clair` | `Yonge-St.Clair` |

The remaining 151 names match verbatim. Investigation queried both sets at HEAD on 2026-05-01 — re-verify the alias map if the City republishes either resource.

**Risks to flag during Task 31:**

1. **`read_only=True` is mandatory** — full-mode `load_workbook` on this file consumes ~150 MB; read-only mode keeps it under 30 MB and streams cells.
2. **Sample rounding noise:** Statistics Canada randomly rounds 25% sample counts to multiples of 5; the sum of rows 327–334 may diverge from row 326 by up to ±20. Use row 326 as the denominator (not the sum) so cumulative shares stay monotone.
3. **Suppressed cells:** dwelling counts below the suppression threshold appear as `0` (not `None`). This is indistinguishable from a true zero; small-population neighborhoods may produce skewed `builtYear` values. None of the 158 neighborhoods are below the suppression threshold for total dwellings as of the 2021 vintage, so this risk is theoretical.
4. **Bracket boundary republish:** if a future Census splits `2016 to 2021` into two finer brackets, both the row-label list and the midpoint table need updating. The source module should hard-fail (not silently skip) on an unrecognized bracket label.

### Transit Source

- **Dataset:** [TTC Routes and Schedules](https://open.toronto.ca/dataset/ttc-routes-and-schedules/) (CKAN slug `ttc-routes-and-schedules`)
- **Package id:** `7795b45e-e65a-4465-81fc-c36b9dfff169`
- **Resource used:** `TTC Routes and Schedules Data` (the only resource on the package — a single GTFS-static ZIP that the TTC republishes weekly)
  - **Resource id:** `cfb6b2b8-6191-41e3-bda1-b175c51148cb`
  - **Format:** ZIP (GTFS-static bundle: `agency.txt`, `calendar.txt`, `calendar_dates.txt`, `routes.txt`, `shapes.txt`, `stops.txt`, `stop_times.txt`, `trips.txt`)
  - **Size:** ~34 MB
  - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/7795b45e-e65a-4465-81fc-c36b9dfff169/resource/cfb6b2b8-6191-41e3-bda1-b175c51148cb/download/opendata_ttc_schedules.zip`
- **Parser approach (Task 32):** stdlib `zipfile.ZipFile` opens the cached bundle without extracting; `csv.DictReader` (wrapped in `io.TextIOWrapper(..., encoding='utf-8-sig')` to strip the BOM) streams `stops.txt` row by row. Only `stops.txt` is read — `stop_times.txt` is 200 MB and not needed for stop-density. Each row provides `stop_lat`, `stop_lon`; build a `shapely.Point` per row.
- **`stops.txt` shape:** 9,378 rows; columns `stop_id, stop_code, stop_name, stop_desc, stop_lat, stop_lon, zone_id, stop_url, location_type, parent_station, stop_timezone, wheelchair_boarding`. Lat range 43.59–43.91, lon range −79.65 to −79.12 — covers the full Toronto bbox plus a small fringe in 905-area municipalities. **Every row has empty `location_type` and empty `parent_station`** — the feed treats every entry as a regular boardable stop, so no platform/entrance/station filtering is required.
- **Buffer radius decision: 200m.** A strict 0m containment misses stops sitting on the curb just outside the polygon edge, which over-penalizes waterfront strips and other narrow neighborhoods. 200m matches the typical block-and-a-half walk that pedestrians actually use to access transit, and matches the Toronto Walk Score documentation's "transit walkshed" convention. In WGS84 degrees at 43.7°N, 200m ≈ 0.0018° (the ETL applies this as a uniform lat/lon buffer on the polygon — slight east-west stretch is acceptable for ranking purposes since it's applied uniformly to all 158 polygons).
- **Aggregation approach (Task 32):** build `STRtree` over the 158 buffered polygons (one polygon per neighborhood, buffered by 0.0018°); for each stop's point, query candidate polygons, then verify with `polygon.contains(point)`. Per neighborhood: count stops; normalize the count by the 95th-percentile across all 158 (any neighborhood at or above the 95th-percentile gets `transit = 100`).
- **Risks to flag during Task 32:**
  1. **905-area stops:** stops in Vaughan / Markham / Mississauga (lat > 43.85 or lon outside the Toronto-proper bbox) won't intersect any neighborhood polygon and silently drop out of the count — this is the desired behavior (no over-counting) but worth a sanity-check log line in Task 32.
  2. **Weekly republish:** the resource updates every Friday with the next week's schedule. Cache invalidation in Task 32 is not automatic — the operator deletes `tools/cache/ttc_gtfs.zip` to force a refetch.
  3. **`stops.txt` BOM:** the file is UTF-8-with-BOM; using `encoding='utf-8'` (no `-sig`) leaves a stray `﻿` glued to the first column name, which silently breaks the `DictReader` lookup for `stop_id`. Always use `utf-8-sig`.

### Cycling Source

- **Dataset:** [Cycling Network](https://open.toronto.ca/dataset/cycling-network/) (CKAN slug `cycling-network`)
- **Package id:** `abbe5ee3-e249-4f86-a219-f0022eaddcc9`
- **Resource used:** `cycling-network - 4326.geojson` (WGS84 / EPSG:4326)
  - **Resource id:** `023da9a2-8848-4e10-9cad-e7f9119cd874`
  - **Format:** GeoJSON (`FeatureCollection`, all features `MultiLineString`)
  - **Size:** ~3.1 MB
  - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/abbe5ee3-e249-4f86-a219-f0022eaddcc9/resource/023da9a2-8848-4e10-9cad-e7f9119cd874/download/cycling-network-4326.geojson`
  - **Last modified:** 2026-03-16
- **Why GeoJSON 4326 (not Shapefile, GPKG, or 2952 projected):** the WGS84 GeoJSON is the smallest direct-parse-with-stdlib option; Shapefile needs `fiona`, GPKG needs `fiona` or a GDAL build with SQLite. The 2952 (NAD83 / MTM Zone 10) variants would skip a reprojection step, but the source neighborhoods are already in WGS84 so going through 4326 avoids any CRS bookkeeping inside the source module.
- **Schema:** 1,538 features. Properties include `SEGMENT_ID`, `INSTALLED` (year), `UPGRADED`, `STREET_NAME`, `FROM_STREET`, `TO_STREET`, `ROADCLASS`, `CNPCLASS` (Cycling Network Plan classification — bike lane / cycle track / multi-use trail / etc.), `INFRA_LOWORDER`, `INFRA_HIGHORDER`. The metric only needs geometry length, so the attributes serve QA and future per-class weighting.
- **Length-aggregation approach (Task 33):** for each `MultiLineString`, intersect with each candidate neighborhood polygon (use `shapely.STRtree` over the 1,538 line geoms for fast neighborhood lookup, then verify intersection per pair). Measure each intersection's geodesic length via `pyproj.Geod(ellps='WGS84').geometry_length(intersection_geom)` — this returns metres directly, avoiding any equal-area projection bookkeeping. Sum metres per neighborhood and divide by 1000 to get km.
- **Normalization (Task 33):** divide each neighborhood's km by the **95th-percentile across all 158**, multiply by 100, clamp to 0–100. The 95th-percentile (rather than max) prevents a single outlier neighborhood from compressing the rest of the distribution.
- **Risks to flag during Task 33:**
  1. **Edge segments crossing two neighborhoods:** `STRtree` returns candidates; the intersection with a polygon that only touches the line at a single point produces a `Point` geometry whose `geometry_length` is 0 — that's the correct outcome. No special handling needed.
  2. **Multi-use trails (Humber, Don, Lower Don, Martin Goodman):** these run along ravine corridors and frequently sit between two neighborhoods rather than inside one. Intersection-based attribution splits the credit correctly without manual intervention.
  3. **`pyproj.Geod.geometry_length` requirement:** this method exists in pyproj ≥ 2.3; pinned `3.7.0` covers it. If a downgrade ever happens, the fallback is iterating coordinate pairs through `Geod.inv()` and summing.

### Streets Source

- **Dataset:** [Toronto Centreline (TCL)](https://open.toronto.ca/dataset/toronto-centreline-tcl/) (CKAN slug `toronto-centreline-tcl`)
- **Package id:** `1d079757-377b-4564-82df-eb5638583bfb`
- **Resource used:** `Centreline - Version 2 - 4326.geojson` (WGS84 / EPSG:4326)
  - **Resource id:** `7bc94ccf-7bcf-4a7d-88b1-bdfc8ec5aaf1`
  - **Format:** GeoJSON
  - **Size:** ~89 MB
  - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/1d079757-377b-4564-82df-eb5638583bfb/resource/7bc94ccf-7bcf-4a7d-88b1-bdfc8ec5aaf1/download/centreline-version-2-4326.geojson`
  - **Last modified:** 2026-04-30
- **Memory strategy:** at 89 MB on disk, full `json.load` materializes ~400–600 MB of Python objects (lat/lon floats are 28 bytes each, and there are millions of them). That fits comfortably on a typical workstation, so Task 34 starts with `json.load`. Only swap to `ijson` if an operator's machine OOMs — at that point, pin `ijson==3.3.0` in `requirements.txt` and stream `features.item`.
- **Intersection definition (Task 34):**
  - Collect all line endpoints across all features (each `LineString` contributes 2 endpoints; each `MultiLineString` contributes 2 per part).
  - Cluster endpoints whose pairwise distance is within a **5×10⁻⁵° (~5 m at 43.7°N) tolerance** using a simple grid hash (round each (lat, lon) to the tolerance and group by the rounded cell).
  - Any cluster of size **≥ 3 distinct line ids meeting at the same cluster cell** is an intersection. (Size ≥ 3 distinguishes a real T/four-way intersection from a simple two-segment continuation, which is just a digitization break in a long road.)
  - Tolerance rationale: TCL segments are clipped at intersections in the source data, so true intersections always have multiple endpoints within sub-metre distance. 5 m is comfortably above the digitization noise floor and below the spacing between adjacent intersections (typically 50–200 m even on dense urban grids).
- **Per-neighborhood metric (Task 34):** count intersections inside each polygon (point-in-polygon via `STRtree` over the polygons), divide by `area_km2` to get density (intersections / km²). Normalize by 95th-percentile density across all 158, multiply by 100, clamp to 0–100. This is the v1.1 stand-in for "Walk Score" — a Walk Score™ trademark caveat is documented in the spotlight tile copy (per Req 4c.4).
- **Risks to flag during Task 34:**
  1. **Memory:** Centreline GeoJSON is the largest file in the pipeline; if `json.load` exceeds 2 GB resident, swap to `ijson` (add `ijson==3.3.0` to `requirements.txt`) and stream `features.item`.
  2. **Cul-de-sacs and stub-ends:** these contribute size-1 endpoint clusters and correctly do *not* count as intersections.
  3. **Highway interchanges / ramps:** `LINEAR_NAME_FULL_LEGAL` filtering could exclude them, but for v1.1 the simplest correct pass is to count every TCL feature. The size-≥-3 rule already discards endpoint stubs at ramp termini.

### Zoning + Property Source

- **Datasets:** [Zoning By-law](https://open.toronto.ca/dataset/zoning-by-law/) (CKAN slug `zoning-by-law`) **+** [Property Boundaries](https://open.toronto.ca/dataset/property-boundaries/) (CKAN slug `property-boundaries`)
- **Zoning By-law:**
  - **Package id:** `34927e44-fc11-4336-a8aa-a0dfb27658b7`
  - **Resource used:** `Zoning Area - 4326.geojson` — the base zone-class layer; the dozens of overlay resources on this package (`Zoning Policy Area Overlay`, `Zoning Height Overlay`, `Parking Zone Overlay`, etc.) are *not* used for the headroom calculation.
    - **Resource id:** `d75fa1ed-cd04-4a0b-bb6d-2b928ffffa6e`
    - **Format:** GeoJSON, 11,719 `MultiPolygon` features
    - **Size:** ~49 MB
    - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/34927e44-fc11-4336-a8aa-a0dfb27658b7/resource/d75fa1ed-cd04-4a0b-bb6d-2b928ffffa6e/download/zoning-area-4326.geojson`
    - **Last modified:** 2026-02-20
  - **Schema (relevant columns):** `ZN_ZONE` (clean zone class — `RD`, `RS`, `RT`, `RM`, `RA`, `CR`, `O`, `ON`, `E`, `UT`, etc.), `ZN_STRING` (decorated form with site-specific exceptions in parentheses, e.g., `RD (f15.0; a550) (x5)`), `GEN_ZONE` (numeric category code), `ZN_HOLDING`, `ZN_EXCPTN`, `ZBL_CHAPT`, `ZBL_SECTN`. Use **`ZN_ZONE`** for multiplier lookup — it's already stripped of the parenthetical exception decorations, so no string surgery is required.
- **Property Boundaries:**
  - **Package id:** `1acaa8b0-f235-4df6-8305-02025ccdeb07`
  - **Resource used:** `Property Boundaries - 4326.geojson` (WGS84)
    - **Resource id:** `4d4943a6-98ec-4442-9ced-f600f5bc8d27`
    - **Format:** GeoJSON
    - **Size:** ~475 MB — the largest file in the pipeline by an order of magnitude.
    - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/1acaa8b0-f235-4df6-8305-02025ccdeb07/resource/4d4943a6-98ec-4442-9ced-f600f5bc8d27/download/property-boundaries-4326.geojson`
    - **Last modified:** 2026-05-01 (republished daily)
  - **Memory strategy:** at 475 MB on disk, full `json.load` materializes 2–3 GB of Python objects — too much for a typical 8 GB workstation alongside Shapely's STRtree and the other source caches. **Task 35 must use `ijson` from the start** (stream `features.item`), not `json.load`. Pin `ijson==3.3.0` in `requirements.txt` when Task 35 lands.
- **Companion config file:** `tools/zoning_multipliers.json` — JSON object mapping each `ZN_ZONE` class string to `{"max_units_per_lot": int, "comment": "..."}`. Non-residential zones map to `0`. Defaults grounded in **City of Toronto By-law 569-2013 (Zoning By-law)** and the **2023 Multiplex Amendment** (which set the citywide floor to 4 units per residential lot):
  - `RD`, `RS`, `RT` (low-density residential — Detached / Semi-detached / Townhouse) → `4` (multiplex floor per the 2023 amendment)
  - `R` (Residential, generic) → `4`
  - `RM` (Residential Multiple Dwelling) → `8`
  - `RA` (Residential Apartment) → `20`
  - `RAC` (Residential Apartment Commercial — mid-rise mixed-use) → `12`
  - `CR` (Commercial-Residential mixed-use) → `8` (residential portion only; conservative since CR FAR varies by site)
  - `CRE`, `CL` (commercial residential employment, commercial local) → `4` (treat as multiplex-eligible mixed-use)
  - Non-residential employment (`E`, `EH`, `EL`, `EO`) / institutional (`I`, `IE`, `IH`, `IPW`, `IS`) / open space (`O`, `ON`, `OR`, `OG`, `OM`, `OC`) / utility (`UT`, `UR`) → `0`
- **Loud-failure on unknown codes (post-zone-class-coverage fix):** the multiplier table is the *complete* set of `ZN_ZONE` codes the ETL recognizes. Both call sites (`compute_potential` for v1.1, `assemble_parcel_payload` for v1.2) route through `tools.sources.zoning.lookup_multiplier`, which raises `KeyError` when an unrecognized code appears. There is no silent default. When Toronto next amends By-law 569-2013 with a new code, the build will fail visibly — extend `tools/zoning_multipliers.json` (and let the `tools/tests/test_zoning_multipliers.py` coverage regression confirm coverage) before re-running. Empty `zone_class` (parcel sits outside any zoning polygon) is *not* an unknown code; `lookup_multiplier` returns `0` for it.
- **Aggregation approach (Task 35):**
  - Stream-parse Property Boundaries via `ijson.items(f, 'features.item')`; for each parcel, decode geometry via `shapely.geometry.shape(feature['geometry'])` and compute `representative_point()` (cheaper and topology-safe vs `centroid` for irregular parcels).
  - Look up the parcel's zoning category via `STRtree` over zone polygons + a containment check on the representative point.
  - Look up the multiplier from `tools/zoning_multipliers.json` keyed on `ZN_ZONE`.
  - Attribute the parcel to a neighborhood via representative-point containment in neighborhood polygons.
  - Per neighborhood: `potential = sum(parcel_multipliers)`. **Defensive floor:** if `potential < existing_by_name[name]`, set `potential = existing_by_name[name]` and log — this prevents the rare case where the zoning model under-counts an apartment-dense neighborhood (e.g., a single RA parcel hosts a 200-unit tower that the multiplier table prices at 20).
- **Why representative-point attribution (not parcel-polygon intersection):**
  - Toronto parcels are small (median ≈ 200 m²); the representative point lies inside the same neighborhood ≥ 99 % of the time.
  - Polygon-intersection attribution would split a single parcel across two neighborhoods at boundary roads — inflating both counts.
- **Risks to flag during Task 30 / Task 35:**
  1. **Property Boundaries size (475 MB):** non-negotiable — must use `ijson` streaming. A naive `json.load` will OOM.
  2. **`ZN_ZONE` already clean:** the parenthetical exception suffixes (`RD (f15.0; a550) (x5)`) live on `ZN_STRING`, not `ZN_ZONE`. No `partition('(')[0]` stripping needed when keying the multiplier table off `ZN_ZONE`.
  3. **Zone-polygon vs parcel mismatch at boundaries:** a parcel's representative point might fall on a road inside a non-residential zone slice even when the parcel itself is residential. Mitigation: if no zone polygon contains the rep point, assign multiplier `0` (conservative) and count the parcel in `meta.fallbacks["zoning"]` rather than crashing.
  4. **Floor invariant:** without the `potential ≥ existing` floor, a rounding-error neighborhood could ship a negative Missing-Middle headroom score. The defensive clamp is non-optional.

### Heritage Source

- **Dataset:** [Heritage Register](https://open.toronto.ca/dataset/heritage-register/) (CKAN slug `heritage-register`)
- **Package id:** `e41da515-5ad1-4bc3-85ea-18ec9e55cd33`
- **Resource used:** `Heritage Register Data` (current vintage; the 2022 sibling resource is ignored)
  - **Resource id:** `108b1080-d048-439f-a9e8-e8d6cd81bddb`
  - **Format:** ZIP archive containing an Esri Shapefile bundle (`HRAP_<YYYYMMDD>_OpenData.shp/.shx/.dbf/.prj/.cpg/.sbn/.sbx/.shp.xml`)
  - **Size:** ~1.5 MB compressed (12,327 records, 41 attribute fields)
  - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/e41da515-5ad1-4bc3-85ea-18ec9e55cd33/resource/108b1080-d048-439f-a9e8-e8d6cd81bddb/download/heritage_register_address_points_wgs84.zip`
  - **Last modified (snapshot):** 2026-04-02 (resource is refreshed quarterly; the URL is stable)
- **Locked geometry decision: POINT, augmented with DBF status + address.** The CKAN-distributed resource is `HRAP_OpenData` — *Heritage Register Address Points*. SHP shape-type code `1` (POINT) confirmed empirically from the file header; CRS `WGS84 / EPSG:4326` confirmed from the `.prj`. Each record is the geocoded centroid of one listed/designated property's primary street address. There is **no polygon distribution**; the design's earlier "polygon intersect" wording (design.md §99/108/176) was corrected to point-in-parcel before this section was written. The heritage-tiered-status spec (2026-05) extended the loader to also read the DBF in-process so each record's `STATUS` and `ADDRESS` come along for the ride.
- **Three legal protection tiers (heritage-tiered-status spec):** the DBF's `STATUS` field splits records into three legally-distinct levels. The ETL canonicalizes them on the wire as `heritageStatus`:
  - **`"part_iv"`** — individually designated by by-law. Demolition prohibited without an OMB hearing. **Hard block** (`heritage_factor = 0.0`, score is zeroed).
  - **`"part_v"`** — inside a Heritage Conservation District. Friction, not blocker — multiplex conversion of contributing buildings is usually approvable, but design review applies. **Discounted** (`heritage_factor = PART_V_HERITAGE_FACTOR = 0.5`, tunable in `tools/parcel_scoring.py`).
  - **`"listed"`** — on the watchlist, not legally designated. Demolition allowed after a 60-day notice. **Lightly discounted** (`heritage_factor = LISTED_HERITAGE_FACTOR = 0.85`, tunable).
  - Per-tier counts surface in `meta.stats` as `heritagePartIV`, `heritagePartV`, `heritageListed`, plus `heritageUnjoined` for records that didn't match any parcel.
- **Loud-failure on unknown DBF `STATUS` values:** if Toronto introduces a fourth tier or renames an existing one, `tools/sources/heritage._iter_records_from_zip` raises `ValueError` naming the unrecognized status. The operator extends `_DBF_STATUS_MAP` (and, if needed, `_HERITAGE_FACTORS` in `tools/parcel_scoring.py`) and re-runs. The `tools/tests/test_heritage_dbf_coverage.py` regression catches the gap at CI time, before the operator kicks off `build_parcels.py`. Same convention as the zone-class-coverage helper.
- **Spatial join: address-match first, point-in-parcel fallback (Req 3).** The orchestrator tries an address match between the heritage DBF's `ADDRESS` and the Property Boundaries' `ADDRESS_NUMBER + LINEAR_NAME_FULL`, normalized via the closed-set `STREET_TYPE_ABBREVIATIONS` table (uppercase, whitespace collapse, `STREET → ST`, `AVENUE → AVE`, etc.). On match, the parcel receives the record's tier *regardless* of whether the geocoded point falls inside its polygon — this fixes false negatives on subdivisions and condos. On miss, the orchestrator falls back to point-in-parcel containment. Multiple records resolving to the same parcel collapse to the strictest tier via `more_restrictive` (Part IV > Part V > Listed). Records that match neither path increment `meta.stats.heritageUnjoined`.
- **Factor tuning (Req 6):** `PART_V_HERITAGE_FACTOR` and `LISTED_HERITAGE_FACTOR` in `tools/parcel_scoring.py` are the only tuning surfaces. After each ETL run, validate the chosen defaults with:
  ```
  python3 tools/validate_heritage_tiers.py
  ```
  The script asserts that each of the top-5 HCDs (South/North Rosedale, three Cabbagetowns) surfaces ≥ 10 chip-on parcels at `score >= 1`. If any HCD falls below the floor, the script recommends lowering `PART_V_HERITAGE_FACTOR` toward 0.6–0.7 in 0.05 increments. It also enforces the Part IV hard-block invariant (zero Part IV parcels with `score > 0`).
- **Schema (relevant DBF fields):**

  | Field        | Type | Use |
  |--------------|------|-----|
  | `ADDRESS`    | C(254) | E.g. `"17  SALISBURY AVE"`. Read into `address_to_status` via `normalize_address`; the join key. |
  | `STATUS`     | C(254) | One of `"Part IV"`, `"Part V"`, `"Listed"`. Canonicalized to `"part_iv"`, `"part_v"`, `"listed"` via `_DBF_STATUS_MAP`; surfaces as `heritageStatus` on the wire. |
  | `LISTED` / `DESIGNATED` | D | ISO dates; informational only, not surfaced. |
  | `HTG_CONSER` | C(254) | Conservation-district name (e.g. `"Cabbagetown-Metcalfe"`). Not on the wire; re-read by `tools/validate_heritage_tiers.py` for HCD attribution. |

  All other 37 fields (folder rows, sequence numbers, ROLL, X/Y in MTM, ward, etc.) are dropped at ingest. The `.dbf` is 80 MB precisely because of these wide fields — they expand the per-record footprint, not the geometry payload.
- **Operator dependency:** `pyshp==3.0.3` (already pinned). Pure-Python wheel, no GDAL.
- **Other notes:**
  1. **Vintage drift:** the resource filename embeds the snapshot date (`HRAP_20260402_OpenData.shp`). The `_http.py` cache uses ETag/If-Modified-Since on the ZIP — the inner filename varies but is discovered at read-time via `zipfile.namelist()` filtering on `*.shp`.
  2. **WGS84 already:** no reprojection needed. The `.prj` is `GCS_WGS_1984` and the geometries are degrees-lon/lat.
  3. **Coverage limit:** the register only lists *primary* address points. A parcel listed under a side-street address that isn't its primary address would be missed — this is a known source-data limitation. Such records will fall through to the point-in-parcel fallback if their geocoded point is inside the parcel; otherwise they appear in `meta.stats.heritageUnjoined`.

### Building Outlines Source

- **Dataset:** [Topographic Mapping – Building Outlines](https://open.toronto.ca/dataset/topographic-mapping-building-outlines/) (CKAN slug `topographic-mapping-building-outlines`)
- **Package id:** `09a930cc-2a52-49b2-866d-52ac7f769a73`
- **Resource used:** `Building Outlines - 4326.csv` (WGS84 / EPSG:4326)
  - **Resource id:** `41372651-b2eb-4f1e-91d9-b5280b2f0ccd`
  - **Format:** CSV with embedded GeoJSON `MultiPolygon` per row in the `geometry` column
  - **Size:** ~273.6 MB (Content-Length 273,631,677 bytes, HEAD-confirmed)
  - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/09a930cc-2a52-49b2-866d-52ac7f769a73/resource/41372651-b2eb-4f1e-91d9-b5280b2f0ccd/download/building-outlines-4326.csv`
  - **Last modified (snapshot):** 2026-04-05 (resource is refreshed quarterly)
- **Why CSV (not the plain GeoJSON, SHP, or GPKG sibling resources):** the dataset ships in 8 forms (4326 × {csv, shp.zip, gpkg, geojson} plus the same in EPSG:2952). At-a-glance trade-offs across the four 4326 options:

  | Resource | Size | Parser | Verdict |
  |----------|------|--------|---------|
  | `building-outlines-4326.csv` | 273 MB | stdlib `csv` + `json.loads(row["geometry"])` per row | **chosen** — same pattern as SolarTO, smallest stdlib-only payload |
  | `building-outlines-4326.zip` (SHP) | 262 MB | `pyshp` | viable, but unzips to ~600 MB on disk and the SHP→Python overhead beats CSV streaming only marginally |
  | `building-outlines-4326.gpkg` | 199 MB | needs `fiona`/GDAL | smallest on the wire, but adds a heavy operator dep this project deliberately avoids |
  | `building-outlines-4326.geojson` | 414 MB | `ijson` | largest payload; same parse complexity as CSV but ~150 MB more bandwidth per refresh |

  The CSV streams row-by-row through `csv.DictReader` and decodes one MultiPolygon per row with `json.loads` + `shapely.geometry.shape` — bounded memory during parse, no new deps, no subprocess.
- **Schema (relevant columns):**

  | Column                  | Type    | Use |
  |-------------------------|---------|-----|
  | `_id`                   | int     | Stable per-resource row id; not used. |
  | `SUBTYPE_CODE`          | int     | `9003` for the canonical `Building Outline` (verified against first row); other codes may appear (e.g. canopy/awning, ancillary structure). The ETL **must filter `SUBTYPE_CODE == 9003`** to avoid double-counting overhangs as roof footprint. |
  | `SUBTYPE_DESC`          | string  | Human-readable mirror of `SUBTYPE_CODE` (e.g. `Building Outline`); kept for debug. |
  | `ELEVATION`             | float   | Ground elevation at the building footprint (m). Not used for coverage; available for future shadow-analysis sanity checks. |
  | `DERIVED_HEIGHT`        | float   | Building absolute-height-AMSL (not relative-to-ground). **Out of scope here** — Task 3 (3D Massing) ships the relative heights the shadow-analysis stage needs. We do **not** synthesize relative height from `DERIVED_HEIGHT − ELEVATION`; the 3D Massing dataset is the canonical height source per design. |
  | `OBJECTID`              | int     | Provincial-style FK; debug-only. |
  | `LAST_GEOMETRY_MAINT`   | date    | Per-feature edit date (e.g. `2021-10-04`); informs cache invalidation reasoning but is not used at runtime. |
  | `geometry`              | string  | Embedded GeoJSON `MultiPolygon` (4-deep nested array — confirmed from first-row probe: `{"coordinates": [[[[-79.387152..., 43.642539...], ...]]]}`). **WGS84 / EPSG:4326.** |
- **Per-parcel coverage formula (Task 17/18):**
  - `buildingCoverageRatio = clamp(intersected_area_m² / parcel.area_m², 0.0, 1.0)`
  - **Geodesic area, not planar.** All m² are computed with `pyproj.Geod(ellps="WGS84").geometry_area_perimeter(geom)` — *not* `shapely.area` on raw lon/lat (which gives degree² and is meaningless at Toronto's latitude). This matches the v1.1 zoning module's area handling.
  - **Multi-building parcels:** a parcel containing two separate buildings sums both intersection areas before the ratio. The clamp upper bound catches the rare overhang/awning case where building polygons overlap each other and the parcel.
  - **Empty parcels (no candidate building):** `buildingCoverageRatio = 0.0` (not null). Downstream scoring distinguishes "vacant lot" from "missing data" via the `solarShadowQuality` field, not coverage.
- **Aggregation approach (Task 17):**
  1. `download_with_retries` → cache as `tools/cache/building_outlines.csv` (~273 MB).
  2. Stream rows; skip any with `SUBTYPE_CODE != 9003` (defense-in-depth — observed value is uniformly `9003` in the head sample, but the schema admits others).
  3. `geom = shapely.geometry.shape(json.loads(row["geometry"]))` → keep `(geom, OBJECTID)`.
  4. Build `STRtree` once over all building geometries; cache the parallel geom list.
  5. Per-parcel: `idxs = strtree.query(parcel.geometry, predicate="intersects")` → for each candidate, `inter = parcel.intersection(building); area_m² += geod.geometry_area_perimeter(inter)[0]` → ratio + clamp.
- **Operator dependency:** none new. `requests` (download), stdlib `csv` + `json` (parse), `shapely` + `pyproj` (geometry + geodesic area) — all already pinned. No `ijson` needed because the CSV reader bounds memory inherently.
- **Other notes:**
  1. **Cache size:** 273 MB lands under `tools/cache/building_outlines.csv`. This is the largest single source in the parcel pipeline; a cold ETL pulls it once, subsequent runs reuse.
  2. **Snapshot age:** per-feature `LAST_GEOMETRY_MAINT` dates skew toward 2021–2024 even on the 2026 resource. New construction lags the public registry by 1–3 years. The `score==0` gate (Task 25) depends on heritage + zoning, not building presence — so a parcel mid-build will still rank correctly.
  3. **`DERIVED_HEIGHT` vs 3D Massing:** Building Outlines carries height-AMSL on every footprint, and 3D Massing is a separate, larger dataset. We deliberately use 3D Massing for shadow analysis because (a) it ships ground-relative heights computed by the city, (b) the spec already locks it in §103 of design.md, and (c) `DERIVED_HEIGHT − ELEVATION` would silently encode any city ground-truthing drift. Building Outlines is the **footprint** source; 3D Massing is the **height** source.
  4. **Coordinate-axis order:** GeoJSON geometry is `[lon, lat]` per spec; verified against the Salisbury-Ave-area first-row coords (`-79.387, 43.642` is on Toronto's near-west side, plausibly a building near High Park). Do not flip.

### 3D Massing Source

- **Dataset:** [3D Massing](https://open.toronto.ca/dataset/3d-massing/) (CKAN slug `3d-massing`)
- **Package id:** `387b2e3b-2a76-4199-8b3b-0b7d22e2ec10`
- **Resource used:** `3DMassingShapefile_2025_WGS84.zip` (latest snapshot)
  - **Resource id:** `667237d6-4d3c-4cf3-8cb7-e91c48d59375`
  - **Format:** ZIP archive containing an Esri Shapefile bundle (`.shp/.shx/.dbf/.prj/.sbn/.sbx/.shp.xml/.cpg`) plus a `README_Metadata.xlsx` schema dictionary
  - **Size:** ~81.4 MB compressed (Content-Length 81,415,175; uncompressed ~300 MB; 428,184 records)
  - **Direct URL:** `https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/387b2e3b-2a76-4199-8b3b-0b7d22e2ec10/resource/667237d6-4d3c-4cf3-8cb7-e91c48d59375/download/3dmassingshapefile_2025_wgs84.zip`
  - **Last modified (snapshot):** 2025-12-02 (city refreshes annually around year-end; **note the gap** — annual vintages 2016–2023 then 2025; no 2024 snapshot exists)
- **Why Shapefile (not Multipatch, not earlier vintages):** the dataset ships in 19 resources — annual `Shapefile` (2D footprint + height attributes, ~80–99 MB) **paired with** annual `Multipatch` (true 3D mesh, ~140–150 MB) for each vintage. The shadow algorithm in `tools/shadow_analysis.py` (Tasks 20/21) needs footprint + scalar height only — it extrudes a slanted prism per building per sun-angle, not a pixel-accurate render of pitched roofs. Multipatch's per-vertex 3D geometry is wasted at parcel resolution, costs ~70 MB extra bandwidth, and isn't reliably parsed by `pyshp`. The 2D Shapefile gives identical shadow output via `MAX_HEIGHT` and is read by the same `pyshp` already pinned for the Heritage source.
- **CRS surprise — geometry is Web Mercator, not WGS84.** The resource filename and CKAN title both say `WGS84`, but the `.prj` decodes to `PROJCS["WGS_1984_Web_Mercator_Auxiliary_Sphere", ... UNIT["Meter",1.0]]` — i.e. **EPSG:3857**, projected metres. The `LONGITUDE` and `LATITUDE` *attribute* fields carry the true centroid lat/lon (verified: record 0 has Mercator x ≈ −8863319 ≈ −79.62°, lat-attr 43.722°). **Implication:** the ETL must reproject geometry from EPSG:3857 → EPSG:4326 on ingest with `pyproj.Transformer.from_crs(3857, 4326, always_xy=True)` before adding to the STRtree. Parcels (Property Boundaries source) are already in 4326 — reprojecting massing matches them, not the other way around. Do **not** trust the filename.
- **Schema (relevant fields):** confirmed against the in-zip `README_Metadata.xlsx` and the actual `.dbf` (which differ — see note 3 below):

  | Field         | Type        | Units / Domain | Use |
  |---------------|-------------|----------------|-----|
  | `MIN_HEIGHT`  | float       | metres         | Roof minimum (eaves) — useful for sloped-roof shadow calc but optional for tier-1 algo |
  | `MAX_HEIGHT`  | float       | metres         | **Primary height field** for shadow analysis (`Building.height_m` in design.md §347) |
  | `AVG_HEIGHT`  | float       | metres         | Used as fallback when `MAX_HEIGHT == 0.0` and `AVG_HEIGHT > 0.0` (rare but observed) |
  | `HEIGHT_MSL`  | float       | metres above sea level | Absolute height; **do not use** as ground-relative — use `MAX_HEIGHT` |
  | `SURF_ELEV`   | float       | metres         | Ground elevation under footprint; informational only |
  | `HEIGHT_SRC`  | string(30)  | `Lidar-Derived` ‖ `Site Plan` ‖ `3D Model` ‖ `Photogrammetrics` ‖ `Oblique Aerials` | **Drives the three-tier shadow quality classification — see below** |
  | `BLDG_SRC`    | string(30)  | same domain as `HEIGHT_SRC` | Source of the footprint geometry; not used for scoring |
  | `LONGITUDE`   | float       | degrees        | Centroid lon in true WGS84 (sanity-check after reprojection) |
  | `LATITUDE`    | float       | degrees        | Centroid lat in true WGS84 |
- **Three-tier shadow-quality mapping** (drives `solarShadowQuality ∈ {"measured", "estimated", "unavailable"}` per design.md §147):

  | `HEIGHT_SRC` value      | `MAX_HEIGHT` | → `solarShadowQuality` |
  |--------------------------|--------------|------------------------|
  | `Lidar-Derived`          | > 0          | `measured`             |
  | `Photogrammetrics`       | > 0          | `measured`             |
  | `3D Model`               | > 0          | `estimated`            |
  | `Site Plan`              | > 0          | `estimated`            |
  | `Oblique Aerials`        | > 0          | `estimated`            |
  | (any)                    | == 0.0       | `unavailable` (height field truncated/missing) |
  | (parcel has no Massing record within `search_radius_m`) | n/a | `unavailable` |

  Empirical sparsity (random sample of 1,000 of 428,184): 96.3% `Lidar-Derived`, 1.7% `Site Plan`, 1.7% `3D Model`, 0.3% `Photogrammetrics`, plus 4.3% with `MAX_HEIGHT == 0.0` regardless of source. So roughly **96% of parcels with a Massing record will land in `measured`**, ~3% in `estimated`, ~4% in `unavailable` (with non-zero overlap between the zero-height and source-tier counts). The vast majority of the city is LiDAR-derived; the fallback tiers exist for permitted-but-unbuilt and edge cases.
- **Aggregation approach (Task 19, called from Task 20 shadow analysis):**
  1. `download_with_retries` → cache as `tools/cache/massing.shp.zip` (no extraction; pyshp reads from the zip via `zipfile.ZipFile.open` streams, same pattern as Heritage).
  2. `transformer = pyproj.Transformer.from_crs(3857, 4326, always_xy=True)`.
  3. Iterate records → `xy = list(zip(*shape.points))`; `lonlat_x, lonlat_y = transformer.transform(xy[0], xy[1])`; `geom = shapely.geometry.Polygon(zip(lonlat_x, lonlat_y))`.
  4. `height_m = max(rec["MAX_HEIGHT"], rec["AVG_HEIGHT"])` if `rec["MAX_HEIGHT"] == 0.0` else `rec["MAX_HEIGHT"]`. If still `0.0` → mark this record as height-unknown (consumed by Task 20 to assign `unavailable` quality at parcel time).
  5. `quality_tier = "measured" if HEIGHT_SRC in {"Lidar-Derived", "Photogrammetrics"} else "estimated"` (override to `unavailable` if height is unknown).
  6. Build `STRtree` once over all reprojected footprints; cache parallel list of `(geom, height_m, quality_tier)` tuples (or the `Building` dataclass per design.md §347).
- **Operator dependency:** `pyshp` (already pinned for Heritage) + `pyproj` (already pinned for v1.1 geodesy). No new deps.
- **Other notes:**
  1. **Why latest vintage matters:** an older snapshot (e.g. 2023) has 5–8% fewer LiDAR-derived records and more `Site Plan` placeholders for now-built towers. The 2025 snapshot is materially more accurate for current parcel scoring; pin the 2025 resource id, not "latest" (CKAN doesn't expose a latest-alias).
  2. **Vintage drift in `RESOURCE_URL`:** when a 2026 snapshot lands, the resource id changes (it's per-vintage). Update `tools/sources/massing.py:RESOURCE_URL` and re-run; the cache filename stays generic (`massing.shp.zip`) so the cache is invalidated by ETag/Last-Modified, not filename.
  3. **`README_Metadata.xlsx` vs actual `.dbf` schema:** the metadata spreadsheet lists `OBJECTID`, `SURV_ELEV` (note the **typo** — actual field is `SURF_ELEV`), `SHAPE_LENGTH`, and `SHAPE_AREA`. The shipped `.dbf` does **not** include `OBJECTID`, `SHAPE_LENGTH`, or `SHAPE_AREA` — only the 9 fields tabled above. The metadata is stale; trust the `.dbf`.
  4. **No 2024 vintage:** annual snapshots run 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, **(skip)**, 2025. Don't waste time looking for a 2024 resource id; it doesn't exist.
  5. **Building Outlines vs 3D Massing footprints:** these are *different* polygon datasets from different city pipelines. Footprints disagree at the metre level. The design (§103, §347) deliberately uses Building Outlines for `buildingCoverageRatio` and 3D Massing for shadow heights — **don't swap them**. Coverage uses the 273 MB current-quarter Building Outlines; shadow uses the 81 MB 2025 Massing.

## Shadow Analysis

The parcel-level `solarScore` is the raw SolarTO rooftop score multiplied by an
`unshadowed_fraction` derived from the 3D Massing dataset's neighbouring
buildings. The implementation lives in `tools/shadow_analysis.py`. Three tiers
honour the design's accuracy-over-completeness rule (Req 12.2):

| Tier | When | Output `quality` | Method |
|------|------|------------------|--------|
| 1 | All candidates within `search_radius_m` have valid footprint + height | `"measured"` | Planar shadow polygon = footprint + translated copy at each sun angle, unioned, intersected with parcel polygon |
| 2 | At least one candidate carries height but no usable footprint | `"estimated"` | Conservative envelope disc (radius = `height_m / tan(elev)`) at the candidate's centroid; biases `solarScore` downward |
| 3 | No candidate has a usable height anywhere within the radius | `"unavailable"` | Returns `(None, "unavailable")` — orchestrator sets `solarScore = null` and bloom is automatically false |

**Reference sun angles** (`shadow_analysis.REFERENCE_ANGLES`): `(180°, 22°)` and
`(180°, 70°)` — winter and summer solstice noon at Toronto's latitude. The two
fractions are averaged for the per-parcel adjustment.

**Search radius** (`DEFAULT_SEARCH_RADIUS_M = 75`): a building's shadow at
elevation 22° from a 30 m height reaches ~74 m, so 75 m bounds the candidate
set without missing meaningful contributors. `MAX_CANDIDATES_PER_PARCEL = 100`
caps the per-parcel work; for parcels with > 100 buildings within radius
(downtown high-density), we keep the 100 closest by centroid distance.

**Projection method** (`SHADOW_PROJECTION_METHOD = "planar"`): metres-per-degree
constants derived once at Toronto's latitude (43.7°N, `1° lat = 111 km`,
`1° lon ≈ 80.3 km`). Sub-metre accuracy across a 50 m parcel — well below
shadow-edge ambiguity from rooftop pitch and ground slope.

**Per-tier expected error** (vs. an annual ray trace at 1 ° hourly resolution):

| Tier | Bias | Magnitude (estimated) |
|------|------|-----------------------|
| 1 | ~unbiased | ±5–10 % `unshadowed_fraction` from rooftop-shape simplification |
| 2 | systematic underestimate (downward bias) | up to 30 % under-prediction; safe direction |
| 3 | n/a (`null` propagated) | bloom-false guaranteed |

The two-angle averaging covers the year's solstice extremes. A future
v1.3 single-angle compute fallback (`Mar 21 / Sep 21, ~46° elevation`) is
documented in design.md §357 if the warm ETL exceeds the NFR 60-minute budget.

## Parcel ETL

```
python3 tools/build_parcels.py
python3 tools/build_parcels.py --help
```

Expected runtime: **30–60 minutes** with a warm cache (warm = `tools/cache/`
already has `building_outlines.csv` and `massing.shp.zip` in addition to the
v1.1 caches). Cold first run: 60–120 minutes — adds ~354 MB of fresh
downloads (Building Outlines 273 MB CSV + 3D Massing 81 MB SHP zip on top of
the v1.1 ~1.4 GB).

CLI flags:

- `--out PATH` — output GeoJSON path (default: `data/parcels.geojson`).
- `--cache-dir PATH` — download cache directory (default: `tools/cache/`).
- `--include-non-eligible` — keep parcels with `score == 0` on the wire.
  Default off — those parcels skip the expensive shadow stage and never
  reach the GeoJSON, which is required to keep the warm ETL within the
  NFR 60-minute budget.
- `--shard-by-neighborhood` — emit `data/parcels/<slug>.geojson` per neighborhood
  + a `data/parcels/index.json` manifest, instead of one ~5 MB file. Use only
  if the natural output exceeds ~25 MB.
- `--quiet` — suppress per-stage progress logs.

**Memory budget:** the warm pipeline holds the parcels generator + heritage
STRtree (~10 MB) + zone STRtree (~50 MB) + transit Points (small) +
**Building Outlines STRtree (~600 MB)** + **3D Massing STRtree (~500 MB)** +
SolarTO STRtree (~100 MB) + Centreline STRtree (~100 MB) simultaneously.
Persistent peak is roughly **1.4 GB**. The orchestrator streams parcels via
`zoning.iter_parcels` (no list materialization) so per-parcel work is
transient.

**Pipeline stages** (15 in design.md §161, 11 in code after the streaming
refactor): fetch neighborhoods → census (builtYear) → heritage → major-transit
stops → subway-only stops → zone index → building outlines → 3D massing →
solar points + P95 → centreline index → assemble (the dominant cost; per
parcel: lookup, score, gate-skip-if-zero, shadow analysis, properties).

The orchestrator exits non-zero on any unhandled exception and writes via
`os.replace`, so a failure mid-run never leaves a partial GeoJSON on disk.

## Setup

- Python 3.10 or newer.
- Install pinned deps: `pip install --user -r tools/requirements.txt` (or use a venv: `python3 -m venv .venv && .venv/bin/pip install -r tools/requirements.txt`).
- The canopy stage shells out to `ogr2ogr` (GDAL ≥ 3.7) once, to extract the FileGDB. On the dev workstation this is provided by a conda-forge GDAL install; ensure `ogr2ogr` resolves on `PATH` before the first run.
- `data/` (the output dir) and `tools/cache/` (the download cache) are created on demand by the ETL.

## Run

```
python3 tools/build_neighborhoods.py
python3 tools/build_neighborhoods.py --help
```

Expected runtime: **5–15 minutes** with a warm cache; first-ever run is slower (downloads ~1.4 GB across nine resources, of which ~475 MB is the property boundaries file). The `tools/cache/` directory is reused across runs — the ETL only re-downloads if a cache file is missing.

CLI flags:

- `--out PATH` — override the output JSON path (default: `data/neighborhoods.json`).
- `--cache-dir PATH` — override the download cache directory (default: `tools/cache/`).
- `--weights JSON` — override the score weights, e.g. `--weights '{"energy":1.0,"canopy":0,"walk":0,"transit":0,"bike":0,"mm":0}'`. Keys not in the override fall back to the module defaults.
- `--quiet` — suppress per-stage progress logs (errors still go to stderr).

The orchestrator exits non-zero on any unhandled exception and writes via `os.replace`, so a failure mid-run never leaves a partial JSON on disk.

## Deployment

Two ETL outputs are deployed together: `data/neighborhoods.json` (v1.1) and
`data/parcels.geojson` (v1.2). Both are built on a workstation and copied to
the VPS. There is no deploy script — the procedure is one-shot enough to
script per-operator if you want, but the canonical steps are:

1. From the project root on the workstation: `python3 tools/build_neighborhoods.py` → produces `data/neighborhoods.json`. Then `python3 tools/build_parcels.py` → produces `data/parcels.geojson`.
2. `scp data/neighborhoods.json data/parcels.geojson john@<vps>:/var/www/html/bloomto/data/`. The `data/` directory must exist on the VPS first; one-time setup: `mkdir -p /var/www/html/bloomto/data && chgrp -R www-data /var/www/html/bloomto/data && chmod -R 0775 /var/www/html/bloomto/data`.
3. If `.htaccess` was changed in the same iteration, `scp` it together with the data files — the data files are unreachable until `.htaccess` is widened to allow both basenames (`neighborhoods.json` AND `parcels.geojson`).
4. On the VPS: `chgrp www-data /var/www/html/bloomto/data/{neighborhoods.json,parcels.geojson} && chmod 0664 /var/www/html/bloomto/data/{neighborhoods.json,parcels.geojson}` (scp doesn't preserve group ownership across users).
5. Verify: `curl -I https://<host>/bloomto/data/neighborhoods.json` and `curl -I https://<host>/bloomto/data/parcels.geojson` → both `200` with `Cache-Control: public, max-age=300, must-revalidate` and an `ETag`.

## Naming Convention Exception

This `tools/` tree uses **snake_case** for `.py` modules (PEP 8) and **kebab-case** for everything else, contrary to the project-wide kebab-case rule documented in `.claude/steering/structure.md`. Rationale: PEP 8 is non-negotiable for Python imports, and inconsistency inside `tools/` is contained to this directory.

## Do Not Run On VPS

The v1.1 neighborhoods ETL is **workstation-only**. It downloads ~1.4 GB,
unzips a FileGDB, and shells out to `ogr2ogr`. The VPS has neither the disk
budget nor the GDAL install nor the egress quota for this.

The v1.2 parcel ETL (`tools/build_parcels.py`) is **GDAL-free** (pure-Python
deps only — no `ogr2ogr`, no `gdal-bin`) but holds ~1.4 GB persistent in
memory during the per-parcel loop. On a 2 GB VPS it's borderline-feasible
after the streaming refactor; in practice still preferred to run on a
workstation with ≥ 4 GB available RAM.

The runtime site (`index.html`, `geocode-proxy.php`) on the VPS only reads
the produced JSON / GeoJSON files; it never invokes the ETL.
