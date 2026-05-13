"""Per-parcel shadow analysis (three-tier algorithm).

For each parcel, query the 3D Massing STRtree for nearby buildings, classify
each candidate into one of three tiers based on data availability, and
return a `ShadowResult(unshadowed_fraction, quality)`. Quality follows
design.md §147 / §354's accuracy-over-completeness rule:

  - "measured":    all candidates have valid footprint + height
  - "estimated":   at least one candidate falls back to the conservative envelope
  - "unavailable": no candidate has a usable height — orchestrator sets
                   `solarScore = None`, never guesses

The projection is **planar** at Toronto's latitude (43.7°N) — the design's
"flat-earth is fine" approximation, which avoids reaching for `pyproj` and
works because parcels are O(50 m) wide and the per-degree distance error
across that span is sub-metre. Sun direction at azimuth `a` (degrees, North = 0
clockwise) means the sun is *at* that bearing — shadows fall in the *opposite*
direction. For Toronto noon, the sun is south (azimuth 180°), so shadows
extend north.

The reference angles are the winter and summer solstice noon. Averaging
gives a year-round shadow proxy without paying for an annual ray trace.
"""

import math
from collections import namedtuple
from typing import Iterable

import logging

import shapely
from shapely.affinity import translate
from shapely.errors import GEOSException
from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.strtree import STRtree

_log = logging.getLogger(__name__)


def _clean(geom):
    """Return a topology-clean copy of `geom`, or `None` if not salvageable.

    Defends against libgeos segfaults in `unary_union` on inputs with
    self-intersections, near-collinear vertices, or precision artifacts that
    pass shapely's `is_valid` check but blow up inside GEOS C code.

    Strategy: `make_valid` first (preferred — preserves topology); fall back
    to `buffer(0)` (the classic GEOS "snap-clean" idiom). Returns None for
    geometries that come back empty/invalid even after both passes — those
    are dropped from the union rather than risking a crash.
    """
    if geom is None or geom.is_empty:
        return None
    try:
        cleaned = shapely.make_valid(geom)
    except GEOSException:
        try:
            cleaned = geom.buffer(0)
        except GEOSException:
            return None
    if cleaned is None or cleaned.is_empty:
        return None
    return cleaned


def _safe_unary_union(geoms):
    """`unary_union` with topology-clean inputs and a per-pair fallback.

    The per-pair fallback uses sequential `.union()`, which is sometimes
    more stable than bulk `unary_union` for adversarial input combinations.
    Cannot recover from a hard segfault (Python can't catch those), but the
    cleaning step prevents the conditions that trigger one.
    """
    cleaned = [g for g in (_clean(g) for g in geoms) if g is not None]
    if not cleaned:
        return None
    try:
        return unary_union(cleaned)
    except GEOSException as e:
        _log.warning("unary_union failed (%s); falling back to sequential union", e)
        acc = cleaned[0]
        for g in cleaned[1:]:
            try:
                acc = acc.union(g)
            except GEOSException:
                continue
        return acc

from .sources.massing import Building
from .sources.zoning import Parcel

ShadowResult = namedtuple("ShadowResult", ["unshadowed_fraction", "quality"])

DEFAULT_SEARCH_RADIUS_M = 75
MAX_CANDIDATES_PER_PARCEL = 100
SHADOW_PROJECTION_METHOD = "planar"

# (azimuth_deg, elevation_deg) — winter solstice noon, summer solstice noon, at
# Toronto's latitude (~43.7°N). Azimuth 180° (south) is the sun's local-noon
# bearing; elevation 22° (Dec 21) and 70° (Jun 21) bracket the year.
REFERENCE_ANGLES = ((180, 22), (180, 70))

# Metres per degree at Toronto's latitude — used as the planar approximation
# for sub-100m projections. Latitude is nearly constant across the city.
_TORONTO_LAT_DEG = 43.7
_M_PER_DEG_LAT = 111_000.0
_M_PER_DEG_LON = _M_PER_DEG_LAT * math.cos(math.radians(_TORONTO_LAT_DEG))


def _project_shadow_polygon(building, azimuth_deg: float, elevation_deg: float):
    """Return the shadow polygon for one tier-1 candidate at one sun angle.

    Returns `None` if the projection isn't physically meaningful (sun at or
    below horizon, missing height). The shadow is the union of the building's
    own footprint and a copy translated by the shadow length in the sun's
    opposite-direction. This conservatively encloses the swept envelope —
    correct for convex footprints and over-estimates only by the side strips
    for non-convex ones, which is the safe direction.
    """
    if building.height_m is None or building.height_m <= 0:
        return None
    elev_rad = math.radians(elevation_deg)
    if elev_rad <= 0:
        return None
    shadow_len_m = building.height_m / math.tan(elev_rad)

    azim_rad = math.radians(azimuth_deg)
    # Sun direction vector: (sin(azim), cos(azim)) with north=+y.
    # Shadow falls in the opposite direction.
    dx_m = -shadow_len_m * math.sin(azim_rad)
    dy_m = -shadow_len_m * math.cos(azim_rad)

    dx_deg = dx_m / _M_PER_DEG_LON
    dy_deg = dy_m / _M_PER_DEG_LAT

    translated = translate(building.geometry, xoff=dx_deg, yoff=dy_deg)
    return _safe_unary_union([building.geometry, translated])


def _envelope_disc(building, elevation_deg: float):
    """Tier-2 conservative shadow envelope: circle of radius `h/tan(elev)`
    centered at the building's footprint centroid.

    Over-estimates shadow extent (the actual shadow is a directional strip,
    not a circle), which biases the resulting `solarScore` downward — the
    safe direction per design.md §354.
    """
    if building.height_m is None or building.height_m <= 0:
        return None
    elev_rad = math.radians(elevation_deg)
    if elev_rad <= 0:
        return None
    radius_m = building.height_m / math.tan(elev_rad)
    radius_deg = radius_m / _M_PER_DEG_LAT  # use latitude scale (smaller = larger envelope)
    return building.geometry.centroid.buffer(radius_deg)


def _select_candidates(
    parcel: Parcel,
    massing_index: tuple[STRtree, list[Building]],
    search_radius_m: float,
) -> list[Building]:
    """STRtree query → cap → return Building objects."""
    tree, buildings = massing_index

    parcel_lat = parcel.centroid[1]
    buffer_deg = search_radius_m / max(
        _M_PER_DEG_LAT,
        _M_PER_DEG_LAT * math.cos(math.radians(parcel_lat)),
    )
    # Use latitude-axis distance (the larger of the two — smaller cos shrinks
    # longitude metres-per-degree), for a conservative bbox query.
    buffered = parcel.geometry.buffer(buffer_deg)
    idxs = list(tree.query(buffered))

    if len(idxs) > MAX_CANDIDATES_PER_PARCEL:
        parcel_centroid = Point(parcel.centroid)
        ranked = sorted(
            idxs,
            key=lambda i: parcel_centroid.distance(buildings[i].geometry.centroid),
        )
        idxs = ranked[:MAX_CANDIDATES_PER_PARCEL]

    return [buildings[i] for i in idxs]


def _classify(building: Building) -> str:
    """Return 'tier1' | 'tier2' | 'tier3' for one candidate."""
    if building.height_m is None:
        return "tier3"
    geom = building.geometry
    if geom is None or geom.is_empty or geom.area <= 0:
        return "tier2"
    return "tier1"


def analyze_parcel(
    parcel: Parcel,
    massing_index: tuple[STRtree, list[Building]],
    *,
    search_radius_m: float = DEFAULT_SEARCH_RADIUS_M,
    sun_angles: Iterable[tuple[float, float]] = REFERENCE_ANGLES,
) -> ShadowResult:
    """Compute `(unshadowed_fraction, quality)` for one parcel.

    See module docstring for the algorithm and the three-tier accuracy contract.
    Returns `(1.0, "measured")` when no buildings fall within `search_radius_m`
    — no neighbours, no shadow, no adjustment needed (and that *is* the
    measured truth, not an estimate).
    """
    candidates = _select_candidates(parcel, massing_index, search_radius_m)

    if not candidates:
        return ShadowResult(1.0, "measured")

    tier1: list[Building] = []
    tier2: list[Building] = []
    tier3_count = 0
    for b in candidates:
        cls = _classify(b)
        if cls == "tier1":
            tier1.append(b)
        elif cls == "tier2":
            tier2.append(b)
        else:
            tier3_count += 1

    if not tier1 and not tier2:
        # Every candidate is tier3 — no usable height anywhere nearby.
        return ShadowResult(None, "unavailable")

    parcel_geom = parcel.geometry
    parcel_area = parcel_geom.area  # deg² — fine for ratios
    if parcel_area <= 0:
        return ShadowResult(1.0, "measured")

    sun_angles_list = list(sun_angles)
    fractions: list[float] = []
    for azim, elev in sun_angles_list:
        shadows = []
        for b in tier1:
            shadow = _project_shadow_polygon(b, azim, elev)
            if shadow is not None and not shadow.is_empty:
                shadows.append(shadow)
        for b in tier2:
            disc = _envelope_disc(b, elev)
            if disc is not None and not disc.is_empty:
                shadows.append(disc)

        if not shadows:
            fractions.append(1.0)
            continue

        unioned = _safe_unary_union(shadows)
        if unioned is None or unioned.is_empty:
            fractions.append(1.0)
            continue
        try:
            intersection = parcel_geom.intersection(unioned)
        except GEOSException:
            # Same defensive bailout: if the intersection itself fails,
            # treat the parcel as un-shadowed for this sun angle rather than
            # crashing the whole ETL.
            fractions.append(1.0)
            continue
        shadow_frac = intersection.area / parcel_area if not intersection.is_empty else 0.0
        unshadowed = max(0.0, min(1.0, 1.0 - shadow_frac))
        fractions.append(unshadowed)

    avg_unshadowed = sum(fractions) / len(fractions) if fractions else 1.0
    quality = "estimated" if tier2 else "measured"
    return ShadowResult(avg_unshadowed, quality)
