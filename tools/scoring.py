"""Composite Net-Zero Score formula for BloomTO neighborhoods (v1.1).

Six real inputs, weighted to sum to 1.0:
  - heatPump (SolarTO)        — 0.30
  - canopy (Forest/Land Cover) — 0.20, scaled ×2 since urban canopy maxes ~50%
  - walk (Centreline density)  — 0.15
  - transit (TTC stops)        — 0.15
  - bike (Cycling Network)     — 0.10
  - Missing-Middle headroom    — 0.10, derived from (potential/existing − 1) × 50
"""

ENERGY_WEIGHT = 0.30
CANOPY_WEIGHT = 0.20
WALK_WEIGHT = 0.15
TRANSIT_WEIGHT = 0.15
BIKE_WEIGHT = 0.10
MM_WEIGHT = 0.10

CANOPY_SCALE = 2
MM_SCALE = 50

DEFAULT_WEIGHTS = {
    "energy": ENERGY_WEIGHT,
    "canopy": CANOPY_WEIGHT,
    "walk": WALK_WEIGHT,
    "transit": TRANSIT_WEIGHT,
    "bike": BIKE_WEIGHT,
    "mm": MM_WEIGHT,
}

FORMULA_TEXT = (
    "score = round("
    "0.30 × heatPump "
    "+ 0.20 × min(100, canopy × 2) "
    "+ 0.15 × walk "
    "+ 0.15 × transit "
    "+ 0.10 × bike "
    "+ 0.10 × min(100, max(0, (potential / max(existing, 1) - 1) × 50)))"
)


def _missing_middle_headroom(existing: int, potential: int) -> float:
    ratio = potential / max(existing, 1)
    return min(100, max(0, (ratio - 1) * MM_SCALE))


def score(
    *,
    heat_pump: int,
    canopy_pct: int,
    walk: int,
    transit: int,
    bike: int,
    existing: int,
    potential: int,
    weights: dict | None = None,
) -> int:
    w = DEFAULT_WEIGHTS if weights is None else {**DEFAULT_WEIGHTS, **weights}

    canopy_scaled = min(100, canopy_pct * CANOPY_SCALE)
    mm_headroom = _missing_middle_headroom(existing, potential)

    raw = (
        w["energy"] * heat_pump
        + w["canopy"] * canopy_scaled
        + w["walk"] * walk
        + w["transit"] * transit
        + w["bike"] * bike
        + w["mm"] * mm_headroom
    )
    return max(0, min(100, round(raw)))
