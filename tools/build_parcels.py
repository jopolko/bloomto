"""Offline ETL orchestrator for the parcel-level Multiplex Readiness view.

Composes the v1.2 source modules (heritage, building_outlines, massing, plus
extensions to ttc/streets/solar_to/zoning) into a single per-parcel score
and emits `data/parcels.geojson` atomically.

This is the parcel sibling of `tools/build_neighborhoods.py` — same shape:
CLI in, GeoJSON out, no PHP, no plugin, no build step. Run on a workstation
only (downloads ~1.4 GB of cached data and may peak at ~1 GB resident memory
during the per-parcel loop).

Run:
    python3 tools/build_parcels.py
    python3 tools/build_parcels.py --out data/parcels.geojson
    python3 tools/build_parcels.py --include-non-eligible    # keep score==0 parcels
"""

import argparse
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyproj import Geod, Transformer
from shapely.geometry import Point, box
from shapely.strtree import STRtree

from tools import parcel_io, parcel_scoring, shadow_analysis
from tools.sources import (
    building_outlines as bo_src,
    building_permits as permits_src,
    census as census_src,
    cycling as cycling_src,
    flood as flood_src,
    heritage as heritage_src,
    institutions as institutions_src,
    massing as massing_src,
    neighborhoods as neighborhoods_src,
    sixplex_district as sixplex_src,
    solar_to as solar_src,
    street_trees as street_trees_src,
    streets as streets_src,
    trca_floodplain as trca_src,
    ttc as ttc_src,
    osm_ttc_stations as ttc_stations_src,
    zoning as zoning_src,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "data" / "parcels.geojson"
DEFAULT_CACHE = PROJECT_ROOT / "tools" / "cache"

# Worker module-globals used when the per-parcel loop runs under
# multiprocessing. Set by `_init_worker(state)` in each child process at
# Pool startup. Sequential runs also call _init_worker locally so the same
# `_W`-driven `_process_parcel` function works in both modes — single source
# of truth for the per-parcel logic.
_W: dict = {}


def _init_worker(state: dict) -> None:
    """Pool initializer. Stash shared inputs in module-global `_W` so
    `_process_parcel` can use them without re-passing on every call.
    Linux fork() COW means workers share the parent's loaded indexes
    until/unless they're written to — read-only `_W` stays cheap.
    """
    global _W
    _W = state


def _process_parcel(parcel_or_record) -> dict:
    """Process one parcel, return a result dict the parent aggregates.

    Accepts either a `Parcel` object (sequential path) OR a lightweight
    record dict (multiprocessing path — parent streams cheap dicts so the
    GEOS-heavy `shape()` + geodesic-area work parallelizes across workers
    instead of bottlenecking the parent's iter_parcels generator).

    Pure read of `_W` (the shared state set by `_init_worker`). No outer
    state mutation — claims sets and stats counters are RETURNED, not
    mutated, so workers under multiprocessing don't fight over shared sets.

    Result shape:
      {'skip': '<reason>', ['inst_category': str]}      # parcel dropped
      {'feature': dict, 'stats': {...},                 # parcel kept
       'heritage_claims': set[int],
       'permit_claims_by_nb': dict[str, list[int]],
       'permit_unjoined': bool}

    Stats deltas (always present in the keep path; absent in skip path):
      score_pos, residential, sixplex_eligible, corner, postwar, bloom,
      heritage_part_iv, heritage_part_v, heritage_listed,
      outside_transit_buffer, abuts_laneway, near_rapidto,
      in_flooding_area, in_regulated_area, mature_trees,
      permits_address_join (count, not bool), permits_unjoined_per_parcel
    """
    # If we received a record dict from a parent's iter_parcel_records stream,
    # materialize the Parcel here in the worker — this is the GEOS-heavy
    # `shape()` + geodesic area work that we want parallelized.
    if isinstance(parcel_or_record, dict):
        parcel = zoning_src.parcel_from_record(parcel_or_record)
        if parcel is None:
            return {'skip': 'unparseable_geometry'}
    else:
        parcel = parcel_or_record

    nb_tree = _W['nb_tree']
    neighborhoods = _W['neighborhoods']
    institutions_index = _W['institutions_index']
    ttc_station_index = _W['ttc_station_index']
    zone_index = _W['zone_index']
    multipliers = _W['multipliers']
    sixplex_index = _W['sixplex_index']
    heritage_index = _W['heritage_index']
    transit_subway_tree = _W['transit_subway_tree']
    transit_streetcar_only_tree = _W['transit_streetcar_only_tree']
    transit_bus_tree = _W['transit_bus_tree']
    massing_index = _W['massing_index']
    building_geoms = _W['building_geoms']
    building_tree = _W['building_tree']
    solar_tree = _W['solar_tree']
    solar_kwh = _W['solar_kwh']
    solar_p95 = _W['solar_p95']
    centreline_index = _W['centreline_index']
    rapidto_tree = _W['rapidto_tree']
    flood_index = _W['flood_index']
    trca_index = _W['trca_index']
    bike_tree = _W['bike_tree']
    bike_lines = _W['bike_lines']
    street_tree_index = _W['street_tree_index']
    permit_index = _W['permit_index']
    permit_freshness_cutoff = _W['permit_freshness_cutoff']
    nb_canopy_by_name = _W['nb_canopy_by_name']
    built_year_by_name = _W['built_year_by_name']
    include_non_eligible = _W['include_non_eligible']

    zone_tree, zone_classes = zone_index
    centreline_tree, centreline_name_ids, centreline_laneway_idx = centreline_index

    # --- gate stage 1: neighborhood ---
    nb = _lookup_neighborhood(parcel, nb_tree, neighborhoods)
    if nb is None:
        return {'skip': 'no_nb'}
    built_year = built_year_by_name.get(nb.name, 0)

    # --- gate stage 2: institutional ---
    is_inst, inst_category = institutions_src.is_institutional(parcel.geometry, institutions_index)
    if is_inst:
        return {'skip': 'institutional', 'inst_category': inst_category}

    # --- gate stage 3: TTC station ---
    if ttc_stations_src.is_ttc_station(parcel.geometry, ttc_station_index):
        return {'skip': 'ttc_station'}

    # --- per-parcel work ---
    zone_class = _lookup_zone_class(parcel, zone_tree, zone_classes)
    max_units = zoning_src.lookup_multiplier(zone_class, multipliers)
    residential = max_units > 0

    sixplex_eligible = sixplex_src.is_sixplex_eligible(parcel.geometry, sixplex_index)
    if sixplex_eligible and residential and max_units < 6:
        max_units = 6

    # Heritage: in sequential mode `_W['claimed_heritage_indices']` IS the
    # parent's shared set — mutations stick, address-join takes precedence
    # over point-in-parcel exactly like the legacy code. In parallel mode
    # each worker has its own copy via fork-COW; cross-worker dedup is
    # approximate (documented in --workers help). Return the snapshot for
    # parent merge — set-union is idempotent so the parallel merge stays
    # correct even when multiple workers hit overlapping claims.
    shared_claims = _W['claimed_heritage_indices']
    heritage_status = _resolve_heritage_status(parcel, heritage_index, shared_claims)
    local_heritage_claims = set(shared_claims)

    # Build the early-stats dict — these counters fire on EVERY parcel that
    # got far enough to compute residential/sixplex/heritage, even if it
    # later short-circuits via score_zero. Matches legacy sequential code
    # which incremented these BEFORE the score-zero gate. Without this the
    # parallel run reports 0 Part IV (because Part IV parcels score 0 →
    # short-circuit → never counted).
    early_stats = {
        'residential': 1 if residential else 0,
        'sixplex_eligible': 1 if sixplex_eligible else 0,
        'heritage_part_iv': 1 if heritage_status == 'part_iv' else 0,
        'heritage_part_v': 1 if heritage_status == 'part_v' else 0,
        'heritage_listed': 1 if heritage_status == 'listed' else 0,
    }

    rep_pt = parcel.geometry.representative_point()
    rep_coords = (rep_pt.x, rep_pt.y)
    dist_subway_m = _distance_to_nearest_stop_m(rep_coords, transit_subway_tree)
    dist_streetcar_m = _distance_to_nearest_stop_m(rep_coords, transit_streetcar_only_tree)
    dist_subway_streetcar_m = min(dist_subway_m, dist_streetcar_m)
    dist_bus_m = _distance_to_nearest_stop_m(rep_coords, transit_bus_tree)

    full_score = parcel_scoring.compute_full_score(
        residential=residential,
        heritage_status=heritage_status,
        dist_m=dist_subway_streetcar_m,
        max_units=max_units,
        area_m2=parcel.area_m2,
    )
    base_score = full_score['score']
    soft_s = full_score['softScore']
    outside_buffer = full_score['outsideTransitBuffer']

    if base_score == 0 and soft_s == 0 and not include_non_eligible:
        # Skip but carry the early stats (residential / sixplex / heritage
        # tier) so the parent's counters match the legacy sequential code.
        return {
            'skip': 'score_zero',
            'early_stats': early_stats,
            'heritage_claims': local_heritage_claims,
        }

    # Building coverage
    building_area_m2 = 0.0
    for idx in building_tree.query(parcel.geometry):
        try:
            inter = parcel.geometry.intersection(building_geoms[idx])
        except Exception:
            continue
        if inter.is_empty:
            continue
        area_signed, _ = _GEOD.geometry_area_perimeter(inter)
        building_area_m2 += abs(area_signed)
    coverage = (
        max(0.0, min(1.0, building_area_m2 / parcel.area_m2))
        if parcel.area_m2 > 0 else 0.0
    )

    if parcel.address is None and coverage == 0:
        return {
            'skip': 'non_buildable',
            'early_stats': early_stats,
            'heritage_claims': local_heritage_claims,
        }

    aspect = _lot_aspect_ratio(parcel)

    max_kwh = 0.0
    for idx in solar_tree.query(parcel.geometry):
        pt = solar_tree.geometries[idx]
        if not parcel.geometry.contains(pt):
            continue
        kwh = solar_kwh[idx]
        if kwh > max_kwh:
            max_kwh = kwh
    solar_raw = (
        max(0, min(100, round(100 * max_kwh / solar_p95)))
        if solar_p95 > 0 else 0
    )

    try:
        shadow_result = shadow_analysis.analyze_parcel(parcel, massing_index)
    except Exception as e:
        _log.warning(
            "shadow_analysis failed for parcel %s (%s): %s — marking unavailable",
            parcel.parcel_id, parcel.address, e,
        )
        shadow_result = shadow_analysis.ShadowResult(None, 'unavailable')
    if shadow_result.quality == 'unavailable' or shadow_result.unshadowed_fraction is None:
        solar_score = None
    else:
        solar_score = max(0, min(100, round(solar_raw * shadow_result.unshadowed_fraction)))

    corner = streets_src.is_corner_lot(parcel, centreline_tree, centreline_name_ids)
    abuts_laneway = _abuts_laneway(parcel, centreline_tree, centreline_laneway_idx)
    near_rapidto = _near_rapidto(parcel, rapidto_tree)
    in_flooding_area = flood_src.is_in_flooding_area(parcel.geometry, flood_index)
    in_regulated_area = trca_src.is_in_regulated_area(parcel.geometry, trca_index)

    # Permits (per-worker claims; parent merges)
    normalized_addr = (
        heritage_src.normalize_address(parcel.address) if parcel.address else ''
    )
    local_permit_claims: list[int] = []
    if normalized_addr:
        for pi in permit_index.address_to_indices.get(normalized_addr, []):
            if pi in permit_index.claimed:
                continue
            permit_index.claimed.add(pi)  # local-to-worker due to fork COW
            local_permit_claims.append(pi)
    permits_address_join = len(local_permit_claims)
    permits_unjoined_per_parcel_bool = (permits_address_join == 0)
    denom_source = 'address_join' if local_permit_claims else 'no_joined_permits'
    permits_payload = permits_src.aggregate_per_parcel(
        local_permit_claims, permit_index.permits, permit_freshness_cutoff, denom_source,
    )
    permit_claims_by_nb: dict = {}
    if local_permit_claims:
        permit_claims_by_nb[nb.name] = list(local_permit_claims)

    nb_canopy_pct = nb_canopy_by_name.get(nb.name)
    street_tree_count, mature_tree_count = street_trees_src.count_for_parcel(
        parcel.geometry, street_tree_index,
    )
    dist_bike_m = cycling_src.nearest_bike_lane_distance_m(
        parcel.geometry, bike_tree, bike_lines,
    )

    postwar = (
        POSTWAR_BUILT_YEAR_MIN <= built_year <= POSTWAR_BUILT_YEAR_MAX
        and heritage_status is None
    )
    bloom = parcel_scoring.bloom_flag(
        heritage_status=heritage_status,
        dist_subway_streetcar_m=dist_subway_streetcar_m,
        lot_area_m2=parcel.area_m2,
        sixplex_eligible=sixplex_eligible,
        mature_tree_count=mature_tree_count,
        in_regulated_area=in_regulated_area,
    )

    feature = {
        'type': 'Feature',
        'geometry': {
            'type': 'Point',
            'coordinates': [
                round(rep_pt.x, COORD_DECIMALS),
                round(rep_pt.y, COORD_DECIMALS),
            ],
        },
        'properties': {
            'parcelId': parcel.parcel_id,
            'address': parcel.address,
            'score': base_score,
            'softScore': soft_s,
            'outsideTransitBuffer': outside_buffer,
            'zoneClass': zone_class,
            'maxUnits': int(max_units),
            'residential': residential,
            'heritageStatus': heritage_status,
            'distSubwayStreetcarM': int(round(dist_subway_streetcar_m)),
            'distSubwayM': int(round(dist_subway_m)),
            'distStreetcarM': int(round(dist_streetcar_m)),
            'distBusM': int(round(dist_bus_m)),
            'neighborhood': nb.name,
            'builtYear': int(built_year),
            'cornerLot': corner,
            'abutsLaneway': abuts_laneway,
            'nearRapidToCorridor': near_rapidto,
            'inFloodingStudyArea': in_flooding_area,
            'inRegulatedArea': in_regulated_area,
            'permits': permits_payload,
            'neighborhoodPermitComp': None,
            'neighborhoodCanopyPct': nb_canopy_pct,
            'streetTreeCount': street_tree_count,
            'matureTreeCount': mature_tree_count,
            'distBikeLaneM': int(round(dist_bike_m)),
            'sixplexEligible': sixplex_eligible,
            'lotAreaM2': int(round(parcel.area_m2)),
            'lotAspectRatio': round(aspect, 2),
            'buildingCoverageRatio': round(coverage, 3),
            'solarScoreRaw': int(solar_raw),
            'solarScore': solar_score,
            'solarShadowQuality': shadow_result.quality,
            'postwarNeighborhood': postwar,
            'bloom': bloom,
            'lotGeometry': (lambda lo, sh, o: {
                'longAxisM': lo, 'shortAxisM': sh, 'orientationDeg': o,
            })(*_lot_geometry(parcel)),
            'neighborHeights': _neighbor_heights(rep_pt, massing_index),
            'solarYieldKwhPerYr': int(round(max_kwh)) if max_kwh else 0,
            'pvCapacityKwEstimate': round(max_kwh / _TORONTO_PV_YIELD_KWH_PER_KW, 1) if max_kwh else 0.0,
            'sixplexBonusValueCad': None,
        },
    }

    stats_delta = {
        **early_stats,  # residential, sixplex_eligible, heritage_*
        'score_pos': 1 if base_score > 0 else 0,
        'corner': 1 if corner else 0,
        'postwar': 1 if postwar else 0,
        'bloom': 1 if bloom else 0,
        'outside_transit_buffer': 1 if (outside_buffer and soft_s > 0) else 0,
        'abuts_laneway': 1 if abuts_laneway else 0,
        'near_rapidto': 1 if near_rapidto else 0,
        'in_flooding_area': 1 if in_flooding_area else 0,
        'in_regulated_area': 1 if in_regulated_area else 0,
        'mature_trees': 1 if mature_tree_count > 0 else 0,
        'permits_address_join': permits_address_join,
        'permits_unjoined_per_parcel': 1 if permits_unjoined_per_parcel_bool else 0,
    }

    return {
        'feature': feature,
        'stats': stats_delta,
        'heritage_claims': local_heritage_claims,
        'permit_claims_by_nb': permit_claims_by_nb,
    }


def _iterate_parcels(parcels, *, workers: int, state: dict):
    """Yield `_process_parcel` results for every parcel, sequentially or via
    multiprocessing.Pool. Always yields in the order workers complete (i.e.
    NOT input order under parallel mode — caller must not rely on order).
    """
    import multiprocessing
    if workers <= 1:
        # Sequential: set up _W in this process and call _process_parcel directly.
        _init_worker(state)
        for parcel in parcels:
            yield _process_parcel(parcel)
        return
    # Parallel: Pool with fork (default on Linux). Workers inherit parent's
    # loaded indexes via COW, so the heavy state isn't re-pickled per worker.
    ctx = multiprocessing.get_context('fork')
    with ctx.Pool(workers, initializer=_init_worker, initargs=(state,)) as pool:
        # chunksize=500: with 528K parcels and 8 workers, that's ~130 chunks
        # per worker — coarse enough to keep pickle/IPC overhead under 5%,
        # fine enough that the slowest worker doesn't stall the tail by more
        # than ~1 chunk's worth of work. Decimal heuristic: rule of thumb is
        # `total_items / (workers * 100)` ≈ 660; we round down to 500 to
        # tighten tail latency on heterogeneous parcel work (shadow analysis
        # cost varies 10× between dense urban vs vacant suburban lots).
        for result in pool.imap_unordered(_process_parcel, parcels, chunksize=500):
            yield result

DISTANCE_CAP_M = 5000.0
POSTWAR_BUILT_YEAR_MIN = 1945
POSTWAR_BUILT_YEAR_MAX = 1960

# Wire-format coordinate precision. At Toronto's latitude (~43.7°N), 5 decimals
# resolves to roughly 1.1 m — far below parcel-edge resolution (median Toronto
# parcel is ~10 m wide), and the geometry on the wire is the representative
# point, not the polygon, so sub-meter precision is meaningless for the UI.
# Trims ~150–250 KB gzipped off `data/parcels.geojson` vs. shapely's 14-decimal
# default. Number chosen to match the documented Property Boundaries ADDRESS
# precision (the source of all parcel addresses).
COORD_DECIMALS = 5

SOURCE_VERSIONS = {
    "neighborhoods": neighborhoods_src.RESOURCE_URL,
    "property": zoning_src.PROPERTY_RESOURCE_URL,
    "zoning": zoning_src.ZONING_RESOURCE_URL,
    "heritage": heritage_src.RESOURCE_URL,
    "ttc": ttc_src.RESOURCE_URL,
    "building_outlines": bo_src.RESOURCE_URL,
    "massing": massing_src.RESOURCE_URL,
    "solar_to": solar_src.RESOURCE_URL,
    "streets": streets_src.RESOURCE_URL,
}

_GEOD = Geod(ellps="WGS84")

# Toronto-local metres CRS for nearest-stop distance work. UTM Zone 17N covers
# 78°W-72°W; Toronto sits at ~79.4°W, well inside the zone, so projection
# distortion is bounded to <0.5 m over 5 km. Built once at import; transform()
# is thread-safe.
_LONLAT_TO_M = Transformer.from_crs("EPSG:4326", "EPSG:26917", always_xy=True)

_log = logging.getLogger("bloomto.build_parcels")


def _stage(label: str) -> float:
    _log.info("→ %s", label)
    return time.monotonic()


def _done(label: str, started_at: float) -> None:
    _log.info("  %s done in %.1fs", label, time.monotonic() - started_at)


def _distance_to_nearest_stop_m(
    parcel_centroid: tuple[float, float],
    stops_tree: STRtree,
) -> float:
    """Distance (m) from `parcel_centroid` (lng, lat in WGS84) to nearest stop.

    `stops_tree` MUST be built on stops *projected to EPSG:26917* (NAD83 /
    UTM Zone 17N — Toronto's metres-based CRS) by `_build_stops_tree_m`
    below. This function projects the parcel point the same way, then
    `STRtree.nearest` returns the truly-nearest stop because planar
    Euclidean in projected metres equals geodesic metres (within ~10 cm
    for Toronto-scale distances).

    Was previously `STRtree.nearest()` against an **unprojected** tree
    (lng/lat degrees). At Toronto's 43.65°N latitude 1° lat ≈ 111 km vs
    1° lng ≈ 80 km, so degree-space planar Euclidean ≠ geodesic — verified
    to mis-rank 15.7% of elite parcels' `distSubwayM` by 20-983 m.

    Caps at `DISTANCE_CAP_M` (5 km) to bound the wire format's int.
    """
    if len(stops_tree.geometries) == 0:
        return DISTANCE_CAP_M
    px_m, py_m = _LONLAT_TO_M.transform(parcel_centroid[0], parcel_centroid[1])
    pt_m = Point(px_m, py_m)
    idx = stops_tree.nearest(pt_m)
    nearest = stops_tree.geometries[idx]
    dist_m = ((px_m - nearest.x) ** 2 + (py_m - nearest.y) ** 2) ** 0.5
    return min(DISTANCE_CAP_M, dist_m)


def _build_stops_tree_m(stops_lonlat: list[Point]) -> STRtree:
    """Project (lng, lat) Points to EPSG:26917 metres and build an STRtree.

    Pairs with `_distance_to_nearest_stop_m` — both sides must be in the
    same projected CRS for `STRtree.nearest` to be exact in metres.
    """
    return STRtree([
        Point(*_LONLAT_TO_M.transform(p.x, p.y)) for p in stops_lonlat
    ])


def _lookup_neighborhood(parcel, nb_tree: STRtree, neighborhoods):
    """Return the first neighborhood whose polygon contains the parcel centroid."""
    pt = Point(parcel.centroid)
    for idx in nb_tree.query(pt):
        if neighborhoods[idx].polygon.contains(pt):
            return neighborhoods[idx]
    return None


def _lookup_zone_class(parcel, zone_tree: STRtree, zone_classes: list[str]) -> str:
    """Return the zone class label for the parcel (empty string if no match)."""
    rep = parcel.geometry.representative_point()
    for idx in zone_tree.query(rep):
        if zone_tree.geometries[idx].contains(rep):
            return zone_classes[idx]
    return ""


def _resolve_heritage_status(parcel, heritage_index, claimed: set[int]) -> str | None:
    """Return the canonical heritage status for `parcel`, or None.

    Two-pass resolution per design.md (heritage-tiered-status spec):

      1. Address-join: normalize the parcel's address and look it up in
         `heritage_index.address_to_status`. If hit, mark every record index
         whose normalized address matches in `claimed` (via the pre-built
         `address_to_indices` reverse index — O(1) lookup, not an O(n) scan)
         and return the pre-folded status.

      2. Point-in-parcel fallback: if the address-join missed (parcel address
         is empty, or not in the heritage dict), STRtree-query the parcel's
         geometry, fold contained candidates' statuses via `more_restrictive`,
         mark the contained indices in `claimed`, and return the fold. Records
         already claimed by an earlier address-join are skipped — the
         address-join is authoritative, and the geocoded point landing on a
         neighbour is exactly the false-positive the address-join was added
         to fix.

    `claimed` is mutated in-place so the caller can compute
    `heritage_index.points - claimed` after the parcel loop to derive the
    `heritageUnjoined` stat.
    """
    parcel_norm = heritage_src.normalize_address(parcel.address or "")
    if parcel_norm:
        joined = heritage_index.address_to_status.get(parcel_norm)
        if joined is not None:
            for i in heritage_index.address_to_indices.get(parcel_norm, ()):
                claimed.add(i)
            return joined

    status: str | None = None
    for idx in heritage_index.point_tree.query(parcel.geometry):
        if idx in claimed:
            continue
        if parcel.geometry.contains(heritage_index.points[idx]):
            status = heritage_src.more_restrictive(status, heritage_index.statuses[idx])
            claimed.add(idx)
    return status


# Five RapidTO transit-priority arterials per TransformTO 2026-30 Action 6.1.
# Verbatim names matched against centreline LINEAR_NAME_FULL. Buffered ~250m
# (≈0.0025° at Toronto's latitude) for the per-parcel proximity test.
_RAPIDTO_CORRIDOR_NAMES = frozenset({
    "JANE ST",
    "FINCH AVE E",
    "DUFFERIN ST",
    "LAWRENCE AVE E",
    "STEELES AVE W",
})
_RAPIDTO_BUFFER_DEG = 0.0025  # ~250 m at 43.7°N


def _load_rapidto_index(cache_dir: Path) -> STRtree:
    """Build an STRtree of buffered RapidTO corridor segments. One pass over
    the cached centreline.geojson, filtered by LINEAR_NAME_FULL match.
    """
    import json
    from shapely.geometry import shape as _shape
    cached = cache_dir / "centreline.geojson"
    if not cached.exists():
        _log.warning("centreline.geojson not cached — RapidTO index will be empty")
        return STRtree([])
    buffers: list = []
    with cached.open(encoding="utf-8") as fp:
        data = json.load(fp)
    for feat in data.get("features") or []:
        geom_dict = feat.get("geometry")
        if not geom_dict:
            continue
        props = feat.get("properties") or {}
        name = (props.get("LINEAR_NAME_FULL") or "").upper().strip()
        if name not in _RAPIDTO_CORRIDOR_NAMES:
            continue
        try:
            line = _shape(geom_dict)
        except Exception:
            continue
        if line.is_empty:
            continue
        buffers.append(line.buffer(_RAPIDTO_BUFFER_DEG))
    _log.info("rapidto: %d corridor segments buffered", len(buffers))
    return STRtree(buffers)


def _abuts_laneway(parcel, centreline_tree, laneway_idx,
                   buffer_deg: float = 2.7e-5) -> bool:
    """True iff the parcel's boundary touches a centreline feature flagged
    as a Toronto laneway (FEATURE_CODE == 201700). Mirrors the corner-lot
    test's buffer geometry. Used for the laneway-suite-eligibility flag.

    Toronto laneways carry valid LINEAR_NAME_IDs (e.g., "Lane N of Bloor"),
    so the laneway flag is sourced from FEATURE_CODE, surfaced via
    `streets.load_centreline_index`'s third return value (a set of indices).
    """
    boundary = parcel.geometry.boundary
    if boundary.is_empty:
        return False
    buffered = boundary.buffer(buffer_deg)
    for idx in centreline_tree.query(buffered):
        if idx not in laneway_idx:
            continue
        line_geom = centreline_tree.geometries[idx]
        if line_geom.intersects(buffered):
            return True
    return False


def _near_rapidto(parcel, rapidto_tree) -> bool:
    """True iff the parcel intersects any of the buffered RapidTO corridor
    polygons (Jane / Finch E / Dufferin / Lawrence E / Steeles W).
    """
    for idx in rapidto_tree.query(parcel.geometry):
        if rapidto_tree.geometries[idx].intersects(parcel.geometry):
            return True
    return False


def _lot_aspect_ratio(parcel) -> float:
    """Long-axis / short-axis of the minimum-rotated rectangle, ≥ 1.0."""
    try:
        mrr = parcel.geometry.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)
        if len(coords) < 5:
            return 1.0
        # MRR has 4 corners + closing point; compute the two distinct edge lengths.
        e1 = Point(coords[0]).distance(Point(coords[1]))
        e2 = Point(coords[1]).distance(Point(coords[2]))
        if e1 <= 0 or e2 <= 0:
            return 1.0
        long_axis = max(e1, e2)
        short_axis = min(e1, e2)
        return long_axis / short_axis if short_axis > 0 else 1.0
    except Exception:
        return 1.0


def _lot_geometry(parcel) -> tuple[float | None, float | None, float | None]:
    """Return `(longAxisM, shortAxisM, orientationDeg)` for the parcel's
    minimum-rotated rectangle. Edge lengths are geodesic metres (via
    `_GEOD.inv` over WGS84). Orientation is the bearing of the long edge,
    normalized to [0, 180) since axes are non-directional. All `None` on
    geometry failure (degenerate polygons, collapsed slivers).

    The architect-facing detail panel reads these to surface "this lot is
    18 m × 6 m, long axis pointing 75° E-of-N" instead of the 0-100
    `lotAspectRatio` abstraction. A passive-solar designer wants the
    actual orientation, not just a ratio.
    """
    try:
        mrr = parcel.geometry.minimum_rotated_rectangle
        coords = list(mrr.exterior.coords)
        if len(coords) < 5:
            return None, None, None
        _, _, e1 = _GEOD.inv(coords[0][0], coords[0][1], coords[1][0], coords[1][1])
        _, _, e2 = _GEOD.inv(coords[1][0], coords[1][1], coords[2][0], coords[2][1])
        if e1 <= 0 or e2 <= 0:
            return None, None, None
        if e1 >= e2:
            long_start, long_end = coords[0], coords[1]
            long_axis, short_axis = e1, e2
        else:
            long_start, long_end = coords[1], coords[2]
            long_axis, short_axis = e2, e1
        fwd_az, _, _ = _GEOD.inv(long_start[0], long_start[1], long_end[0], long_end[1])
        orientation = fwd_az % 180
        if orientation < 0:
            orientation += 180
        return round(long_axis, 1), round(short_axis, 1), round(orientation, 1)
    except Exception:
        return None, None, None


# Buildings-context radius for `_neighbor_heights`. 30 m matches typical Toronto
# block-face geometry — a parcel's south-side neighbour casting winter shadow,
# the rear-neighbour limiting massing, etc. Larger radii dilute the signal.
_NEIGHBOR_RADIUS_M = 30.0
_NEIGHBOR_RADIUS_DEG = _NEIGHBOR_RADIUS_M / 111_000  # ≤1.4× over-bound for bbox query at 43°N


def _neighbor_heights(rep_pt, massing_index) -> dict:
    """Average building height (m) within `_NEIGHBOR_RADIUS_M` of the parcel's
    representative point, binned into N/S/E/W quadrants relative to that point.

    Quadrants use compass bearings on a 360° circle:
        N: 315°..45°    E: 45°..135°    S: 135°..225°    W: 225°..315°

    Returns a dict with keys `nAvgM`, `sAvgM`, `eAvgM`, `wAvgM`. A quadrant with
    no buildings returns `None` (not 0.0 — distinguishes "open sky" from "low
    bungalow"). `massing_index` must be `(STRtree, list[Building])` from
    `tools.sources.massing.load_massing_index`; buildings without a height (e.g.
    Toronto 3D Massing records lacking MAX_HEIGHT) are skipped.

    The architect-facing panel surfaces these so designers can see "south-side
    is 11 m (3-storey rowhouse blocking winter sun) vs north 6 m" instead of
    inferring it from the shadow-adjusted solarScore alone.
    """
    tree, buildings = massing_index
    quad = {"n": [], "s": [], "e": [], "w": []}
    bbox = box(
        rep_pt.x - _NEIGHBOR_RADIUS_DEG,
        rep_pt.y - _NEIGHBOR_RADIUS_DEG,
        rep_pt.x + _NEIGHBOR_RADIUS_DEG,
        rep_pt.y + _NEIGHBOR_RADIUS_DEG,
    )
    for idx in tree.query(bbox):
        b = buildings[idx]
        if b.height_m is None:
            continue
        c = b.geometry.centroid
        fwd_az, _, dist_m = _GEOD.inv(rep_pt.x, rep_pt.y, c.x, c.y)
        if dist_m > _NEIGHBOR_RADIUS_M:
            continue
        az = fwd_az if fwd_az >= 0 else fwd_az + 360
        if az >= 315 or az < 45:
            quad["n"].append(b.height_m)
        elif az < 135:
            quad["e"].append(b.height_m)
        elif az < 225:
            quad["s"].append(b.height_m)
        else:
            quad["w"].append(b.height_m)
    return {
        f"{k}AvgM": (round(sum(v) / len(v), 1) if v else None)
        for k, v in quad.items()
    }


# PV nameplate capacity estimation: Toronto-latitude rule of thumb is that
# 1 kW of installed PV generates ~1,150 kWh/year (south-facing, 30° tilt,
# unshaded). Solving for the inverse: pv_kw = max_rooftop_kwh_per_year / 1150.
# Used to convert the `solarYieldKwhPerYr` wire field into a "you could install
# ~X kW on this roof" tease for the developer detail panel.
_TORONTO_PV_YIELD_KWH_PER_KW = 1150.0


def assemble_parcel_payload(
    *,
    neighborhoods,
    parcels,
    heritage_index,
    institutions_index,
    ttc_station_index: STRtree,
    flood_index,
    trca_index,
    rapidto_tree: STRtree,
    zone_index,
    multipliers,
    transit_subway_tree: STRtree,
    transit_streetcar_only_tree: STRtree,
    transit_bus_tree: STRtree,
    massing_index,
    building_geoms: list,
    building_tree: STRtree,
    solar_tree: STRtree,
    solar_kwh: list[float],
    solar_p95: float,
    centreline_index: tuple[STRtree, list[int], set[int]],
    built_year_by_name: dict[str, int],
    permit_index,
    permit_freshness_cutoff,
    bike_tree: STRtree,
    bike_lines: list,
    street_tree_index,
    sixplex_index,
    nb_canopy_by_name: dict[str, int],
    include_non_eligible: bool,
    workers: int = 1,
) -> dict:
    """Build the GeoJSON FeatureCollection payload (no I/O).

    Exposed at module scope so the e2e test can drive it against in-memory
    fixtures without touching disk. Caller is responsible for sourcing every
    index / tree from the cached data; this function only composes them.

    `workers`: 1 (default) runs sequentially in-process. >1 fans the per-
    parcel loop out across `multiprocessing.Pool` workers (Linux fork). Heavy
    indexes are COW-shared with workers so memory only grows ~1.5×, not N×.
    """
    nb_tree = STRtree([n.polygon for n in neighborhoods])
    zone_tree, zone_classes = zone_index
    centreline_tree, centreline_name_ids, centreline_laneway_idx = centreline_index

    features = []
    stats_total = 0
    stats_skipped_unparseable = 0
    stats_score_pos = 0
    stats_heritage_part_iv = 0
    stats_heritage_part_v = 0
    stats_heritage_listed = 0
    stats_residential = 0
    stats_corner = 0
    stats_postwar = 0
    stats_bloom = 0
    stats_skipped_no_nb = 0
    stats_skipped_non_buildable = 0
    stats_skipped_institutional = 0
    stats_skipped_ttc_station = 0
    stats_outside_transit_buffer = 0
    stats_abuts_laneway = 0
    stats_near_rapidto = 0
    stats_in_flooding_area = 0
    stats_in_regulated_area = 0
    stats_mature_trees = 0
    stats_sixplex_eligible = 0
    institutional_by_category: dict[str, int] = {}
    claimed_heritage_indices: set[int] = set()
    # Permit-join state ── populated as parcels are processed; consumed in a
    # second pass after the loop to compute neighborhoodPermitComp.
    parcel_permit_payloads: list[dict] = []  # 1:1 with `features` after loop
    permits_claims_by_neighborhood: dict[str, list[int]] = {}
    stats_permits_address_join = 0
    stats_permits_unjoined_per_parcel = 0  # parcels with denominatorSource="no_joined_permits"

    # Build the worker state dict — every shared input the per-parcel loop
    # body needs. Both sequential and parallel paths use the same
    # `_process_parcel` function, so the per-parcel logic is single-source.
    # IMPORTANT: `claimed_heritage_indices` is the same Python set object
    # the parent uses; in sequential mode the worker mutates it in-place
    # (preserves address-join-precedence dedup). In parallel mode each
    # worker gets its own fork-COW copy.
    _worker_state = {
        'claimed_heritage_indices': claimed_heritage_indices,
        'nb_tree': nb_tree,
        'neighborhoods': neighborhoods,
        'institutions_index': institutions_index,
        'ttc_station_index': ttc_station_index,
        'zone_index': zone_index,
        'multipliers': multipliers,
        'sixplex_index': sixplex_index,
        'heritage_index': heritage_index,
        'transit_subway_tree': transit_subway_tree,
        'transit_streetcar_only_tree': transit_streetcar_only_tree,
        'transit_bus_tree': transit_bus_tree,
        'massing_index': massing_index,
        'building_geoms': building_geoms,
        'building_tree': building_tree,
        'solar_tree': solar_tree,
        'solar_kwh': solar_kwh,
        'solar_p95': solar_p95,
        'centreline_index': centreline_index,
        'rapidto_tree': rapidto_tree,
        'flood_index': flood_index,
        'trca_index': trca_index,
        'bike_tree': bike_tree,
        'bike_lines': bike_lines,
        'street_tree_index': street_tree_index,
        'permit_index': permit_index,
        'permit_freshness_cutoff': permit_freshness_cutoff,
        'nb_canopy_by_name': nb_canopy_by_name,
        'built_year_by_name': built_year_by_name,
        'include_non_eligible': include_non_eligible,
    }

    # No materialization. Generators stream directly to the pool — workers
    # spawn immediately and parsing+processing run in parallel. The 2026-05-06
    # multiproc fast-path swaps `parcels` (eager Parcel objects) for cheap
    # record dicts via `iter_parcel_records`; workers do the GEOS work.
    for result in _iterate_parcels(parcels, workers=workers, state=_worker_state):
        stats_total += 1
        if 'skip' in result:
            reason = result['skip']
            if reason == 'no_nb':
                stats_skipped_no_nb += 1
            elif reason == 'institutional':
                stats_skipped_institutional += 1
                cat = result.get('inst_category', 'unknown')
                institutional_by_category[cat] = institutional_by_category.get(cat, 0) + 1
            elif reason == 'ttc_station':
                stats_skipped_ttc_station += 1
            elif reason == 'non_buildable':
                stats_skipped_non_buildable += 1
            elif reason == 'unparseable_geometry':
                stats_skipped_unparseable += 1
            # 'score_zero' is the bulk of skips — not counted in its own
            # bucket (matches the legacy sequential loop, which fell through
            # silently). BUT residential / sixplex / heritage counters DO
            # need to fire because the legacy code incremented them BEFORE
            # the score-zero gate. The skip result carries `early_stats` +
            # `heritage_claims` for exactly that purpose.
            es = result.get('early_stats')
            if es:
                stats_residential += es['residential']
                stats_sixplex_eligible += es['sixplex_eligible']
                stats_heritage_part_iv += es['heritage_part_iv']
                stats_heritage_part_v += es['heritage_part_v']
                stats_heritage_listed += es['heritage_listed']
            hc = result.get('heritage_claims')
            if hc:
                claimed_heritage_indices |= hc
            continue

        # Keep path: feature + stats deltas + claims to merge
        features.append(result['feature'])
        st = result['stats']
        stats_score_pos += st['score_pos']
        stats_residential += st['residential']
        stats_sixplex_eligible += st['sixplex_eligible']
        stats_corner += st['corner']
        stats_postwar += st['postwar']
        stats_bloom += st['bloom']
        stats_heritage_part_iv += st['heritage_part_iv']
        stats_heritage_part_v += st['heritage_part_v']
        stats_heritage_listed += st['heritage_listed']
        stats_outside_transit_buffer += st['outside_transit_buffer']
        stats_abuts_laneway += st['abuts_laneway']
        stats_near_rapidto += st['near_rapidto']
        stats_in_flooding_area += st['in_flooding_area']
        stats_in_regulated_area += st['in_regulated_area']
        stats_mature_trees += st['mature_trees']
        stats_permits_address_join += st['permits_address_join']
        stats_permits_unjoined_per_parcel += st['permits_unjoined_per_parcel']
        # Merge per-worker claim sets so the parent's globally-deduplicated
        # views stay correct (heritage stats + permit aggregations).
        claimed_heritage_indices |= result['heritage_claims']
        for nb_name, claims in result['permit_claims_by_nb'].items():
            permits_claims_by_neighborhood.setdefault(nb_name, []).extend(claims)

    # Loop body fully extracted to `_process_parcel` (2026-05-06 multiproc
    # refactor). The legacy inline body is gone — single source of truth.


    # Sort by max(score, softScore) so soft-only parcels (the >500m opt-in
    # catchment, score=0 but softScore>0) appear in the top-N projection
    # alongside in-buffer parcels — secondary key on score keeps in-buffer
    # parcels first when softScore values tie.
    features.sort(
        key=lambda f: (
            max(f["properties"]["score"], f["properties"].get("softScore") or 0),
            f["properties"]["score"],
        ),
        reverse=True,
    )

    # Heritage records that didn't match any parcel via address-join or
    # point-in-parcel. Logged for operator triage.
    stats_heritage_unjoined = len(heritage_index.points) - len(claimed_heritage_indices)

    # ── Second pass: stamp neighborhoodPermitComp onto each feature ──
    # The medians need every neighborhood's full claimed-permit set, which
    # only exists after the parcel loop. Loop is in-memory so cost is O(N).
    nb_perm_comp = permits_src.aggregate_per_neighborhood(
        permits_claims_by_neighborhood,
        permit_index.permits,
        permit_freshness_cutoff,
        freshness_years=permits_src.DEFAULT_FRESHNESS_YEARS,
        min_sample_size=permits_src.MIN_NEIGHBORHOOD_SAMPLE_SIZE,
    )
    default_nb_comp = {
        "medianCostPerUnit": None,
        "sampleSize": 0,
        "freshnessYears": permits_src.DEFAULT_FRESHNESS_YEARS,
    }
    for feat in features:
        nb_name = feat["properties"]["neighborhood"]
        comp = nb_perm_comp.get(nb_name, default_nb_comp)
        feat["properties"]["neighborhoodPermitComp"] = comp
        # `sixplexBonusValueCad`: revenue uplift if the lot uses its sixplex
        # carve-out (2 extra units vs the citywide 4-cap). Computed here
        # because the median cost-per-unit input only exists after the
        # neighborhood comp aggregation. Null when the parcel isn't sixplex-
        # eligible OR the neighborhood has insufficient permit sample to
        # ground a median (the wire keeps None vs 0 to distinguish "no
        # bonus" from "no estimate available").
        if feat["properties"].get("sixplexEligible") and comp.get("medianCostPerUnit"):
            feat["properties"]["sixplexBonusValueCad"] = int(2 * comp["medianCostPerUnit"])
        # else: stays at the None placeholder set in the loop body above.

    # Multi-proc safe: count claimed permits from the merged
    # `permits_claims_by_neighborhood` dict (which IS up-to-date in the
    # parent), not from `permit_index.claimed` (whose mutations stay
    # worker-local under fork-COW). For sequential runs the two are
    # equivalent — `_W['claimed_heritage_indices']` is the parent set.
    all_claimed_permits = set()
    for claims in permits_claims_by_neighborhood.values():
        all_claimed_permits.update(claims)
    permits_joined_count = len(all_claimed_permits)
    permits_unjoined_count = len(permit_index.permits) - permits_joined_count
    _log.info(
        "permits joined: %d address, 0 spatial fallback, %d unjoined",
        permits_joined_count, permits_unjoined_count,
    )

    meta = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sourceVersions": dict(SOURCE_VERSIONS),
        "scoreFormula": parcel_scoring.FORMULA_TEXT,
        "bloomFormula": parcel_scoring.BLOOM_FORMULA_TEXT,
        "solarMethodology": parcel_scoring.SOLAR_METHODOLOGY_TEXT,
        "shadowAnalysis": {
            "sunAngles": list(shadow_analysis.REFERENCE_ANGLES),
            "searchRadiusM": shadow_analysis.DEFAULT_SEARCH_RADIUS_M,
            "projectionMethod": shadow_analysis.SHADOW_PROJECTION_METHOD,
        },
        # Solar normalization constants surfaced so the frontend can re-derive
        # absolute kWh from `solarScoreRaw` if it ever needs to (the wire also
        # ships `solarYieldKwhPerYr` directly per-parcel — these are for any
        # downstream consumer that wants the math itself).
        "solarConstants": {
            "p95Kwh": int(round(solar_p95)) if solar_p95 else 0,
            "pvYieldKwhPerKw": _TORONTO_PV_YIELD_KWH_PER_KW,
            "pvYieldNote": (
                "Toronto-latitude rule of thumb: 1 kW of installed PV generates "
                "~1,150 kWh/yr south-facing, 30° tilt, unshaded. Use as a back-of-"
                "envelope conversion only — actual yield varies with tilt, azimuth, "
                "shading, panel efficiency, and inverter losses."
            ),
        },
        "permits": {
            "totalPermitsKept": len(permit_index.permits),
            "joinedByAddress": len(permit_index.claimed),
            "joinedBySpatialFallback": 0,  # source has no lat/lng — see building_permits.py
            "unjoined": permits_unjoined_count,
            "freshnessYears": permits_src.DEFAULT_FRESHNESS_YEARS,
            "sanityCeilingCad": permits_src.SANITY_VALUE_CEILING_CAD,
            "minNeighborhoodSampleSize": permits_src.MIN_NEIGHBORHOOD_SAMPLE_SIZE,
            "denominatorLabel": "declared_construction_cost_cad",
            "denominatorPerUnit": True,
            "notes": (
                "Permit values are the declared construction cost on the building permit "
                "application. They are NOT market sale prices, assessed values, or final "
                "build costs. Per-neighborhood denominator is dwelling-units-created "
                "(source CSV omits floor area)."
            ),
        },
        "stats": {
            "totalParcels": stats_total,
            "scorePositive": stats_score_pos,
            "heritagePartIV": stats_heritage_part_iv,
            "heritagePartV": stats_heritage_part_v,
            "heritageListed": stats_heritage_listed,
            "heritageUnjoined": stats_heritage_unjoined,
            "residential": stats_residential,
            "cornerLot": stats_corner,
            "postwar": stats_postwar,
            "bloom": stats_bloom,
            "skippedNoNeighborhood": stats_skipped_no_nb,
            "skippedNonBuildable": stats_skipped_non_buildable,
            "skippedUnparseableGeometry": stats_skipped_unparseable,
            "skippedInstitutional": stats_skipped_institutional,
            "skippedInstitutionalByCategory": dict(institutional_by_category),
            "skippedTtcStation": stats_skipped_ttc_station,
            "outsideTransitBuffer": stats_outside_transit_buffer,
            "abutsLaneway": stats_abuts_laneway,
            "nearRapidToCorridor": stats_near_rapidto,
            "inFloodingStudyArea": stats_in_flooding_area,
            "inRegulatedArea": stats_in_regulated_area,
            "matureTrees": stats_mature_trees,
            "sixplexEligible": stats_sixplex_eligible,
        },
    }

    return {"type": "FeatureCollection", "meta": meta, "features": features}


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="Build BloomTO data/parcels.geojson from Toronto Open Data.",
    )
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help=f"output GeoJSON path (default: {DEFAULT_OUT.relative_to(PROJECT_ROOT)})")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE,
                   help=f"download cache dir (default: {DEFAULT_CACHE.relative_to(PROJECT_ROOT)})")
    p.add_argument("--shard-by-neighborhood", action="store_true",
                   help="emit per-neighborhood shards under data/parcels/ instead of one file (≥25 MB hint)")
    p.add_argument("--include-non-eligible", action="store_true",
                   help="keep parcels with score=0 (non-residential / heritage / no major transit) on the wire")
    p.add_argument("--quiet", action="store_true",
                   help="suppress progress logs (errors still printed)")
    import os as _os
    p.add_argument("--workers", type=int, default=1,
                   help="number of parallel worker processes for the per-parcel loop. "
                        f"1 = sequential (default, safest). Try {_os.cpu_count() or 8} to use all cores. "
                        "Linux fork+COW means heavy indexes are shared across workers — memory grows ~1.5×, not N×. "
                        "Stats counters are slightly approximate under parallel mode (heritage/permit "
                        "deduplication crosses worker boundaries imperfectly), but per-parcel data is bit-identical.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cache = args.cache_dir
    started = time.monotonic()

    t = _stage("fetch neighborhoods")
    neighborhoods = neighborhoods_src.fetch_neighborhoods(cache)
    _done("fetch neighborhoods", t)

    t = _stage("compute census (for builtYear → postwar flag)")
    built_year_by_name, _existing, _fb = census_src.compute_census(neighborhoods, cache)
    _done("census", t)

    t = _stage("compute heritage")
    heritage_index = heritage_src.compute_heritage(cache)
    _done("heritage", t)

    t = _stage("compute institutions (schools, parks, places of worship, etc.)")
    institutions_index = institutions_src.compute_institutions(cache)
    _done("institutions", t)

    t = _stage("compute TTC subway-station exclusion (buffered subway stops)")
    ttc_station_index = ttc_stations_src.compute_station_exclusion_index(cache)
    _done("TTC station exclusion", t)

    t = _stage("compute basement-flooding study areas")
    flood_index = flood_src.compute_flood_index(cache)
    _done("flood", t)

    t = _stage("compute TRCA Regulated Area (Ont. Reg. 41/24 riverine)")
    trca_index = trca_src.compute_trca_index(cache)
    _done("trca", t)

    # Major-transit (subway∪streetcar) tree was previously loaded here for an
    # independent distSubwayStreetcarM query. Now derived as min(subway,
    # streetcar) inside the loop, so the union tree is redundant.

    # Stops are loaded as (lng, lat) Points, then projected to EPSG:26917
    # metres for the STRtree. `_distance_to_nearest_stop_m` projects the
    # parcel point through the same Transformer at query time, so
    # `STRtree.nearest` is exact in metres (vs. the previous degree-space
    # planar metric, which mis-ranked ~15% of subway nearest-stop picks).
    t = _stage("compute subway-only stops (projected to metres)")
    subway_tree = _build_stops_tree_m(ttc_src.compute_subway_stops(cache))
    _done("subway stops", t)

    t = _stage("compute streetcar-only stops (projected to metres)")
    streetcar_only_tree = _build_stops_tree_m(ttc_src.compute_streetcar_stops(cache))
    _done("streetcar-only stops", t)

    t = _stage("compute bus stops (projected to metres)")
    bus_tree = _build_stops_tree_m(ttc_src.compute_bus_stops(cache))
    _done("bus stops", t)

    t = _stage("load zone index")
    zone_index = zoning_src.load_zone_index(cache)
    multipliers = zoning_src._load_multipliers()
    _done("zone index", t)

    t = _stage("load building outlines")
    bo_cache = bo_src._ensure_cached(cache)
    building_geoms = bo_src._load_building_polygons(bo_cache)
    building_tree = STRtree(building_geoms)
    _done("building outlines", t)

    t = _stage("load 3D Massing index")
    massing_index = massing_src.load_massing_index(cache)
    _done("massing", t)

    t = _stage("compute SolarTO points + P95")
    solar_tree, solar_kwh = solar_src.compute_solar_points(cache)
    solar_p95 = solar_src.kwh_p95(solar_kwh)
    _log.info("solar P95 = %.0f kWh", solar_p95)
    _done("solar", t)

    t = _stage("load centreline index (corner-lot + laneway lookup)")
    centreline_index = streets_src.load_centreline_index(cache)
    _done("centreline index", t)

    t = _stage("build RapidTO corridor index (5 named arterials)")
    rapidto_tree = _load_rapidto_index(cache)
    _done("rapidto index", t)

    t = _stage("load Toronto building permits (residential new-build / conversion)")
    permit_index = permits_src.compute_permits(cache)
    permit_freshness_cutoff = permits_src.freshness_cutoff()
    _done("permits", t)

    t = _stage("load cycling network (per-parcel distance index)")
    bike_tree, bike_lines = cycling_src.load_cycling_index(cache)
    _done("cycling index", t)

    t = _stage("load Street Tree Data (~700K trees, DBH-tagged)")
    street_tree_index = street_trees_src.compute_street_trees(cache)
    _done("street trees", t)

    t = _stage("load sixplex-eligible district overlay (T&EY + Ward 23)")
    sixplex_index = sixplex_src.compute_sixplex_index(cache)
    _done("sixplex district", t)

    # Pre-compute neighborhood canopy lookup so the assembly loop reads it
    # by name in O(1). Source is the v1.1 wire format `data/neighborhoods.json`
    # (NOT the Neighborhood dataclass, which only carries spatial-join fields).
    # Falls back to None per neighborhood when the file is absent or stale —
    # build_parcels emits null for those parcels' `neighborhoodCanopyPct`.
    nb_canopy_by_name: dict[str, int] = {}
    try:
        nb_json_path = Path("data/neighborhoods.json")
        if nb_json_path.exists():
            with nb_json_path.open(encoding="utf-8") as fp:
                nb_data = json.load(fp)
            for nb in nb_data.get("neighborhoods", []):
                if "name" in nb and "canopy" in nb:
                    nb_canopy_by_name[nb["name"]] = nb["canopy"]
            _log.info("nb_canopy: %d neighborhood canopy values loaded from data/neighborhoods.json",
                      len(nb_canopy_by_name))
        else:
            _log.warning("nb_canopy: data/neighborhoods.json absent — canopy passthrough will be null per parcel")
    except Exception as e:
        _log.warning("nb_canopy: could not load data/neighborhoods.json (%s) — passthrough null", e)

    t = _stage("assemble parcel features (streaming iter_parcels)")
    # Stream parcels via the generator — never materializing the ~500k list.
    # The corner-lot test is now inline (`streets.is_corner_lot`), eliminating
    # the up-front `compute_corner_lots(list(iter_parcels(...)))` peak that
    # previously held all parcel geometries in memory at once.
    payload = assemble_parcel_payload(
        neighborhoods=neighborhoods,
        # Multiprocessing fast-path: stream cheap record dicts, let workers
        # do the GEOS-heavy parsing in parallel. Sequential path uses the
        # eager iter_parcels (already-materialized Parcel objects) since
        # there's no benefit to deferring the work.
        parcels=(zoning_src.iter_parcel_records(cache) if args.workers > 1
                 else zoning_src.iter_parcels(cache)),
        heritage_index=heritage_index,
        institutions_index=institutions_index,
        ttc_station_index=ttc_station_index,
        flood_index=flood_index,
        trca_index=trca_index,
        rapidto_tree=rapidto_tree,
        zone_index=zone_index,
        multipliers=multipliers,
        transit_subway_tree=subway_tree,
        transit_streetcar_only_tree=streetcar_only_tree,
        transit_bus_tree=bus_tree,
        massing_index=massing_index,
        building_geoms=building_geoms,
        building_tree=building_tree,
        solar_tree=solar_tree,
        solar_kwh=solar_kwh,
        solar_p95=solar_p95,
        centreline_index=centreline_index,
        built_year_by_name=built_year_by_name,
        permit_index=permit_index,
        permit_freshness_cutoff=permit_freshness_cutoff,
        bike_tree=bike_tree,
        bike_lines=bike_lines,
        street_tree_index=street_tree_index,
        sixplex_index=sixplex_index,
        nb_canopy_by_name=nb_canopy_by_name,
        include_non_eligible=args.include_non_eligible,
        workers=args.workers,
    )
    _done("assemble", t)

    t = _stage(f"write {args.out}")
    parcel_io.write_atomic(payload, args.out)
    _done("write", t)

    stats = payload["meta"]["stats"]
    out_size_mb = args.out.stat().st_size / (1 << 20)
    elapsed = time.monotonic() - started
    _log.info(
        "DONE: %d parcels (%d score>0, %d Part IV / %d Part V / %d Listed / %d unjoined, "
        "%d residential, %d corner, %d postwar, %d bloom, %d skipped institutional) → %s | %.1f MB | %.1fs",
        stats["totalParcels"], stats["scorePositive"],
        stats["heritagePartIV"], stats["heritagePartV"], stats["heritageListed"],
        stats["heritageUnjoined"],
        stats["residential"], stats["cornerLot"], stats["postwar"], stats["bloom"],
        stats.get("skippedInstitutional", 0),
        args.out, out_size_mb, elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
