"""Toronto Building Permits — Active Permits source loader.

Pulls Toronto's `building-permits-active-permits` CKAN dataset (CSV, ~50-80MB),
filters to residential new-build / conversion permits, and produces a
`PermitIndex` for `tools/build_parcels.py` to join against parcels.

## Wire-side adaptations from the spec (2026-05-04)

The spec at `.claude/specs/building-permits-source/` was written assuming the
CKAN feed carried lat/lng + floor_area_m2. The actual CSV (verified
2026-05-04) carries neither. Two pragmatic adaptations:

1. **No lat/lng → no spatial-fallback.** The CSV ships STREET_NUM +
   STREET_NAME + STREET_TYPE + STREET_DIRECTION pieces, no centroid. We
   reconstruct the address from those pieces and join via `_address.normalize_address`.
   The `PermitIndex.spatial_tree` still exists (empty STRtree) so callers
   can stay shape-compatible with `HeritageIndex`, but the orchestrator
   skips the spatial-fallback phase (every claim is `denominatorSource =
   "address_join"`). The `"spatial_fallback"` and `"mixed"` enum values
   become unreachable; the `validate()` enum check still accepts them
   so a future ETL upgrade can re-enable them without a wire-format break.

2. **No FLOOR_AREA → per-unit denominator instead of per-m².** The CSV ships
   DWELLING_UNITS_CREATED. The per-neighborhood metric becomes
   `medianCostPerUnit` (CAD per dwelling unit created), keeping the same
   sample-size guard. Per-unit is arguably more dev-relevant than per-m²
   anyway: developers ask "what does it cost to build a unit in this
   neighborhood?" not "what's the cost per square meter of bedroom + closet
   + mechanical room?" `meta.permits.denominatorLabel` is unchanged
   (`"declared_construction_cost_cad"` describes the value, not the
   denominator); a sibling key `denominatorPerUnit: true` documents the shift.

## Classifier

The closed-set `PERMIT_CATEGORY_TABLE` keys on the CSV's `PERMIT_TYPE` field
(verified values from the 2026-05-04 sample include "New Building",
"Small Residential Projects", etc.). Combined with `DWELLING_UNITS_CREATED > 0`
the loader keeps any permit that creates net-new dwelling units. That's the
"is this multiplex-relevant" gate.
"""

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import NamedTuple

from shapely.geometry import Point
from shapely.strtree import STRtree

from . import _http
from ._address import normalize_address

CACHE_FILENAME = "building_permits.csv"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/datastore/dump/"
    "6d0229af-bc54-46de-9c2b-26759b01dd05"
)
# Cleared building permits since 2017 — separate dataset from active.
# Same schema (incl. STRUCTURE_TYPE), used only by `build_structure_type_index`.
CLEARED_CACHE_FILENAME = "cleared_permits.csv"
CLEARED_RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/datastore/dump/"
    "a96c0ba4-3026-402b-b09d-5b1268b8f810"
)

DEFAULT_FRESHNESS_YEARS = 5
SANITY_VALUE_CEILING_CAD = 50_000_000
MAX_UNCLASSIFIED = 1000
MIN_NEIGHBORHOOD_SAMPLE_SIZE = 5  # was 10 — lowered 2026-05-11 alongside the
# ≥3-units-created filter on comp aggregation. The stricter unit filter
# drops the available sample by ~60% in many neighbourhoods; keeping the
# old "10 minimum" would null out half the city's pro-forma anchor.
# 5 is the floor where median is still defensible (vs noise from a small
# luxury outlier).

# Closed-set classifier on PERMIT_TYPE. Keys are uppercased exact-match.
# Comprehensive 2026-05-04 calibration against the full 230K-row CSV — every
# value with ≥1 occurrence is mapped. Unseen values still trigger a one-shot
# WARN + counter with `ClassifierDriftError` above MAX_UNCLASSIFIED so a
# future Toronto schema add surfaces loudly.
#
# The DWELLING_UNITS_CREATED > 0 gate further narrows the kept set: a kitchen
# reno typed as "BUILDING ADDITIONS/ALTERATIONS" with 0 new units is dropped.
PERMIT_CATEGORY_TABLE: dict[str, str] = {
    # ── KEEP (residential development creating dwelling units) ──
    "NEW HOUSES": "new_residential",
    "NEW BUILDING": "new_residential",
    "RESIDENTIAL BUILDING PERMIT": "addition_with_units",
    "SMALL RESIDENTIAL PROJECTS": "addition_with_units",
    "BUILDING ADDITIONS/ALTERATIONS": "addition_with_units",
    "PARTIAL PERMIT": "addition_with_units",
    "CONDITIONAL PERMIT": "addition_with_units",
    "AS ALTERNATIVE SOLUTION": "addition_with_units",
    "MULTIPLE USE PERMIT": "addition_with_units",

    # ── DROP (no new dwelling units) ──
    "PLUMBING(PS)": "renovation",
    "MECHANICAL(MS)": "renovation",
    "DRAIN AND SITE SERVICE": "renovation",
    "FIRE/SECURITY UPGRADE": "interior_alteration",
    "DEMOLITION FOLDER (DM)": "demolition_only",
    "RENTAL RENOVATION LICENCE": "renovation",
    "DESIGNATED STRUCTURES": "non_residential",
    "NON-RESIDENTIAL BUILDING PERMIT": "non_residential",
    "DCS DEFERREDFEES": "non_residential",
    "TEMPORARY STRUCTURES": "non_residential",
    "PORTABLE CLASSROOMS": "non_residential",
    "TORONTO BUILDINGS CONTACTS": "non_residential",
    "SITE INSPECTION(SCARBOROUGH)": "non_residential",
    "BUILDING HISTORICAL DATA - CONVERTED": "non_residential",
    "TORONTO BUILDING STANDARD ATTACHMENTS": "non_residential",
}

# Categories we KEEP. The DWELLING_UNITS_CREATED > 0 gate further narrows
# `addition_with_units` (a kitchen reno is "addition/alteration" with 0 new
# units; a basement-suite conversion is the same category but with units > 0).
KEPT_CATEGORIES: frozenset[str] = frozenset({
    "new_residential",
    "conversion",
    "addition_with_units",
})


class ClassifierDriftError(RuntimeError):
    """Raised when too many unclassified permit_type values appear in one run.

    Surfaces loudly so a future Toronto schema change doesn't silently drop
    a category that should have been kept. The fix is to update
    `PERMIT_CATEGORY_TABLE` with the new value.
    """


@dataclass(frozen=True)
class BuildingPermit:
    """One residential-relevant building permit, post-classification.

    `description` is intentionally classifier-only — never logged, never
    serialized to the wire, never reproduced in tests beyond synthetic
    strings. Toronto's permit description field can leak contractor or
    applicant names that the structured fields do not.
    """
    permit_id: str
    address: str         # raw, pre-normalize
    permit_type: str     # upstream PERMIT_TYPE
    description: str     # classifier input ONLY — do not surface
    declared_value_cad: int
    issued_date: date
    units_created: int   # DWELLING_UNITS_CREATED, ≥0
    units_existing: int  # DWELLING_UNITS_EXISTING, ≥0 — pre-construction
                         # unit count, ground truth for the parcel's existing-
                         # units count when this permit's address joins to a
                         # parcel. Added 2026-05-09 for the existingUnitsApprox
                         # feature (Item 4 ETL pipeline).
    category: str        # one of KEPT_CATEGORIES
    builder_name: str | None = None  # raw BUILDER_NAME from CSV (uppercase
                                     # company / individual), or None when
                                     # the permit has no builder recorded
                                     # (~30% of records). Surfaced on the
                                     # nearby-multiplex-permit comp wire to
                                     # tell devs who's active in this micro-
                                     # pocket. Added 2026-05-12.


class PermitIndex(NamedTuple):
    """Bundle consumed by `tools/build_parcels.py`.

    `spatial_tree` is included for shape-compatibility with `HeritageIndex`
    and a hypothetical future spatial-fallback phase. As of 2026-05-04 the
    Toronto CSV does not ship lat/lng, so the tree is empty and the
    orchestrator's spatial phase becomes a no-op (every claim is
    address-join).
    """
    permits: list[BuildingPermit]
    address_to_indices: dict[str, list[int]]
    spatial_tree: STRtree
    centroids: list  # always empty in 2026-05-04 build; reserved
    claimed: set[int]


_log = logging.getLogger(__name__)


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading Toronto Building Permits CSV → %s", cached)
    _http.download_with_retries(RESOURCE_URL, cached)
    return cached


def classify(permit_type: str) -> str | None:
    """Map an upstream PERMIT_TYPE to a coarse category, or None when unseen."""
    if permit_type is None:
        return None
    key = str(permit_type).strip().upper()
    return PERMIT_CATEGORY_TABLE.get(key)


def _build_address(row: dict) -> str:
    """Reconstruct the canonical address string from CSV pieces."""
    parts = [
        (row.get("STREET_NUM") or "").strip(),
        (row.get("STREET_NAME") or "").strip(),
        (row.get("STREET_TYPE") or "").strip(),
        (row.get("STREET_DIRECTION") or "").strip(),
    ]
    return " ".join(p for p in parts if p)


def _parse_int(text: str | None) -> int | None:
    if text is None or text == "":
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def compute_permits(
    cache_dir: Path,
    freshness_years: int = DEFAULT_FRESHNESS_YEARS,
    sanity_ceiling_cad: int = SANITY_VALUE_CEILING_CAD,
) -> PermitIndex:
    """Load + classify + filter Toronto building permits → `PermitIndex`.

    Streams the CSV via `csv.DictReader`, classifies each row, drops
    non-residential / out-of-window / outlier-value / unclassified rows,
    and returns the index for the orchestrator to join against parcels.

    Loud-failure: if more than MAX_UNCLASSIFIED rows match no entry in
    `PERMIT_CATEGORY_TABLE`, raises `ClassifierDriftError` — the table
    needs an update before the build can proceed.
    """
    path = _ensure_cached(Path(cache_dir))

    permits: list[BuildingPermit] = []
    address_to_indices: dict[str, list[int]] = {}

    skipped = {
        "missing_field": 0,
        "non_residential_construction": 0,
        "unclassified_type": 0,
        "outlier_value": 0,
        "bad_value": 0,
        "bad_date": 0,
        "no_units_created": 0,
    }
    unseen_types: set[str] = set()
    rows_seen = 0

    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            rows_seen += 1

            permit_type = row.get("PERMIT_TYPE") or ""
            permit_id = (row.get("PERMIT_NUM") or "").strip()
            issued_date = _parse_date(row.get("ISSUED_DATE"))
            value = _parse_int(row.get("EST_CONST_COST"))
            units = _parse_int(row.get("DWELLING_UNITS_CREATED")) or 0
            units_existing = _parse_int(row.get("DWELLING_UNITS_EXISTING")) or 0
            description = row.get("DESCRIPTION") or ""

            address = _build_address(row)
            if not permit_id or not address or not permit_type or issued_date is None:
                skipped["missing_field"] += 1
                continue

            category = classify(permit_type)
            if category is None:
                key = permit_type.strip().upper()
                if key not in unseen_types:
                    _log.warning(
                        "permits: unknown PERMIT_TYPE=%r; treating as unclassified", key
                    )
                    unseen_types.add(key)
                skipped["unclassified_type"] += 1
                if skipped["unclassified_type"] > MAX_UNCLASSIFIED:
                    raise ClassifierDriftError(
                        "upstream PERMIT_TYPE vocabulary may have shifted; review "
                        "tools/sources/building_permits.py:PERMIT_CATEGORY_TABLE"
                    )
                continue

            if category not in KEPT_CATEGORIES:
                skipped["non_residential_construction"] += 1
                continue

            # Multiplex-relevance gate: must create net-new dwelling units.
            # An "addition_with_units" permit-type that created 0 units is
            # a kitchen/bath reno, not a multiplex play.
            if units <= 0:
                skipped["no_units_created"] += 1
                continue

            if value is None or value <= 0:
                skipped["bad_value"] += 1
                continue
            if value > sanity_ceiling_cad:
                skipped["outlier_value"] += 1
                continue

            i = len(permits)
            builder_raw = (row.get("BUILDER_NAME") or "").strip() or None
            permits.append(BuildingPermit(
                permit_id=permit_id,
                address=address,
                permit_type=permit_type,
                description=description,
                declared_value_cad=value,
                issued_date=issued_date,
                units_created=units,
                units_existing=units_existing,
                category=category,
                builder_name=builder_raw,
            ))
            normalized = normalize_address(address)
            if normalized:
                address_to_indices.setdefault(normalized, []).append(i)

    _log.info(
        "permits: %d rows seen, %d kept (skipped: missing=%d non_res=%d "
        "unclass=%d outlier=%d bad_val=%d bad_date=%d no_units=%d)",
        rows_seen, len(permits),
        skipped["missing_field"], skipped["non_residential_construction"],
        skipped["unclassified_type"], skipped["outlier_value"],
        skipped["bad_value"], skipped["bad_date"], skipped["no_units_created"],
    )

    return PermitIndex(
        permits=permits,
        address_to_indices=address_to_indices,
        spatial_tree=STRtree([]),
        centroids=[],
        claimed=set(),
    )


# Map from CSV STRUCTURE_TYPE values → our 5-class enum
# (detached / semi / row / vacant / unknown). Apartment / townhouse /
# stacked variants all collapse to "row" because for L2/3 elite-gate
# purposes they're equivalent (party-wall structure, excluded from elite).
# Values not listed here are non-residential or ambiguous and don't
# override the cross-boundary classifier.
PERMIT_STRUCTURE_TYPE_TO_ENUM = {
    "SFD - Detached":        "detached",
    "2 Unit - Detached":     "detached",
    "3+ Unit - Detached":    "detached",
    "Converted House":       "detached",  # originally detached, now multi-unit; structure unchanged
    "SFD - Semi-Detached":   "semi",
    "2 Unit - Semi-detached": "semi",
    "SFD - Townhouse":       "row",
    "Stacked Townhouses":    "row",
    "Apartment Building":    "row",        # always attached/large
    "Multiple Unit Building": "row",
    # Excluded values (don't override classifier): "Other", "Unknown",
    # "Office", "Retail Store", "Industrial", "Mixed Use/Res w Non Res",
    # "Restaurant ...", "Hospital", etc., and "Laneway / Rear Yard Suite"
    # (an addition, not the main structure).
}


_NEW_DETACHED_GARAGE_RE = __import__("re").compile(
    r"\b(new|build|construct|erect)[a-z\s]*?\bdetached\s*garage\b"
)
_LANE_REF_RE = __import__("re").compile(
    r"\b(laneway|lane[\s-]?way|rear[\s-]?yard|rear[\s-]?lane)\b"
)


def compute_laneway_suite_address_set(cache_dir: Path) -> set[str]:
    """Return the set of normalized addresses that have at least one
    laneway-indicating permit on file. Used as a back-derived
    laneway-abutment signal — Toronto Centreline + OSM both have
    gaps on some Roncesvalles / Caledonia-Fairbank back-lanes, but
    any address in this set abuts a real lane by construction-permit
    ground truth.

    Two patterns matched:
      A. STRUCTURE_TYPE == "Laneway / Rear Yard Suite" (1303 addresses).
         Every laneway suite must abut a real lane per the 2018 by-law.
      B. PERMIT_TYPE description matches "new/build/construct DETACHED
         garage" AND the description references "laneway" / "rear yard"
         / "rear lane" (~242 net new addresses). A new detached garage
         explicitly tied to a lane reference is strong evidence the
         parcel abuts a lane — homeowner intentionally building
         lane-facing accessory infrastructure.
    """
    csv_path = _ensure_cached(Path(cache_dir))
    addrs: set[str] = set()
    n_suite = 0
    n_garage = 0
    with csv_path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            raw = _build_address(row)
            if not raw:
                continue
            norm = normalize_address(raw)
            if not norm:
                continue
            st = (row.get("STRUCTURE_TYPE") or "").strip()
            if st == "Laneway / Rear Yard Suite":
                if norm not in addrs:
                    n_suite += 1
                addrs.add(norm)
                continue
            # Pattern B: new detached garage + explicit lane reference
            desc = (row.get("DESCRIPTION") or "").lower()
            if _NEW_DETACHED_GARAGE_RE.search(desc) and _LANE_REF_RE.search(desc):
                if norm not in addrs:
                    n_garage += 1
                addrs.add(norm)
    _log.info(
        "laneway_suite_addresses: %d unique addresses (%d from Laneway/Rear-Yard-Suite STRUCTURE_TYPE, %d net-new from new-detached-garage-with-lane-ref descriptions)",
        len(addrs), n_suite, n_garage,
    )
    return addrs


def build_structure_type_index(cache_dir: Path) -> dict[str, str]:
    """Return `{normalized_address: enum_structure_type}` from THREE
    permit datasets, merged most-recent-wins:
    - active building permits (`building_permits.csv`)
    - cleared building permits since 2017 (`cleared_permits.csv`)
    - demolition permits (already cached as `demo_permits.json` for the
      signals layer)

    Each row's `STRUCTURE_TYPE` is mapped through
    `PERMIT_STRUCTURE_TYPE_TO_ENUM`. Most-recent ISSUED_DATE wins per
    address. Coverage on master cohort is ~120K unique addresses (~23 %
    of 528K parcels) and ~50–60 % of curated/broader picks (which are
    biased toward parcels with redev history).

    For matched parcels we have direct ground truth — no classifier
    heuristic needed. The cross-boundary classifier remains the fallback
    for unmatched parcels.
    """
    cache = Path(cache_dir)
    best_by_addr: dict[str, tuple[date, str, str]] = {}  # (date, enum, source)
    rows_with_struct = 0
    rows_mapped = 0
    unseen: set[str] = set()

    def ingest_row(row: dict, source: str) -> None:
        nonlocal rows_with_struct, rows_mapped
        st_raw = (row.get("STRUCTURE_TYPE") or "").strip()
        if not st_raw:
            return
        rows_with_struct += 1
        enum = PERMIT_STRUCTURE_TYPE_TO_ENUM.get(st_raw)
        if enum is None:
            unseen.add(st_raw)
            return
        addr = _build_address(row)
        if not addr:
            return
        normalized = normalize_address(addr)
        if not normalized:
            return
        issued = _parse_date(row.get("ISSUED_DATE")) or date(1900, 1, 1)
        existing = best_by_addr.get(normalized)
        if existing is None or issued > existing[0]:
            best_by_addr[normalized] = (issued, enum, source)
            rows_mapped += 1

    # 1. Active permits
    active = _ensure_cached(cache)
    with active.open("r", encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            ingest_row(row, "active")

    # 2. Cleared permits (since 2017) — separate dataset, similar schema.
    # Auto-download if not cached (~135 MB, takes ~20s). On a fresh
    # machine the first build_parcels run will pull this once.
    cleared = cache / CLEARED_CACHE_FILENAME
    if not cleared.exists() or cleared.stat().st_size == 0:
        _log.info("downloading cleared permits CSV (~135 MB) → %s", cleared)
        try:
            _http.download_with_retries(CLEARED_RESOURCE_URL, cleared)
        except Exception as e:
            _log.warning("cleared permits download failed (%s) — skipping that source", e)
    if cleared.exists() and cleared.stat().st_size > 0:
        with cleared.open("r", encoding="utf-8", newline="") as fp:
            for row in csv.DictReader(fp):
                ingest_row(row, "cleared")
    else:
        _log.warning("cleared_permits.csv not in cache — skipping cleared-permits ingest")

    # 3. Demolition permits — already cached as JSON for the signals
    # layer. Same schema as building permits.
    import json as _json
    demo = cache / "demo_permits.json"
    if demo.exists():
        try:
            with demo.open() as fp:
                d = _json.load(fp)
            recs = d.get("result", {}).get("records", []) if isinstance(d, dict) else d
            for row in (recs or []):
                ingest_row(row, "demo")
        except Exception as e:
            _log.warning("demo_permits.json read failed: %s — skipping", e)

    out = {addr: enum for addr, (_d, enum, _s) in best_by_addr.items()}
    _log.info(
        "permits/structure_type: %d rows had STRUCTURE_TYPE across active+cleared+demo, "
        "%d mapped to enum, %d unique addresses indexed (~23%% of citywide parcels, "
        "~50%%+ of redev-active cohort). %d unmapped STRUCTURE_TYPE values seen.",
        rows_with_struct, rows_mapped, len(out), len(unseen),
    )
    return out


def freshness_cutoff(today: date | None = None,
                     freshness_years: int = DEFAULT_FRESHNESS_YEARS) -> date:
    """Return the date floor for in-window aggregation."""
    today = today or date.today()
    # Crude "N years ago" — exact boundary is fine; a 1-day off-by-one at the
    # cutoff doesn't change the aggregate.
    return today - timedelta(days=freshness_years * 365)


def existing_units_from_permits(
    permit_indices: list[int],
    permits: list[BuildingPermit],
) -> int | None:
    """Return the most-recent permit's `units_existing` for a parcel,
    or None if no permits are joined.

    Used by `build_parcels.py` to derive `existingUnitsApprox` with
    basis='permits' (the highest-confidence source). The "most recent"
    rule favours fresher signals — a 2024 reno after a 2018 permit
    reflects today's reality, not what was on the lot 6 years ago.
    """
    if not permit_indices:
        return None
    relevant = [permits[i] for i in permit_indices]
    if not relevant:
        return None
    most_recent = max(relevant, key=lambda p: p.issued_date)
    return most_recent.units_existing


def aggregate_per_parcel(
    permit_indices: list[int],
    permits: list[BuildingPermit],
    cutoff: date,
    denominator_source: str,
) -> dict:
    """Compute the per-parcel `permits` dict from claimed permit indices."""
    in_window = [permits[i] for i in permit_indices if permits[i].issued_date >= cutoff]
    if not in_window:
        return {
            "recentCount": 0,
            "recentValueTotal": 0,
            "recentMostRecentDate": None,
            "denominatorSource": "no_joined_permits",
        }
    return {
        "recentCount": len(in_window),
        "recentValueTotal": sum(p.declared_value_cad for p in in_window),
        "recentMostRecentDate": max(p.issued_date for p in in_window).isoformat(),
        "denominatorSource": denominator_source,
    }


# ----------------------------------------------------------------------------
# Nearby-multiplex-permit comp (queued 2026-05-12 from 83 Twenty Seventh St
# case). For each parcel, find the nearest recent multiplex permits within
# 250m. Stronger underwriting evidence than the neighborhood-median comp
# we already surface — "4-plex built 80m away last year" is an on-block
# precedent, not a vibe-check.
# ----------------------------------------------------------------------------

NEARBY_COMP_MIN_UNITS = 3        # multiplex floor — same as aggregate_per_neighborhood
NEARBY_COMP_WINDOW_YEARS = 5     # post-Bill 185 environment + a couple years before
NEARBY_COMP_RADIUS_M = 250       # ≈ 3 typical Toronto blocks


class NearbyMultiplexIndex(NamedTuple):
    """Spatial index of recent multiplex permits in UTM (EPSG:26917).

    `tree.geometries[i]` aligns with `permits[i]` and `points_utm[i]`.
    Empty tree is allowed; consumers should check `len(permits) == 0`.
    """
    tree: STRtree
    points_utm: list[Point]
    permits: list[BuildingPermit]


def build_nearby_multiplex_index(
    permit_index: PermitIndex,
    ap_records_by_norm: dict[str, Point] | None,
    today: date | None = None,
    min_units: int = NEARBY_COMP_MIN_UNITS,
    window_years: int = NEARBY_COMP_WINDOW_YEARS,
) -> NearbyMultiplexIndex:
    """Build an STRtree of recent multiplex permits keyed by location.

    `ap_records_by_norm` is the `{normalized_address: Point(lon, lat)}`
    map derived from Toronto Address Points — supplies each permit's
    coordinate (the permits CSV ships no lat/lng). Permits whose address
    doesn't resolve to an Address Point are silently dropped.

    Filters: `units_created >= min_units` AND `issued_date >=
    today - window_years`. Returns an index in EPSG:26917 (Toronto UTM
    metres) so per-parcel queries can compute distance directly in m.
    """
    if ap_records_by_norm is None or not ap_records_by_norm:
        return NearbyMultiplexIndex(tree=STRtree([]), points_utm=[], permits=[])

    from pyproj import Transformer
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:26917", always_xy=True).transform

    cutoff = freshness_cutoff(today=today, freshness_years=window_years)
    points: list[Point] = []
    kept: list[BuildingPermit] = []
    matched = 0
    unmatched = 0
    for p in permit_index.permits:
        if p.units_created < min_units:
            continue
        if p.issued_date < cutoff:
            continue
        ap_pt = ap_records_by_norm.get(normalize_address(p.address))
        if ap_pt is None:
            unmatched += 1
            continue
        x, y = to_utm(ap_pt.x, ap_pt.y)
        points.append(Point(x, y))
        kept.append(p)
        matched += 1
    _log.info(
        "nearby_multiplex_index: %d multiplex permits in window, %d address-matched, %d unmatched",
        matched + unmatched, matched, unmatched,
    )
    return NearbyMultiplexIndex(
        tree=STRtree(points),
        points_utm=points,
        permits=kept,
    )


def query_nearby_multiplex(
    index: NearbyMultiplexIndex,
    parcel_centroid_lonlat: tuple[float, float] | None,
    parcel_own_permit_indices: set[int] | None = None,
    radius_m: float = NEARBY_COMP_RADIUS_M,
    builder_activity_counter: dict[str, int] | None = None,
) -> dict | None:
    """Return the per-parcel `nearbyMultiplexPermits` payload, or None when
    no permits qualify within the radius.

    `parcel_own_permit_indices` lets the caller drop the parcel's own
    permits so they don't count as "nearby comp" for themselves.

    `builder_activity_counter` is the citywide
    `{normalized_builder_name: count}` map (same one used by
    build_signals.py for demo permits) — used to tag the nearest
    permit's builder with an activity classification.

    Returns:
        {
            "count": int,
            "nearestDistM": int,
            "nearestDate": "YYYY-MM-DD",
            "nearestUnitsCreated": int,
            "nearestBuilderName": str | None,
            "nearestBuilderActivityCount": int | None,
            "nearestBuilderActivityLabel": str | None,
        }
        or None when no permits within radius.
    """
    if parcel_centroid_lonlat is None or len(index.permits) == 0:
        return None
    from pyproj import Transformer
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:26917", always_xy=True).transform
    lon, lat = parcel_centroid_lonlat
    cx, cy = to_utm(lon, lat)
    centroid_utm = Point(cx, cy)
    candidates = index.tree.query(centroid_utm.buffer(radius_m))
    own = parcel_own_permit_indices or set()
    nearest_dist = None
    nearest_perm = None
    count = 0
    for ci in candidates:
        if ci in own:
            continue
        pt = index.points_utm[ci]
        d = centroid_utm.distance(pt)
        if d > radius_m:
            continue
        count += 1
        if nearest_dist is None or d < nearest_dist:
            nearest_dist = d
            nearest_perm = index.permits[ci]
    if count == 0 or nearest_perm is None:
        return None
    # Builder classification for the nearest permit (same algorithm
    # used by build_signals.py for demo permits). Done client-side
    # here so build_parcels.py doesn't need to import build_signals.
    builder_name = nearest_perm.builder_name
    activity_count = None
    activity_label = None
    if builder_name and builder_activity_counter:
        norm = _normalize_builder_for_activity(builder_name)
        if norm:
            activity_count = builder_activity_counter.get(norm)
            if activity_count:
                if activity_count == 1:
                    activity_label = "first permit on file"
                elif activity_count <= 3:
                    activity_label = "occasional builder"
                else:
                    activity_label = "active operator"
    return {
        "count": count,
        "nearestDistM": int(round(nearest_dist)),
        "nearestDate": nearest_perm.issued_date.isoformat(),
        "nearestUnitsCreated": int(nearest_perm.units_created),
        "nearestBuilderName": builder_name,
        "nearestBuilderActivityCount": activity_count,
        "nearestBuilderActivityLabel": activity_label,
    }


# Mirror of build_signals.py:_normalize_builder_name. Kept here to avoid
# the source module importing the entry-point script.
import re as _re_builder
_BUILDER_TRAILING_RE_B = _re_builder.compile(
    r"\s+(INC|LTD|LIMITED|CORP|CORPORATION|CO|COMPANY|LLP)\.?\s*$",
    flags=_re_builder.IGNORECASE,
)


def _normalize_builder_for_activity(name: str | None) -> str | None:
    if not name:
        return None
    s = name.strip().upper()
    for _ in range(3):
        new = _BUILDER_TRAILING_RE_B.sub("", s).strip().rstrip(".").strip()
        if new == s:
            break
        s = new
    return s or None


def build_builder_activity_counter_from_permits(permits) -> dict[str, int]:
    """Citywide `{normalized_builder: count}` from all kept building
    permits. Mirrors build_signals.py's demo-permit counter so the
    nearby-multiplex-permit comp can tag the nearest permit's builder.
    """
    from collections import Counter
    counter: Counter = Counter()
    for p in permits:
        n = _normalize_builder_for_activity(p.builder_name)
        if n:
            counter[n] += 1
    return dict(counter)


def compute_builder_counter_all_permits(
    cache_dir: Path,
    *,
    window_years: int = 5,
) -> dict[str, int]:
    """Raw-CSV pass to count every permit (any STRUCTURE_TYPE, any
    PERMIT_TYPE, any unit count) per normalized BUILDER_NAME within the
    window. Used by build_signals.py to attribute builder activity
    across BOTH demolition and building permits — a custom-home builder
    with no demos still gets credit for their building permits.

    Counts each individual permit row (one builder might appear on a
    plumbing permit + a mechanical permit + a structural permit for the
    same project — all 3 count). The threshold buckets are tuned to
    this: 1 = first project, 2-3 = occasional, 4+ = active operator.
    Inflating counts via multi-trade rollup is roughly correct here
    because a builder filing 4 trade permits IS more committed than a
    one-off owner-builder who files only the main permit.
    """
    import csv as _csv
    from collections import Counter as _Counter
    from datetime import date as _date, timedelta as _td
    cutoff = _date.today() - _td(days=window_years * 365)
    cutoff_iso = cutoff.isoformat()
    csv_path = _ensure_cached(Path(cache_dir))
    counter: _Counter = _Counter()
    rows = 0
    with csv_path.open("r", encoding="utf-8", newline="") as fp:
        reader = _csv.DictReader(fp)
        for row in reader:
            iss = (row.get("ISSUED_DATE") or "")[:10]
            applied = (row.get("APPLICATION_DATE") or "")[:10]
            latest = max(iss, applied)
            if not latest or latest < cutoff_iso:
                continue
            builder = (row.get("BUILDER_NAME") or "").strip()
            norm = _normalize_builder_for_activity(builder)
            if not norm:
                continue
            counter[norm] += 1
            rows += 1
    _log.info(
        "builder_counter_all_permits: %d permits with builder names in last %dy → %d unique builders",
        rows, window_years, len(counter),
    )
    return dict(counter)


def aggregate_per_neighborhood(
    claims_by_neighborhood: dict[str, list[int]],
    permits: list[BuildingPermit],
    cutoff: date,
    freshness_years: int = DEFAULT_FRESHNESS_YEARS,
    min_sample_size: int = MIN_NEIGHBORHOOD_SAMPLE_SIZE,
) -> dict[str, dict]:
    """Compute `{neighborhood_name: {medianCostPerUnit, sampleSize, freshnessYears}}`.

    2026-05-11 — filter comps to MULTIPLEX-COMPARABLE permits only (units
    created ≥ 3). Previously the comp set included every residential permit
    adding ≥1 unit, which in wealthy neighbourhoods (Bayview Village, Lawrence
    Park) was dominated by $3-5M single-family custom rebuilds. Those inflated
    the per-unit median to ~$900K — wildly above what a 4-6 unit multiplex
    would actually cost. The multiplex-floor cutoff (3 units) drops single-
    family rebuilds and conversions to <3 units while keeping triplex,
    fourplex, sixplex, and small-apartment comps. Sample sizes will shrink
    in wealthy areas where multiplex activity is sparse — that's correct;
    "we don't have a credible comp" is better than "luxury single-family
    median masquerading as multiplex math."
    """
    out: dict[str, dict] = {}
    for nb_name, indices in claims_by_neighborhood.items():
        in_window = [permits[i] for i in indices if permits[i].issued_date >= cutoff]
        ratios = [
            p.declared_value_cad / p.units_created
            for p in in_window if p.units_created >= 3
        ]
        if len(ratios) < min_sample_size:
            out[nb_name] = {
                "medianCostPerUnit": None,
                "sampleSize": len(ratios),
                "freshnessYears": freshness_years,
            }
        else:
            out[nb_name] = {
                "medianCostPerUnit": int(round(median(ratios))),
                "sampleSize": len(ratios),
                "freshnessYears": freshness_years,
            }
    return out
