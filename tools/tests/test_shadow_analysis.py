"""Tests for `tools/shadow_analysis.py` (Task 21).

All fixtures are built inline — no real 3D Massing data needed. Coordinates
are placed at Toronto's latitude (~43.7°N) so the planar approximation
constants (`_M_PER_DEG_LAT/LON`) reflect realistic shadow lengths.

At lat 43.7°N: 1° lat = 111,000 m, 1° lon ≈ 80,250 m. So 1 m ≈ 1.25e-5° lon
and 9.0e-6° lat. Shadow length at elev 22° (winter) for a 30 m building:
30 / tan(22°) ≈ 74 m ≈ 6.7e-4° lat. Plenty to reach a 50 m parcel placed
~0.0005° (~55 m) north.
"""

import math
import unittest

from shapely.geometry import Polygon
from shapely.strtree import STRtree

from tools.shadow_analysis import (
    DEFAULT_SEARCH_RADIUS_M,
    MAX_CANDIDATES_PER_PARCEL,
    REFERENCE_ANGLES,
    ShadowResult,
    analyze_parcel,
)
from tools.sources.massing import Building
from tools.sources.zoning import Parcel


def _make_parcel(parcel_id: str, polygon: Polygon) -> Parcel:
    c = polygon.centroid
    return Parcel(
        parcel_id=parcel_id,
        address=None,
        geometry=polygon,
        centroid=(c.x, c.y),
        area_m2=1.0,  # not used by analyze_parcel
    )


def _square_polygon(min_lon: float, min_lat: float, side_deg: float) -> Polygon:
    return Polygon([
        (min_lon, min_lat),
        (min_lon + side_deg, min_lat),
        (min_lon + side_deg, min_lat + side_deg),
        (min_lon, min_lat + side_deg),
        (min_lon, min_lat),
    ])


def _massing_index(buildings: list[Building]) -> tuple[STRtree, list[Building]]:
    return STRtree([b.geometry for b in buildings]), buildings


class ShadowAnalysisTests(unittest.TestCase):
    def test_zero_candidates_returns_one_measured(self):
        parcel = _make_parcel("P", _square_polygon(-79.400, 43.700, 0.0005))
        # Massing index has buildings, but they're 1km away — outside the 75m search radius.
        far_building = Building(
            geometry=_square_polygon(-79.380, 43.700, 0.0001),
            height_m=30.0,
        )
        result = analyze_parcel(parcel, _massing_index([far_building]))
        self.assertEqual(result, ShadowResult(1.0, "measured"))

    def test_southern_tier1_building_casts_shadow_into_parcel(self):
        # Parcel at lat 43.7000–43.7005 (north). Building 50 m south at lat 43.6994–43.6997.
        # Tall (30 m) at winter solstice elevation 22° → shadow ~74 m north.
        parcel = _make_parcel("P", _square_polygon(-79.400, 43.7000, 0.00045))
        building = Building(
            geometry=_square_polygon(-79.4002, 43.6994, 0.00025),
            height_m=30.0,
        )
        result = analyze_parcel(parcel, _massing_index([building]))
        self.assertEqual(result.quality, "measured")
        self.assertLess(result.unshadowed_fraction, 1.0)
        self.assertGreaterEqual(result.unshadowed_fraction, 0.0)

    def test_tier3_only_returns_none_unavailable(self):
        parcel = _make_parcel("P", _square_polygon(-79.400, 43.700, 0.0005))
        # Building near the parcel but with no usable height.
        building = Building(
            geometry=_square_polygon(-79.4001, 43.6996, 0.0002),
            height_m=None,
        )
        result = analyze_parcel(parcel, _massing_index([building]))
        self.assertIsNone(result.unshadowed_fraction)
        self.assertEqual(result.quality, "unavailable")

    def test_tier2_envelope_marks_quality_estimated(self):
        # Tier 2 = height present but footprint unusable. The load-time filter
        # in massing.py drops degenerate footprints, so this case is rare in
        # production — but the algorithm must still classify correctly.
        # We synthesize a degenerate footprint (zero-area polygon) here.
        parcel = _make_parcel("P", _square_polygon(-79.400, 43.700, 0.0005))
        degenerate = Polygon([
            (-79.4001, 43.6996),
            (-79.4001, 43.6996),
            (-79.4001, 43.6996),
        ])
        # Tag a non-degenerate envelope-source: place a separate visible building
        # in the area (tier1) so the shadow envelope can be observed.
        tier2_building = Building(geometry=degenerate, height_m=30.0)
        tier1_building = Building(
            geometry=_square_polygon(-79.4003, 43.6994, 0.0002),
            height_m=30.0,
        )
        result = analyze_parcel(parcel, _massing_index([tier2_building, tier1_building]))
        self.assertEqual(result.quality, "estimated")
        self.assertIsNotNone(result.unshadowed_fraction)

    def test_tier2_only_still_returns_a_fraction(self):
        # Pure-tier2 case: only candidate is height-only.
        parcel = _make_parcel("P", _square_polygon(-79.400, 43.700, 0.0005))
        degenerate = Polygon([
            (-79.4001, 43.6996),
            (-79.4001, 43.6996),
            (-79.4001, 43.6996),
        ])
        building = Building(geometry=degenerate, height_m=30.0)
        result = analyze_parcel(parcel, _massing_index([building]))
        self.assertEqual(result.quality, "estimated")
        self.assertIsNotNone(result.unshadowed_fraction)

    def test_candidate_cap_fires_when_more_than_max_in_radius(self):
        # Place 150 small buildings packed inside the search radius of one parcel.
        # All have height_m=10 and tiny footprints. Verify the run completes
        # (cap protects O(N) work) and quality is still "measured".
        parcel = _make_parcel("P", _square_polygon(-79.400, 43.700, 0.0005))
        buildings = []
        # 150 buildings in a 15×10 grid, each 5m × 5m, centered ~50m south.
        n_x, n_y = 15, 10
        spacing = 0.00006  # ~5 m at this latitude
        size = 0.00003     # ~2.5 m
        base_lon = -79.4015
        base_lat = 43.6995
        for ix in range(n_x):
            for iy in range(n_y):
                lon = base_lon + ix * spacing
                lat = base_lat + iy * spacing
                buildings.append(Building(
                    geometry=_square_polygon(lon, lat, size),
                    height_m=10.0,
                ))
        self.assertGreater(len(buildings), MAX_CANDIDATES_PER_PARCEL)

        result = analyze_parcel(parcel, _massing_index(buildings))
        # Quality stays measured (all tier1) — cap is just a candidate selector.
        self.assertEqual(result.quality, "measured")
        self.assertIsNotNone(result.unshadowed_fraction)

    def test_short_circuit_at_high_elevation_means_smaller_shadow(self):
        # Compare winter (22°) vs summer (70°) elevation — summer should have
        # less shadow → higher unshadowed fraction.
        parcel = _make_parcel("P", _square_polygon(-79.400, 43.7000, 0.00045))
        building = Building(
            geometry=_square_polygon(-79.4002, 43.6994, 0.00025),
            height_m=30.0,
        )
        idx = _massing_index([building])
        winter_only = analyze_parcel(parcel, idx, sun_angles=[(180, 22)])
        summer_only = analyze_parcel(parcel, idx, sun_angles=[(180, 70)])
        self.assertGreater(summer_only.unshadowed_fraction, winter_only.unshadowed_fraction)


if __name__ == "__main__":
    unittest.main()
