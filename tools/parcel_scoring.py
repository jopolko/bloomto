"""Per-parcel Multiplex Readiness score + the precomputed `bloom` boolean.

The base score is multiplicative (eligibility × transit-factor × unit-cap-factor),
*not* the additive weighted-sum used by the v1.1 neighborhood score. The
multiplicative shape encodes the as-of-right gate: if a parcel fails any
eligibility check (non-residential, heritage-listed, no major-transit stop
within 500 m), the score is unconditionally 0 — there is no "partial credit."

The four category filters surfaced on the wire (cornerLot, deepLot via
lotAspectRatio, postwarNeighborhood, bloom) deliberately do *not* enter
this formula. They live as parcel properties so the frontend can filter the
Top-N list without changing rank order.

Stdlib only. Mirrors the constant + FORMULA_TEXT idiom from `tools/scoring.py`.
"""

# As-of-right transit buffer for the score's transit_factor (the parking-waiver
# threshold tied to "major transit station area" under Toronto's by-laws).
TRANSIT_BUFFER_M = 500

# Soft transit range: parcels in the 500-1500m band are still as-of-right
# multiplex candidates per the 2019 / 2022 by-laws (the multiplex law applies
# city-wide), they just lose the parking-waiver perk that the 500m buffer
# enables. Used by `soft_score()` so suburban-multiplex parcels can still
# rank meaningfully (vs. the strict score's hard cliff at 500m → 0).
SOFT_TRANSIT_RANGE_M = 1500

# Lower edge of the multiplex-eligible zone scale. Below 4 units per lot,
# the parcel falls outside the Multiplex By-law's as-of-right envelope; at or
# above 4 the multiplier_factor begins ramping toward 1.0.
MULTIPLEX_FLOOR = 4

# Minimum buildable lot area, in m². Below this we treat the polygon as a
# sliver — common-element strip, laneway segment, road-widening leftover, or
# easement — that the Property Boundaries dataset surfaces alongside real
# residential parcels. The 100 m² cutoff is calibrated against the v1.2
# parcel set: ~85% of <50 m² polygons and ~26% of 50–99 m² polygons are
# unaddressed in the upstream data, vs ≤8% of 100+ m² polygons. Toronto's
# narrowest legitimate residential lots are ~130–180 m², so 100 is well
# clear of any real housing parcel.
MIN_BUILDABLE_AREA_M2 = 100

# Bloom gate thresholds — premium multiplex-friction-clear tier.
# Reframed 2026-05-06 from "net-zero premium" (solar + subway) to
# "premium multiplex" (frictionless multiplex feasibility). A bloom-true
# parcel must clear ALL of:
#   - heritage clear (no Part IV / V / Listed)
#   - within 500m of major transit (subway∪streetcar combined)
#   - lot >= 600 m² (room for a real sixplex w/o variance)
#   - sixplex-eligible district (T&EY + Ward 23, June 2025 carve-out)
#   - 0 mature ≥30cm DBH protected trees (no Section 7 friction)
#   - not TRCA-regulated
# Solar moved to a separate `solarPrime` badge in the frontend.
BLOOM_TRANSIT_M = 500
BLOOM_MIN_LOT_M2 = 600
BLOOM_MAX_TREES = 0

# Per-tier heritage discount factors. Part IV (individually designated) is a hard
# block at 0.0 — under by-law 569-2013 demolition is prohibited without an OMB
# hearing. Part V (Heritage Conservation District) is friction not blocker —
# multiplex conversion of contributing buildings is usually approvable but
# design review applies. Listed (watchlist, not legally designated) is the
# mildest discount — demolition allowed after a 60-day notice. These two
# named constants are the single tuning surface for factor adjustments
# (re-run `tools/validate_heritage_tiers.py` after changing them).
PART_V_HERITAGE_FACTOR = 0.5
LISTED_HERITAGE_FACTOR = 0.85

# Private lookup table consumed by `score()`. Unknown keys raise KeyError
# (loud-failure pattern; the score caller should never see an unknown status —
# the wire format validator and the heritage source loader both gate that).
_HERITAGE_FACTORS: dict[str | None, float] = {
    None: 1.0,
    "part_iv": 0.0,
    "part_v": PART_V_HERITAGE_FACTOR,
    "listed": LISTED_HERITAGE_FACTOR,
}

FORMULA_TEXT = (
    "score = round("
    "100 × residential × heritage_factor × transit_factor × multiplier_factor"
    "), where residential ∈ {0, 1}, "
    "heritage_factor ∈ {1.0 (no heritage), 0.5 (Part V), 0.85 (Listed), 0.0 (Part IV)}, "
    "transit_factor = max(0, 1 − dist_m / 500), "
    "multiplier_factor = min(1.0, max_units_per_lot / 4); "
    "score is forced to 0 when lot_area_m2 < 100 (sliver gate excluding "
    "common-element strips, laneway segments, and road-widening leftovers)"
)

BLOOM_FORMULA_TEXT = (
    "bloom = (heritageStatus is null) AND "
    "(distSubwayStreetcarM < 500) AND "
    "(lotAreaM2 >= 600) AND "
    "(sixplexEligible == True) AND "
    "(matureTreeCount == 0) AND "
    "(inRegulatedArea == False). "
    "Premium multiplex-friction-clear tier — heritage-clear, walks to "
    "major transit (matches Bill 185 parking-waiver buffer), room for a "
    "real sixplex, in a sixplex-eligible district (T&EY + Ward 23), no "
    "Section 7 Tree Bylaw friction, no TRCA permit gate."
)

# SolarTO upstream rooftop screening — what passes into BloomTO's solarScore.
# Surfaced separately from BLOOM_FORMULA_TEXT (which is locked by Req 2.7) so
# the methodology disclosure can travel on the wire (`meta.solarMethodology`)
# and be quoted verbatim by the UI without re-introducing it via copy drift.
SOLAR_METHODOLOGY_TEXT = (
    "solarScore inherits SolarTO's per-rooftop screening: a roof surface must "
    "receive >=800 kWh/m^2/yr incident solar radiation, have >=30 m^2 of clear "
    "space, slope <45 degrees, and not face north. Toronto yield factor: 1 kW "
    "installed PV generates ~1,150 kWh/yr. BloomTO's solarScore = SolarTO max "
    "rooftop kWh (P95-normalized to 0-100) shadow-adjusted by 3D Massing "
    "neighbor-building modeling."
)


def score(
    *,
    residential: bool,
    heritage_status: str | None,
    dist_m: float | None,
    max_units: int,
    area_m2: float | None = None,
) -> int:
    """Base Multiplex Readiness score, 0–100.

    Returns 0 unless **all** eligibility checks pass:
      - parcel is residential (zone multiplier > 0),
      - not Part IV heritage-designated (`heritage_factor > 0`),
      - within `TRANSIT_BUFFER_M` (500 m) of a major-transit stop,
      - lot area ≥ `MIN_BUILDABLE_AREA_M2` when `area_m2` is supplied (sliver
        gate; passing `area_m2=None` skips the check, preserving the original
        4-arg signature for legacy callers).

    On eligibility, the score scales with transit proximity, the zone's per-lot
    unit cap (4-plex → 1.0, smaller caps clamp under), and the per-tier heritage
    factor (Part IV → 0 hard block; Part V → 0.5 friction discount; Listed →
    0.85 mild discount; null → 1.0 no discount).

    `heritage_status` must be one of `None`, `"part_iv"`, `"part_v"`, `"listed"`.
    Any other value raises `KeyError` (loud-failure pattern; the wire format
    validator should never let an unknown status reach this function).
    """
    if not residential:
        return 0
    heritage_factor = _HERITAGE_FACTORS[heritage_status]
    if heritage_factor == 0:
        return 0
    if dist_m is None or dist_m >= TRANSIT_BUFFER_M:
        return 0
    if area_m2 is not None and area_m2 < MIN_BUILDABLE_AREA_M2:
        return 0

    transit_factor = max(0.0, 1.0 - dist_m / TRANSIT_BUFFER_M)
    multiplier_factor = min(1.0, max_units / MULTIPLEX_FLOOR)
    raw = 100 * heritage_factor * transit_factor * multiplier_factor
    return max(0, min(100, round(raw)))


def soft_score(
    *,
    residential: bool,
    heritage_status: str | None,
    dist_m: float | None,
    max_units: int,
    area_m2: float | None = None,
) -> int:
    """Suburban-multiplex variant of `score()` — extends the transit decay
    to 1500m so parcels outside the 500m parking-waiver buffer still rank.

    All non-transit gates are identical to `score()` (residential, heritage,
    sliver). Only the transit factor changes:
      0–1500m: factor = max(0, 1 − dist_m / 1500)   (linear citywide decay)
      ≥1500m:  factor = 0

    Used by `compute_full_score()` to populate the `softScore` wire field
    alongside the strict `score`. The frontend's "🚗 Include >500m from
    transit" chip toggles between which score to display/sort by.
    """
    if not residential:
        return 0
    heritage_factor = _HERITAGE_FACTORS[heritage_status]
    if heritage_factor == 0:
        return 0
    if dist_m is None or dist_m >= SOFT_TRANSIT_RANGE_M:
        return 0
    if area_m2 is not None and area_m2 < MIN_BUILDABLE_AREA_M2:
        return 0
    soft_transit = max(0.0, 1.0 - dist_m / SOFT_TRANSIT_RANGE_M)
    multiplier_factor = min(1.0, max_units / MULTIPLEX_FLOOR)
    raw = 100 * heritage_factor * soft_transit * multiplier_factor
    return max(0, min(100, round(raw)))


def compute_full_score(
    *,
    residential: bool,
    heritage_status: str | None,
    dist_m: float | None,
    max_units: int,
    area_m2: float | None = None,
) -> dict:
    """Compute both strict and soft scores plus the outside-transit-buffer
    flag in one call. Returns:
      {
        "score":                   <strict score, 0-100, 0 if dist_m >= 500m>,
        "softScore":               <soft score, 0-100, 0 if dist_m >= 1500m>,
        "outsideTransitBuffer":    <bool, True iff dist_m >= 500m>,
      }
    """
    strict = score(
        residential=residential, heritage_status=heritage_status,
        dist_m=dist_m, max_units=max_units, area_m2=area_m2,
    )
    soft = soft_score(
        residential=residential, heritage_status=heritage_status,
        dist_m=dist_m, max_units=max_units, area_m2=area_m2,
    )
    outside = (dist_m is None) or (dist_m >= TRANSIT_BUFFER_M)
    return {
        "score": strict,
        "softScore": soft,
        "outsideTransitBuffer": outside,
    }


def bloom_flag(
    *,
    heritage_status: str | None,
    dist_subway_streetcar_m: float | None,
    lot_area_m2: float | None,
    sixplex_eligible: bool,
    mature_tree_count: int,
    in_regulated_area: bool,
) -> bool:
    """Bloom flag — the precomputed "premium multiplex" boolean.

    Reframed 2026-05-06 from "net-zero premium" (solar + subway only) to
    "premium multiplex-friction-clear": every input is a city-data signal
    that directly affects multiplex feasibility cost or timeline. Solar
    moved to a separate `solarPrime` badge in the frontend.

    All six gates must pass:
      - heritage_status is None (no Part IV / V / Listed encumbrance)
      - dist_subway_streetcar_m < BLOOM_TRANSIT_M (500m, matches the
        TRANSIT_BUFFER_M parking-waiver buffer / Bill 185)
      - lot_area_m2 >= BLOOM_MIN_LOT_M2 (600m², room for a real sixplex
        without variance)
      - sixplex_eligible is True (T&EY District + Ward 23, June 2025)
      - mature_tree_count <= BLOOM_MAX_TREES (no Section 7 Tree Bylaw
        friction — protected trees add $5K-$30K + permit delay each)
      - not in_regulated_area (TRCA permit gate doesn't apply)

    Returns False when any gate's input is None (accuracy-over-completeness).
    """
    if heritage_status is not None:
        return False
    if dist_subway_streetcar_m is None or dist_subway_streetcar_m >= BLOOM_TRANSIT_M:
        return False
    if lot_area_m2 is None or lot_area_m2 < BLOOM_MIN_LOT_M2:
        return False
    if not sixplex_eligible:
        return False
    if mature_tree_count > BLOOM_MAX_TREES:
        return False
    if in_regulated_area:
        return False
    return True
