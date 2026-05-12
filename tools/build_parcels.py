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
from shapely.geometry import LineString, Point, box
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

from tools import parcel_io, parcel_scoring, shadow_analysis
from tools.sources import (
    address_points as ap_src,
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
    osm_landuse as landuse_src,
    osm_buildings as osm_src,
    tax_exemptions as tax_exempt_src,
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
      residential, sixplex_eligible, corner, postwar,
      heritage_part_iv, heritage_part_v, heritage_listed,
      abuts_laneway, near_rapidto,
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
    landuse_index = _W['landuse_index']
    amenity_holdover_index = _W.get('amenity_holdover_index')
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

    zone_tree, zone_records = zone_index
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

    # --- gate stage 3b: OSM landuse (parking / industrial / construction / brownfield) ---
    # Replaces the dead Address Points USE_CODE path. Catches parcels that
    # are physically a parking lot or industrial / brownfield / construction
    # site even when zoning data alone classifies them as residential.
    if landuse_src.is_landuse_excluded(parcel.geometry, landuse_index):
        return {'skip': 'osm_landuse'}

    # --- gate stage 3d: tax-exempt institutional address ---
    # Catches Royal Canadian Legion halls, registered charities, city-
    # owned community facilities, universities, etc. — institutional uses
    # whose zoning permits residential redevelopment but which won't sell
    # to a multiplex dev. 677 unique addresses citywide; the gate is
    # address-join (parcel.address normalized → exempt set lookup).
    if tax_exempt_src.is_tax_exempt(parcel.address, _W['tax_exempt_addrs']):
        return {'skip': 'tax_exempt'}

    # --- gate stage 3c: tall existing building (3D Massing) ---
    # Excludes parcels already carrying a 5+ storey structure — apartment
    # buildings and mid-rises where teardown economics fail vs the 4–6 unit
    # multiplex envelope. Computed once, reused below for the wire field
    # so the frontend can show "currently a 4-storey building" inline.
    existing_max_h = _existing_max_building_height(parcel, massing_index, _W.get('address_points_index'))
    if existing_max_h is not None and existing_max_h >= EXISTING_BUILDING_HEIGHT_THRESHOLD_M:
        return {'skip': 'tall_existing_building'}
    # --- gate stage 3d: implied-FSI vs zone-FSI mismatch (added 2026-05-09) ---
    # Catches non-conforming apartments that squeak under the 18m hard cap
    # by being 17m tall on a low-FSI residential lot. The 1 Leonard Crcl
    # case (RD zone d=0.35 FSI, existing 17.8m × ~152m² footprint × 6
    # storeys = ~1.2 implied FSI = 3.4× over the zone cap). No real
    # detached SFH triples its zone's FSI cap; that signature is an
    # apartment building tagged as "detached" in stale permit data.
    # Threshold = 2× zone FSI to leave headroom for legit pre-WW2 stock
    # that mildly exceeds modern setbacks. Only fires when zone_fsi is
    # known; missing zone data falls through (no false positives).
    if (existing_max_h is not None and existing_max_h > 5
            and parcel.area_m2 > 0):
        # Need zone_record loaded for FSI; lift the lookup ahead of stage 4.
        _gate_zone_record = _lookup_zone_record(parcel, zone_tree, zone_records)
        _gate_zone_fsi = _gate_zone_record.fsi if _gate_zone_record else None
        if _gate_zone_fsi is not None and _gate_zone_fsi > 0:
            _building_area_m2 = 0.0
            for idx in building_tree.query(parcel.geometry):
                try:
                    inter = building_geoms[idx].intersection(parcel.geometry)
                    if inter.is_empty:
                        continue
                    a, _ = _GEOD.geometry_area_perimeter(inter)
                    _building_area_m2 += abs(a)
                except Exception:
                    continue
            if _building_area_m2 > 0:
                _storeys = max(1, round(existing_max_h / 3.0))
                _implied_fsi = (_building_area_m2 * _storeys) / parcel.area_m2
                # 3× threshold tuned 2026-05-09 — catches 1 Leonard Crcl /
                # 324 Riverside Dr (~3.4× over) without nuking legit pre-
                # WW2 detached stock with steeply pitched roofs (commonly
                # 2.0-2.5× over their RD d=0.35 cap because 3D Massing
                # measures roof apex height, not storey count). 2× was
                # too aggressive in calibration (13% of elite hidden);
                # 3× hits 1.6% which matches the actual apartment-on-RD
                # signature.
                if _implied_fsi > 3.0 * _gate_zone_fsi:
                    return {'skip': 'implied_fsi_mismatch'}

    # --- per-parcel work ---
    zone_record = _lookup_zone_record(parcel, zone_tree, zone_records)
    zone_class = zone_record.zone_class if zone_record else ""
    max_units, max_units_rationale = _derive_max_units(
        zone_record, multipliers, parcel.area_m2,
    )
    residential = max_units > 0

    sixplex_eligible = sixplex_src.is_sixplex_eligible(parcel.geometry, sixplex_index)
    # Sixplex carve-out (T&EY/Ward 23, June 2025) lifts the cap to 6 only
    # when the by-law's own number is below 6 — RM/RA/CR with explicit
    # higher caps already exceed 6 and shouldn't be reduced.
    if sixplex_eligible and residential and max_units < 6:
        max_units = 6
        max_units_rationale = "sixplex_carveout"

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
    # later short-circuits via the eligibility gate. Without this the
    # parallel run would report 0 Part IV (because Part IV parcels are
    # filtered → never counted).
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

    # Eligibility gates (replaces the prior synthesised score-zero gate).
    # Each is a binary check on a city primitive; passing means the parcel
    # is multiplex-eligible. Identical exclusion shape as before — the
    # *combined* gate was previously expressed as `score == 0`.
    is_eligible = (
        residential
        and heritage_status != 'part_iv'  # Part IV bars redevelopment outright
        and dist_subway_streetcar_m is not None
        and dist_subway_streetcar_m < ELIGIBLE_TRANSIT_BUFFER_M  # 1500m wide window
        and parcel.area_m2 >= ELIGIBLE_MIN_LOT_AREA_M2
    )
    if not is_eligible and not include_non_eligible:
        return {
            'skip': 'not_eligible',
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

    # Address-points spatial query — done up-front because the count is
    # surfaced as its own wire field (`addressPointCount`) regardless of
    # whether it overrides anything below. AP=1 + structure=detached →
    # frontend "True Detached" badge. AP≥2 → "multi-unit existing" signal
    # (separate from structure type).
    address_points_index = _W.get('address_points_index')
    address_point_count = 0
    ap_attached_verdict: str | None = None
    if address_points_index is not None and parcel.geometry is not None:
        # Inset the parcel polygon by ~0.5m before the AP containment test
        # to guard against boundary fuzziness (177 Symons spillover case,
        # 2026-05-09). Without the inset, a neighbour's address point that
        # falls just inside our boundary would bump `addressPointCount` and
        # wrongly trigger the multi-unit attached-housing classifier. With
        # the inset, only address points that sit cleanly inside this parcel
        # count. Falls back to the original geometry on degenerate insets
        # (very small parcels where buffer(-x) collapses to empty/invalid).
        ap_test_geom = parcel.geometry.buffer(-ADDRESS_POINT_INSET_DEG)
        if ap_test_geom.is_empty or not ap_test_geom.is_valid:
            ap_test_geom = parcel.geometry
        ap_records = ap_src.points_in_parcel(address_points_index, ap_test_geom)
        # Distinct addresses (a parcel can host duplicate point records for
        # the same address — e.g., front-door + side-door — which we don't
        # want to double-count).
        distinct = {pt.address_full.upper().strip() for pt in ap_records}
        address_point_count = len(distinct)
        ap_attached_verdict, _ = ap_src.classify_attachment_from_points(ap_records)
        # Address-record drift gate (added 2026-05-11 — 106 Eastwood Rd
        # case: parcel polygon's ADDRESS field said "106 Eastwood Rd" but
        # the Address Points inside the polygon resolved to a different
        # street ("143 Edgewood Ave"). The wire-attached address was
        # unverifiable against the city's own location database. If the
        # parcel's claimed address doesn't appear in any AP inside the
        # polygon, flag the parcel so the elite gate can reject it.
        # Falls back to "not suspect" when no parcel.address is present
        # (handled by the existing "addressed" filter upstream).
        if parcel.address and ap_records:
            parcel_addr_norm = heritage_src.normalize_address(parcel.address)
            ap_addrs_norm = {pt.address_norm for pt in ap_records}
            if parcel_addr_norm and parcel_addr_norm not in ap_addrs_norm:
                # No matching address point inside the polygon — drift.
                # Mark on the wire so elite gate + frontend can react.
                # We DON'T outright skip here — leave the parcel in
                # broader tier with a flag; elite tier rejects via gate.
                address_drift_suspect = True
            else:
                address_drift_suspect = False
        else:
            address_drift_suspect = False
    else:
        address_drift_suspect = False

    # Structure type — four-tier waterfall:
    #   1. Permit-derived (city building-permit STRUCTURE_TYPE record) — ~32 %
    #      coverage citywide, ~43 % of curated. Highest confidence.
    #   2. OSM-derived (OpenStreetMap `building=*` tag, volunteer-mapped) —
    #      ~12 % citywide additional coverage, complementary to permits
    #      (4 % overlap). 96 % agreement with permits where they overlap.
    #   3. Cross-boundary classifier fallback — ~82 % accuracy heuristic,
    #      always available.
    #   4. Address-points override (added 2026-05-09): when the classifier
    #      verdict is in play (no permit/OSM ground truth) AND the parcel
    #      polygon contains ≥2 distinct address points, flip the verdict
    #      with high confidence — multiple municipal addresses on one
    #      polygon is near-deterministic for attached housing. Source flips
    #      to "address_points". Conservative: never overrides permit/OSM,
    #      never overrides "vacant".
    permit_struct_type = None
    osm_struct_type = None
    norm_addr = (
        heritage_src.normalize_address(parcel.address) if parcel.address else ""
    )
    if norm_addr:
        permit_struct_type = _W['permit_structure_type_by_addr'].get(norm_addr)
    if permit_struct_type:
        existing_structure_type = permit_struct_type
        existing_structure_source = "permit"
    elif parcel.address:
        osm_struct_type = osm_src.lookup_osm_structure(
            parcel.address, _W['osm_structure_type_by_addr'],
        )
        if osm_struct_type:
            existing_structure_type = osm_struct_type
            existing_structure_source = "osm"
        else:
            existing_structure_type = _classify_existing_structure(
                parcel, building_tree, building_geoms,
            )
            existing_structure_source = "classifier" if existing_structure_type != "vacant" else "vacant"
    else:
        existing_structure_type = _classify_existing_structure(
            parcel, building_tree, building_geoms,
        )
        existing_structure_source = "classifier" if existing_structure_type != "vacant" else "vacant"
    # Address-points override (only applies to classifier-derived rows).
    if (existing_structure_source == "classifier"
            and ap_attached_verdict is not None):
        existing_structure_type = ap_attached_verdict
        existing_structure_source = "address_points"
    # Classifier-on-low-coverage demotion (added 2026-05-09 — 158 Dufferin
    # St case). The cross-boundary side-yard classifier returns "detached"
    # whenever the building has clear side-yards, regardless of whether
    # the building is plausibly residential. On parcels with very low
    # coverage (<10%), the structure is likely a derelict shed / industrial
    # outbuilding / back-lot residue, not an SFH. Demote to "unknown" so
    # the parcel fails the elite-tier {detached, vacant} gate. Permit/OSM-
    # sourced verdicts are ground truth and are not affected. Address-
    # points-overridden verdicts are also skipped (they fired BEFORE this
    # gate, and AP=2+ on a low-coverage lot is its own valid signal).
    if (existing_structure_source == "classifier"
            and existing_structure_type == "detached"
            and coverage < 0.10):
        existing_structure_type = "unknown"
        existing_structure_source = "classifier_low_cov_demotion"
    # Classifier-on-CR-zone demotion (added 2026-05-09 — 581 Parliament
    # St case). CR / CRE / RAC / RA / CL are commercial-residential and
    # mid-rise residential zones — typically mainstreet retail with
    # apartment-above or mid-rise apartment buildings, NOT detached SFHs.
    # When the classifier guesses "detached" on a CR-zoned parcel without
    # permit/OSM ground truth, the verdict is unreliable: a real detached
    # SFH on a CR lot would typically be permit- or OSM-tagged. Demoting
    # cleans up Cabbagetown / Danforth / Bloor / College mainstreet
    # parcels that would otherwise misrepresent as "detached SFH teardown."
    elif (existing_structure_source == "classifier"
            and existing_structure_type == "detached"
            and zone_class in ("CR", "CRE", "RAC", "RA", "CL")):
        existing_structure_type = "unknown"
        existing_structure_source = "classifier_cr_zone_demotion"
    # False-vacant demotion (added 2026-05-11 — 106 Eastwood Rd / 14 elite
    # parcels surfaced). The geometry classifier returns "vacant" when no
    # qualifying building polygon is found in 3D Massing OR Building
    # Outlines. Both datasets have coverage gaps, so "vacant" can mean
    # "geometry data missed it" rather than "really no structure." If
    # Toronto Address Points registers ≥1 municipal address inside the
    # parcel polygon, a structure exists per the city's own records —
    # demote to "unknown" so the parcel drops from the elite cohort
    # (which gates on detached+vacant only) instead of falsely claiming
    # "no demolition required."
    if (existing_structure_type == "vacant"
            and address_point_count >= 1):
        existing_structure_type = "unknown"
        existing_structure_source = "false_vacant_demotion"
    corner = streets_src.is_corner_lot(parcel, centreline_tree, centreline_name_ids)
    abuts_laneway = _abuts_laneway(parcel, centreline_tree, centreline_laneway_idx)
    near_rapidto = _near_rapidto(parcel, rapidto_tree)
    # Distance from the parcel's representative point (rep_pt) to the
    # nearest centreline geometry. Surfaces as `addrToStreetM` on the wire;
    # combined with `abutsLaneway` it exposes back-lot residue parcels
    # (1030 Danforth / 1558 Davenport pattern — address geocodes to a
    # frontage that this parcel sits BEHIND, accessed only by laneway).
    addr_to_street_m = streets_src.dist_addr_to_centreline_m(rep_pt, centreline_tree)
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

    # Existing-units derivation (Item 4, 2026-05-09). Three-tier precedence:
    #   1. Permits — DWELLING_UNITS_EXISTING from the most-recent permit
    #      joined to this parcel's address. Highest confidence.
    #   2. Height × footprint — storeys × footprint / 90 m²/unit (CMHC
    #      residential per-unit average). Medium confidence; misjoins or
    #      spillover would inflate the count.
    #   3. Height band — pure height-bucket fallback when footprint is
    #      missing. Low confidence; surfaces "any structure at all" rough
    #      tier.
    # Vacant lots emit 0/'vacant' explicitly. No-signal parcels emit
    # null/'unknown' — frontend renders "—" when basis is unknown.
    existing_units_approx: int | None = None
    existing_units_basis = "unknown"
    if existing_structure_type == "vacant":
        existing_units_approx = 0
        existing_units_basis = "vacant"
    else:
        units_from_permits = permits_src.existing_units_from_permits(
            local_permit_claims, permit_index.permits,
        )
        if units_from_permits is not None and units_from_permits > 0:
            existing_units_approx = int(units_from_permits)
            existing_units_basis = "permits"
        elif (existing_max_h is not None and existing_max_h > 0
                and building_area_m2 > 0):
            storeys = max(1, round(existing_max_h / 3.0))
            if storeys <= 20:  # sanity cap — taller suggests massing misjoin
                floor_area = building_area_m2 * storeys
                existing_units_approx = max(1, round(floor_area / 90.0))
                existing_units_basis = "height_x_footprint"
        elif existing_max_h is not None and existing_max_h > 0:
            if existing_max_h < 4:
                existing_units_approx = 1
            elif existing_max_h < 8:
                existing_units_approx = 2
            elif existing_max_h < 12:
                existing_units_approx = 4
            else:
                existing_units_approx = 8
            existing_units_basis = "height_band"

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
            'zoneClass': zone_class,
            'maxUnits': int(max_units),
            'maxUnitsRationale': max_units_rationale,
            'zoneString': zone_record.zone_string if zone_record else "",
            'zoneFsi': zone_record.fsi if zone_record else None,
            'zoneMinLotFrontageM': zone_record.min_lot_frontage_m if zone_record else None,
            'zoneMinLotAreaM2': zone_record.min_lot_area_m2 if zone_record else None,
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
            'addrToStreetM': round(addr_to_street_m, 1),
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
            'lotGeometry': (lambda lo, sh, o: {
                'longAxisM': lo, 'shortAxisM': sh, 'orientationDeg': o,
            })(*_lot_geometry(parcel)),
            'neighborHeights': (_nh_for_feat := _neighbor_heights(rep_pt, massing_index)),
            'existingMaxBuildingHeightM': (
                round(existing_max_h, 1) if existing_max_h is not None else None
            ),
            'existingStructureType': existing_structure_type,
            'existingStructureSource': existing_structure_source,
            'addressPointCount': int(address_point_count),
            'addressDriftSuspect': bool(address_drift_suspect),
            # Geometry-suspect flag (added 2026-05-11). True when the
            # height-attribution likely reflects a catastrophic polygon
            # mis-draw in Toronto's Property Boundaries dataset — either:
            #   (a) Tall (≥12 m) on low coverage (<20%) — 807 Glencairn
            #       pattern (apartment-neighbour spillover)
            #   (b) Existing height exactly matches a neighbour-height
            #       (within 0.1 m) — 177 Symons pattern
            # Used by `is_elite` in build_parcels_top.py as a hard reject.
            # Affected parcels drop to broader tier (still reachable) so
            # devs willing to verify on Street View can find them, but
            # elite stays 100%-trust.
            'geometrySuspect': bool(
                _compute_geometry_suspect(existing_max_h, coverage, _nh_for_feat)
            ),
            'existingUnitsApprox': existing_units_approx,
            'existingUnitsBasis': existing_units_basis,
            # OSM commercial-holdover amenity (added 2026-05-09): the
            # `amenity` tag (e.g., "fast_food", "restaurant", "bank") of
            # any building substantially sitting on this parcel. None
            # for residential / vacant lots. NOT a hard exclusion — the
            # 505 Jarvis case (A&W on R-zoned land) is a legitimate
            # teardown candidate; the dev needs to know about the
            # commercial holdover before they walk up to it.
            'osmAmenityType': (
                landuse_src.osm_amenity_type(parcel.geometry, amenity_holdover_index)
                if amenity_holdover_index is not None and parcel.geometry is not None
                else None
            ),
            'solarYieldKwhPerYr': int(round(max_kwh)) if max_kwh else 0,
            'pvCapacityKwEstimate': round(max_kwh / _TORONTO_PV_YIELD_KWH_PER_KW, 1) if max_kwh else 0.0,
            'sixplexBonusValueCad': None,
        },
    }

    stats_delta = {
        **early_stats,  # residential, sixplex_eligible, heritage_*
        'corner': 1 if corner else 0,
        'postwar': 1 if postwar else 0,
        'abuts_laneway': 1 if abuts_laneway else 0,
        'back_lot_candidate': 1 if (abuts_laneway and addr_to_street_m >= 15) else 0,
        'near_rapidto': 1 if near_rapidto else 0,
        'in_flooding_area': 1 if in_flooding_area else 0,
        'in_regulated_area': 1 if in_regulated_area else 0,
        'mature_trees': 1 if mature_tree_count > 0 else 0,
        'permits_address_join': permits_address_join,
        'permits_unjoined_per_parcel': 1 if permits_unjoined_per_parcel_bool else 0,
        f'structure_{existing_structure_type}': 1,
        # 1 iff the address-points override actually flipped this parcel's
        # structure verdict (was classifier, now address_points).
        'address_point_flip': 1 if existing_structure_source == "address_points" else 0,
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

# Eligibility gate constants (2026-05-07 — replaces synthesised
# score/softScore filter). A parcel must be residential, not Part IV
# heritage, within the wide transit window, and meet a minimum lot
# size — every condition is a binary check on a city primitive, no
# weighting. ELIGIBLE_TRANSIT_BUFFER_M is intentionally wide (matches
# the prior softScore window): downstream `_passes_shared` in
# build_parcels_top.py applies tighter binary gates for ELITE / BROADER
# tiers.
ELIGIBLE_TRANSIT_BUFFER_M = 1500
ELIGIBLE_MIN_LOT_AREA_M2 = 100

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


def _lookup_zone_record(parcel, zone_tree: STRtree, zone_records):
    """Return the `ZoneRecord` for the parcel, or None if no zone polygon
    contains its representative point.
    """
    rep = parcel.geometry.representative_point()
    for idx in zone_tree.query(rep):
        if zone_tree.geometries[idx].contains(rep):
            return zone_records[idx]
    return None


# Approximate Toronto multiplex unit floor area (median of 2-3 BR mix from
# recent permit data). Used to derive max-units from FSI when the by-law's
# UNITS field is not set explicitly.
ZONE_TYPICAL_UNIT_AREA_M2 = 85.0


def _derive_max_units(zone_record, multipliers: dict[str, int], lot_area_m2: float) -> tuple[int, str]:
    """Per-parcel max-units derivation from the by-law's actual parameters.

    Returns `(max_units, rationale)` where `rationale` is one of:
      'by_law_units'  — explicit UNITS field set in the zoning polygon
      'fsi_derived'   — derived from FSI_TOTAL × lot_area / typical_unit_area
      'zone_average'  — fallback to per-class average (zoning_multipliers.json)
                        when the polygon's by-law parameters are absent
      'unzoned'       — no zone record (parcel outside zoning boundary)

    Per-class average is the LOWER bound; if the by-law's actual cap is
    higher (e.g. by_law_units = 4 on RD lot, but zone_average says 4 too),
    we use the per-class. The point of the upgrade is honesty — if the
    by-law SAYS u4, surface that explicitly and label it `by_law_units`.
    """
    if zone_record is None:
        return 0, "unzoned"
    zone_class = zone_record.zone_class
    if not zone_class:
        return 0, "unzoned"
    # 0. Non-residential zones short-circuit FIRST. Employment / Institutional /
    #    Open Space / Utility classes have multipliers[zone] == 0 in the
    #    zoning_multipliers.json table — they're not multiplex territory
    #    regardless of what FSI_TOTAL the zoning polygon happens to carry
    #    (Employment polygons carry FSI for commercial massing, not housing).
    #    Without this guard, an 80,000 m² industrial lot at FSI 2.86 derives
    #    2,549 "units" via step 2, marking it residential=True and leaking
    #    into the broader-tier display. Verified on 2026-05-07 build:
    #    701 Runnymede Rd (zone=E, maxU=1134), 3003 Danforth Ave (zone=CR,
    #    maxU=2549, but CR genuinely is mixed-use so this case is fine),
    #    a cluster of E/EL parcels on Unwin Ave.
    if zone_class in multipliers and multipliers[zone_class] == 0:
        return 0, "non_residential"
    # 1. Explicit UNITS in the by-law text
    if zone_record.units is not None and zone_record.units > 0:
        return zone_record.units, "by_law_units"
    # 2. Derive from FSI envelope
    if zone_record.fsi is not None and zone_record.fsi > 0 and lot_area_m2 > 0:
        units = max(1, int(round(zone_record.fsi * lot_area_m2 / ZONE_TYPICAL_UNIT_AREA_M2)))
        return units, "fsi_derived"
    # 3. Per-class average fallback (legacy behavior)
    if zone_class in multipliers:
        return multipliers[zone_class], "zone_average"
    # Truly unknown zone class — surface loudly, with a pointer to the file
    # the operator needs to update (matches legacy `lookup_multiplier` text
    # so a future Toronto by-law amendment fails closed with an actionable
    # error rather than a silent wrong score).
    raise KeyError(
        f"unrecognized ZN_ZONE class {zone_class!r}; "
        f"add it to tools/zoning_multipliers.json"
    )


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

# Existing-building height threshold for the multiplex-teardown gate.
# 18m ≈ 6 storeys at 3m floor-to-floor. At/above this the existing
# structure is a real apartment building — teardown economics fail.
# Below this, the lot may carry a 1–5 storey structure that's still a
# legitimate teardown candidate. Bumped 2026-05-07 from 15m after user
# spot-check found 614 Dovercourt Rd at 14.5m — a real single-family
# Dufferin Grove home on a 2300m² lot with a steeply pitched roof
# (Toronto's pre-WW2 detached stock peaks 14–17m at the roof while
# being only 2.5–3 habitable storeys). 15m was over-blocking that
# cohort.
EXISTING_BUILDING_HEIGHT_THRESHOLD_M = 18.0
# Overlap-ratio gate for crediting a 3D Massing building's height to a
# parcel: the building's footprint must sit at least 80% inside the
# parcel polygon. Bumped 2026-05-09 from 0.5 after the 177 Symons St
# spillover case — a 3-4 storey apartment block on the neighbouring
# parcel had its footprint clip ~10-30% into 177 Symons via boundary
# fuzziness, and the prior 0.5 gate let the 9.1m height get credited to
# 177 Symons (actually a 1-storey bungalow per Street View). 0.80
# tightens the gate so a building must SUBSTANTIALLY sit on this parcel
# before its height counts. Cleanly excludes shared / clipped-neighbour
# structures from BOTH parcels — better to read "vacant or no recorded
# structure" than a wrong height.
EXISTING_BUILDING_OVERLAP_RATIO = 0.80
# Boundary-fuzziness inset applied to the parcel polygon before testing
# Address-Point containment. Same principle as the overlap ratio above:
# Toronto's Property Boundaries polygons can drift 0.5-1m at the edges
# (digitization imprecision, retired subdivision lines, etc), so a
# neighbour's address point that barely clips inside this parcel via
# the fuzz boundary should not bump `addressPointCount`. ~5e-6 deg is
# ~0.5m at Toronto's latitude (≈80-111km per degree depending on axis).
# Slight asymmetry between N-S and E-W is acceptable at this scale.
ADDRESS_POINT_INSET_DEG = 0.5 / 111_000

# Cross-boundary classifier (2026-05-08 — replaces the side-yard test
# against parcel edges, which was over-claiming detached because Toronto's
# Building Outlines polygons are drawn ~1m INSIDE the property line).
#
# New approach: measure the distance from this parcel's MAIN building to
# the NEAREST FOREIGN building (a building polygon on a neighbouring parcel,
# filtered to ≥ MIN_CLASSIFIER_BUILDING_M2 to ignore garages/sheds/decks
# masquerading as buildings).
#
# Tuned 2026-05-08 against 200 known-detached + 200 known-attached parcels:
#   - 50 m² outbuilding filter improves separation: detached median 5.93m vs
#     attached median 0.22m
#   - 1.5m threshold: 90% precision on excluding attached, 74% recall on
#     detached. Asymmetric on purpose — we accept missing some true detached
#     in exchange for very few attached leaking through.
SIDE_YARD_CLEAR_M = 1.5  # cross-building threshold (was 0.4 against parcel edge)
# Outbuilding cutoff for "is this a real residence" — sheds bottom at ~14m²,
# detached single-car garages ~18m², larger garages 25-50m². 50m² catches
# only main residences + larger granny suites / coach houses. Same threshold
# applies to MY building (must be ≥ this to count as classifier subject) and
# FOREIGN buildings (must be ≥ this to count as attachment-evidence).
MIN_CLASSIFIER_BUILDING_M2 = 50.0


def _existing_max_building_height(parcel, massing_index, address_points_index=None) -> float | None:
    """Height of the building anchored to THIS parcel's address point(s).

    2026-05-11 third rewrite. Prior rules (largest-overlap, then centroid-
    proximity) both failed on 807 Glencairn and 177 Symons because
    Toronto's Property Boundaries polygons for those parcels are mis-drawn
    enough that the neighbour's apartment-block footprint AND centroid sit
    inside the wrong polygon. Geometry algorithms reading the polygon as
    truth will be wrong as long as the polygon is wrong.

    The data-source fix: Address Points are an independent city dataset
    that records WHERE the front door of each address is located. They
    don't suffer from the boundary drift that affects Property Boundaries.
    For each candidate building, ask "where is the nearest registered
    address to this building's centroid?" If that address sits inside
    THIS parcel's polygon, the building belongs here. If it sits in a
    neighbour's polygon, the building belongs to the neighbour — even if
    our own polygon also (wrongly) contains the building's centroid.

    Algorithm:
      1. For each building intersecting the parcel polygon:
      2.   Find the nearest address point (across the whole city) to
           the building's centroid.
      3.   Test whether THAT address point sits inside this parcel.
      4.   If yes → eligible for height attribution.
      5.   If no → it's a neighbour's building; skip.
      6. Among eligible buildings, return the tallest height.

    Falls back to the centroid-proximity rule when `address_points_index`
    is None (defensive — should not happen in normal builds).
    """
    tree, buildings = massing_index
    parcel_geom = parcel.geometry
    best_height: float | None = None
    ap_tree = address_points_index.tree if address_points_index is not None else None
    ap_points = address_points_index.points if address_points_index is not None else None
    for idx in tree.query(parcel_geom):
        b = buildings[idx]
        if b.height_m is None:
            continue
        try:
            if not parcel_geom.intersects(b.geometry):
                continue
            centroid = b.geometry.centroid
            if ap_tree is not None:
                # STRtree.nearest returns the index of the geometry nearest
                # to the query point. The result is one of the ~750K
                # city-registered address points — the city's authoritative
                # record of "where is the front door for this number?"
                nearest_idx = ap_tree.nearest(centroid)
                nearest_ap = ap_points[nearest_idx]
                if not parcel_geom.contains(nearest_ap):
                    continue
            else:
                # Centroid-proximity fallback for builds without an AP index.
                if not parcel_geom.contains(centroid):
                    continue
        except Exception:
            continue
        if best_height is None or b.height_m > best_height:
            best_height = b.height_m
    return best_height


def _compute_geometry_suspect(existing_max_h, coverage, neighbor_heights) -> bool:
    """Heuristic: does this parcel's height-attribution look like a
    catastrophic polygon mis-draw (vs a real on-lot building)?

    Two independent triggers, either fires the flag:

      (a) **Tall on narrow**: existing height ≥ 12 m AND coverage < 20%.
          A 4-storey-ish building on what reads as a small footprint is
          geometrically incoherent for typical residential teardown
          candidates. 807 Glencairn (14.5 m / 17.2%) is the canonical
          case — the height comes from a neighbour's apartment block
          whose footprint lies inside our mis-drawn polygon.

      (b) **Exact neighbour match on a tall reading**: existing height
          ≥ 9 m AND matches one of the four neighbor-direction averages
          within 0.1 m. The 9 m floor matters — two adjacent 2-storey
          detached homes (typical Toronto stock at ~6 m) routinely share
          a height by virtue of being similar buildings, NOT because of
          spillover. The spillover hypothesis only makes sense when the
          height reads as apartment-block scale (~3+ storeys). 177 Symons
          (9.1 m matching N-side neighbour 9.1 m) is the canonical case.

    Returns False on missing data (existing_max_h is None, coverage
    is None or 0, etc.) to avoid false-flagging clean cases.
    """
    if existing_max_h is None or existing_max_h <= 0:
        return False
    if coverage is None or coverage <= 0:
        return False
    # (a) Tall + narrow
    if existing_max_h >= 12.0 and coverage < 0.20:
        return True
    # (b) Exact neighbour-height match on a narrow footprint. Requires
    # BOTH height ≥ 9 m AND coverage < 25%. Both conditions matter:
    # - Real 3-storey detached homes on full residential lots have
    #   25%+ coverage (large footprint) and often share heights with
    #   neighbouring same-vintage 3-storey homes — that's natural
    #   uniform-stock pattern, not spillover (46 High Park Blvd case,
    #   11.7m exact-match on 25.8% cov).
    # - Spillover cases combine a tall reading with a SMALL footprint
    #   — the bungalow is real, the height comes from a neighbour's
    #   apartment block creeping into the polygon (177 Symons St, 9.1m
    #   on 21.4% cov; 1030 Danforth Ave, 9.2m on 20.0% cov).
    if existing_max_h >= 9.0 and coverage < 0.25 and neighbor_heights:
        for k in ('nAvgM', 'sAvgM', 'eAvgM', 'wAvgM'):
            nh = neighbor_heights.get(k)
            if nh is not None and nh > 0 and abs(nh - existing_max_h) < 0.1:
                return True
    return False


def _classify_existing_structure(parcel, building_tree, building_geoms) -> str:
    """Classify by cross-boundary building proximity.

    Returns "detached" | "semi" | "row" | "vacant" | "unknown".

    Algorithm: find this parcel's main building polygon (largest >= 50 m²
    polygon overlapping the parcel). Find FOREIGN building polygons —
    polygons within ~10 m of the parcel that are not the main building and
    don't substantially overlap the parcel themselves. Filter foreign
    polygons to >= 50 m² (drops garages, sheds, decks; keeps main
    residences + coach houses). Project everything to UTM 17N (metres) and
    measure shapely.distance from the main building to each foreign
    building, taking the minimum.

    Verdict by count of foreign buildings within SIDE_YARD_CLEAR_M (1.5 m):
      0 close → "detached"
      1 close → "semi"
      2+ close → "row"

    Calibrated 2026-05-08 against 200 known-detached + 200 known-attached
    parcels — best separation at 1.5 m threshold + 50 m² outbuilding filter
    (90 % precision on excluding attached, 74 % recall on detached).
    Asymmetric by design: better to miss a true detached than to include
    an attached parcel in the elite cohort.
    """
    # 1. Find this parcel's main building (>= 50 m²)
    my_building_idxs = []
    for idx in building_tree.query(parcel.geometry):
        b = building_geoms[idx]
        try:
            if not parcel.geometry.intersects(b):
                continue
            inter = parcel.geometry.intersection(b)
            if inter.is_empty:
                continue
            inter_area_signed, _ = _GEOD.geometry_area_perimeter(inter)
            if abs(inter_area_signed) >= MIN_CLASSIFIER_BUILDING_M2:
                my_building_idxs.append(idx)
        except Exception:
            continue

    if not my_building_idxs:
        return "vacant"

    # Pick the largest as the "main"
    main_idx = max(my_building_idxs,
                   key=lambda i: building_geoms[i].area)
    main_geom = building_geoms[main_idx]

    # 2. Project main + parcel buffer to metres
    try:
        main_m = shp_transform(_LONLAT_TO_M.transform, main_geom)
    except Exception:
        return "unknown"
    if main_m.area < MIN_CLASSIFIER_BUILDING_M2:
        # Edge case — outbuilding survives the geodesic check but fails
        # the projected-area sanity filter. Treat as vacant.
        return "vacant"

    # 2.5. Merged-polygon case — Toronto's Building Outlines sometimes draws
    # one polygon spanning multiple parcels (a semi-pair as one building).
    # If MY main building extends >15% outside my parcel, it's clearly
    # attached. > 40% outside = three-or-more parcel span = row.
    try:
        inter = parcel.geometry.intersection(main_geom)
        if not inter.is_empty:
            inter_a_signed, _ = _GEOD.geometry_area_perimeter(inter)
            main_a_signed, _ = _GEOD.geometry_area_perimeter(main_geom)
            main_area_geo = abs(main_a_signed) or 1
            inside_ratio = abs(inter_a_signed) / main_area_geo
            outside_ratio = 1.0 - inside_ratio
            if outside_ratio > 0.40:
                return "row"
            if outside_ratio > 0.15:
                return "semi"
    except Exception:
        pass

    # 3. Search neighbouring buildings within ~10 m of this parcel
    parcel_buf = parcel.geometry.buffer(0.0001)  # ~10 m at Toronto lat
    foreign_close = 0
    seen = {main_idx} | set(my_building_idxs)
    for idx in building_tree.query(parcel_buf):
        if idx in seen:
            continue
        seen.add(idx)
        b = building_geoms[idx]
        try:
            # Skip if it substantially overlaps THIS parcel (it's another
            # of MY buildings, not foreign)
            if parcel.geometry.intersects(b):
                inter = parcel.geometry.intersection(b)
                if not inter.is_empty:
                    inter_area_signed, _ = _GEOD.geometry_area_perimeter(inter)
                    if b.area > 0 and abs(inter_area_signed) / (
                        _GEOD.geometry_area_perimeter(b)[0] or 1
                    ) > 0.05:
                        continue
            # Project + outbuilding filter
            b_m = shp_transform(_LONLAT_TO_M.transform, b)
            if b_m.area < MIN_CLASSIFIER_BUILDING_M2:
                continue
            if main_m.distance(b_m) <= SIDE_YARD_CLEAR_M:
                foreign_close += 1
                if foreign_close >= 2:
                    return "row"
        except Exception:
            continue

    if foreign_close == 0:
        return "detached"
    if foreign_close == 1:
        return "semi"
    return "row"


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
    landuse_index,
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
    permit_structure_type_by_addr: dict,
    osm_structure_type_by_addr: dict,
    address_points_index,
    tax_exempt_addrs: set,
    bike_tree: STRtree,
    bike_lines: list,
    street_tree_index,
    sixplex_index,
    nb_canopy_by_name: dict[str, int],
    include_non_eligible: bool,
    workers: int = 1,
    amenity_holdover_index=None,
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
    zone_tree, zone_records = zone_index
    centreline_tree, centreline_name_ids, centreline_laneway_idx = centreline_index

    features = []
    stats_total = 0
    stats_skipped_unparseable = 0
    stats_heritage_part_iv = 0
    stats_heritage_part_v = 0
    stats_heritage_listed = 0
    stats_residential = 0
    stats_corner = 0
    stats_postwar = 0
    stats_skipped_no_nb = 0
    stats_skipped_non_buildable = 0
    stats_skipped_institutional = 0
    stats_skipped_ttc_station = 0
    stats_skipped_osm_landuse = 0
    stats_skipped_tax_exempt = 0
    stats_skipped_tall_building = 0
    stats_skipped_implied_fsi = 0
    stats_abuts_laneway = 0
    stats_back_lot_candidates = 0
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
    stats_structure_counts = {
        "detached": 0, "semi": 0, "row": 0, "vacant": 0, "unknown": 0,
    }
    stats_address_point_flips = 0
    stats_osm_amenity_holdover = 0

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
        'landuse_index': landuse_index,
        'amenity_holdover_index': amenity_holdover_index,
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
        'permit_structure_type_by_addr': permit_structure_type_by_addr,
        'osm_structure_type_by_addr': osm_structure_type_by_addr,
        'address_points_index': address_points_index,
        'tax_exempt_addrs': tax_exempt_addrs,
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
            elif reason == 'osm_landuse':
                stats_skipped_osm_landuse += 1
            elif reason == 'tax_exempt':
                stats_skipped_tax_exempt += 1
            elif reason == 'tall_existing_building':
                stats_skipped_tall_building += 1
            elif reason == 'implied_fsi_mismatch':
                stats_skipped_implied_fsi += 1
            elif reason == 'non_buildable':
                stats_skipped_non_buildable += 1
            elif reason == 'unparseable_geometry':
                stats_skipped_unparseable += 1
            # 'not_eligible' is the bulk of skips — not counted in its own
            # bucket. Residential / sixplex / heritage counters DO need to
            # fire on these via the early_stats dict because the legacy
            # behavior incremented them BEFORE the score-zero (now
            # eligibility) gate.
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
        stats_residential += st['residential']
        stats_sixplex_eligible += st['sixplex_eligible']
        stats_corner += st['corner']
        stats_postwar += st['postwar']
        stats_heritage_part_iv += st['heritage_part_iv']
        stats_heritage_part_v += st['heritage_part_v']
        stats_heritage_listed += st['heritage_listed']
        stats_abuts_laneway += st['abuts_laneway']
        stats_back_lot_candidates += st.get('back_lot_candidate', 0)
        stats_near_rapidto += st['near_rapidto']
        stats_in_flooding_area += st['in_flooding_area']
        stats_in_regulated_area += st['in_regulated_area']
        stats_mature_trees += st['mature_trees']
        stats_permits_address_join += st['permits_address_join']
        stats_permits_unjoined_per_parcel += st['permits_unjoined_per_parcel']
        for kind in stats_structure_counts:
            stats_structure_counts[kind] += st.get(f'structure_{kind}', 0)
        stats_address_point_flips += st.get('address_point_flip', 0)
        stats_osm_amenity_holdover += st.get('osm_amenity_holdover', 0)
        # Merge per-worker claim sets so the parent's globally-deduplicated
        # views stay correct (heritage stats + permit aggregations).
        claimed_heritage_indices |= result['heritage_claims']
        for nb_name, claims in result['permit_claims_by_nb'].items():
            permits_claims_by_neighborhood.setdefault(nb_name, []).extend(claims)

    # Loop body fully extracted to `_process_parcel` (2026-05-06 multiproc
    # refactor). The legacy inline body is gone — single source of truth.


    # Default sort: lot area desc (replaces prior score-desc sort that
    # used a synthesised composite). Bigger lots first — single primitive,
    # multiplex-prospect signal (more land = more units = more revenue).
    # Frontend may re-sort by any other primitive; this is just the
    # canonical write-order.
    features.sort(
        key=lambda f: f["properties"].get("lotAreaM2") or 0,
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
            "heritagePartIV": stats_heritage_part_iv,
            "heritagePartV": stats_heritage_part_v,
            "heritageListed": stats_heritage_listed,
            "heritageUnjoined": stats_heritage_unjoined,
            "residential": stats_residential,
            "cornerLot": stats_corner,
            "postwar": stats_postwar,
            "skippedNoNeighborhood": stats_skipped_no_nb,
            "skippedNonBuildable": stats_skipped_non_buildable,
            "skippedUnparseableGeometry": stats_skipped_unparseable,
            "skippedInstitutional": stats_skipped_institutional,
            "skippedInstitutionalByCategory": dict(institutional_by_category),
            "skippedTtcStation": stats_skipped_ttc_station,
            "skippedOsmLanduse": stats_skipped_osm_landuse,
            "skippedTaxExempt": stats_skipped_tax_exempt,
            "skippedTallExistingBuilding": stats_skipped_tall_building,
            "skippedImpliedFsiMismatch": int(stats_skipped_implied_fsi),
            "abutsLaneway": stats_abuts_laneway,
            "backLotCandidates": int(stats_back_lot_candidates),
            "nearRapidToCorridor": stats_near_rapidto,
            "inFloodingStudyArea": stats_in_flooding_area,
            "inRegulatedArea": stats_in_regulated_area,
            "matureTrees": stats_mature_trees,
            "sixplexEligible": stats_sixplex_eligible,
            "existingStructureType": dict(stats_structure_counts),
            "addressPointFlips": int(stats_address_point_flips),
            "osmAmenityHoldover": int(stats_osm_amenity_holdover),
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

    t = _stage("compute OSM landuse exclusion (parking / industrial / construction / brownfield)")
    landuse_index = landuse_src.compute_landuse_exclusion_index(cache)
    _done("OSM landuse exclusion", t)

    t = _stage("compute OSM amenity holdover (fast_food / restaurant / bank / pharmacy on R-zoned land)")
    amenity_holdover_index = landuse_src.compute_amenity_holdover_index(cache)
    _done("OSM amenity holdover", t)

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
    permit_structure_type_by_addr = permits_src.build_structure_type_index(cache)
    osm_structure_type_by_addr = osm_src.build_osm_structure_type_index(cache)
    tax_exempt_addrs = tax_exempt_src.build_exempt_address_set(cache)
    permit_freshness_cutoff = permits_src.freshness_cutoff()
    _done("permits", t)

    t = _stage("load Address Points (Toronto One Address Repository, ~525K records)")
    address_points_index = ap_src.build_address_points_index(cache)
    _done("address points", t)

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
        landuse_index=landuse_index,
        amenity_holdover_index=amenity_holdover_index,
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
        permit_structure_type_by_addr=permit_structure_type_by_addr,
        osm_structure_type_by_addr=osm_structure_type_by_addr,
        address_points_index=address_points_index,
        tax_exempt_addrs=tax_exempt_addrs,
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
        "DONE: %d parcels (%d Part IV / %d Part V / %d Listed / %d unjoined, "
        "%d residential, %d corner, %d postwar, %d skipped institutional) → %s | %.1f MB | %.1fs",
        stats["totalParcels"],
        stats["heritagePartIV"], stats["heritagePartV"], stats["heritageListed"],
        stats["heritageUnjoined"],
        stats["residential"], stats["cornerLot"], stats["postwar"],
        stats.get("skippedInstitutional", 0),
        args.out, out_size_mb, elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
