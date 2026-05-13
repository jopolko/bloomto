"""Tests for `tools/sources/building_outlines.compute_coverage`.

Builds a tiny CSV fixture in `setUp` (no committed binary) with four hand-placed
building MultiPolygons, then constructs three Parcel scenarios:
  - parcel A: contains one full building → coverage > 0
  - parcel B: vacant → coverage 0.0
  - parcel C: shares a building straddling its boundary with parcel A →
    coverage shows only the intersected fraction (less than the full building area)

Coordinates are placed in WGS84 around 43.7°N, with parcels sized so the
expected coverage fraction is verifiable by hand.
"""

import csv
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pyproj import Geod
from shapely.geometry import Polygon, mapping

from tools.sources.building_outlines import CACHE_FILENAME, compute_coverage
from tools.sources.zoning import Parcel

_GEOD = Geod(ellps="WGS84")


def _geodesic_area(geom) -> float:
    return abs(_GEOD.geometry_area_perimeter(geom)[0])


def _make_parcel(parcel_id: str, polygon: Polygon, address: str | None = None) -> Parcel:
    centroid = polygon.centroid
    return Parcel(
        parcel_id=parcel_id,
        address=address,
        geometry=polygon,
        centroid=(centroid.x, centroid.y),
        area_m2=_geodesic_area(polygon),
    )


def _write_buildings_csv(csv_path: Path, buildings: list[Polygon]) -> None:
    """Write a CSV mimicking the Toronto Building Outlines schema.

    Includes one extra non-building row (`SUBTYPE_CODE = 9999`) to exercise
    the SUBTYPE_CODE filter — that row's geometry must NOT contribute to coverage.
    """
    fieldnames = [
        "_id", "SUBTYPE_CODE", "SUBTYPE_DESC", "ELEVATION", "DERIVED_HEIGHT",
        "OBJECTID", "LAST_GEOMETRY_MAINT", "LAST_ATTRIBUTE_MAINT", "geometry",
    ]
    rows = []
    for i, b in enumerate(buildings):
        rows.append({
            "_id": str(i + 1),
            "SUBTYPE_CODE": "9003",
            "SUBTYPE_DESC": "Building Outline",
            "ELEVATION": "100.0",
            "DERIVED_HEIGHT": "112.0",
            "OBJECTID": str(i + 1),
            "LAST_GEOMETRY_MAINT": "2023-01-01",
            "LAST_ATTRIBUTE_MAINT": "2023-01-01",
            "geometry": json.dumps(mapping(b)),
        })
    # Non-building decoy row: same geometry as one of the real buildings, but
    # SUBTYPE_CODE 9999. compute_coverage must skip it.
    rows.append({
        "_id": "999", "SUBTYPE_CODE": "9999", "SUBTYPE_DESC": "Canopy",
        "ELEVATION": "100.0", "DERIVED_HEIGHT": "105.0",
        "OBJECTID": "999", "LAST_GEOMETRY_MAINT": "2023-01-01",
        "LAST_ATTRIBUTE_MAINT": "2023-01-01",
        "geometry": json.dumps(mapping(buildings[0])),
    })

    with csv_path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


class CoverageRatioTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.cache_path = self.tmpdir / CACHE_FILENAME

        # Parcel A: 0.001° × 0.001° square at (-79.400, 43.700) — about 80m × 110m.
        self.parcel_a = _make_parcel("A", Polygon([
            (-79.400, 43.700), (-79.399, 43.700),
            (-79.399, 43.701), (-79.400, 43.701),
            (-79.400, 43.700),
        ]))

        # Parcel B: same shape, shifted east. Will be vacant.
        self.parcel_b = _make_parcel("B", Polygon([
            (-79.395, 43.700), (-79.394, 43.700),
            (-79.394, 43.701), (-79.395, 43.701),
            (-79.395, 43.700),
        ]))

        # Parcel C: directly east of parcel A, sharing a boundary.
        self.parcel_c = _make_parcel("C", Polygon([
            (-79.399, 43.700), (-79.398, 43.700),
            (-79.398, 43.701), (-79.399, 43.701),
            (-79.399, 43.700),
        ]))

        # Building 1: fully inside parcel A (top-left quadrant).
        self.building_inside_a = Polygon([
            (-79.3998, 43.7002), (-79.3994, 43.7002),
            (-79.3994, 43.7008), (-79.3998, 43.7008),
            (-79.3998, 43.7002),
        ])
        # Building 2: straddles A↔C boundary at lon=-79.399 (50/50 split).
        self.building_straddling = Polygon([
            (-79.3995, 43.7003), (-79.3985, 43.7003),
            (-79.3985, 43.7007), (-79.3995, 43.7007),
            (-79.3995, 43.7003),
        ])
        # Building 3: outside all three parcels (well east of B).
        self.building_outside = Polygon([
            (-79.380, 43.700), (-79.379, 43.700),
            (-79.379, 43.701), (-79.380, 43.701),
            (-79.380, 43.700),
        ])
        # Building 4: tiny, fully inside C.
        self.building_inside_c = Polygon([
            (-79.3987, 43.7004), (-79.3985, 43.7004),
            (-79.3985, 43.7006), (-79.3987, 43.7006),
            (-79.3987, 43.7004),
        ])

        _write_buildings_csv(self.cache_path, [
            self.building_inside_a,
            self.building_straddling,
            self.building_outside,
            self.building_inside_c,
        ])

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parcel_with_full_building_has_positive_coverage(self):
        coverage = compute_coverage(
            [self.parcel_a, self.parcel_b, self.parcel_c],
            self.tmpdir,
        )
        # Parcel A contains building 1 fully + half of building 2.
        full_inside = _geodesic_area(self.building_inside_a)
        half_straddle = _geodesic_area(self.building_straddling) / 2
        expected_a = (full_inside + half_straddle) / self.parcel_a.area_m2
        self.assertAlmostEqual(coverage["A"], expected_a, places=2)

    def test_vacant_parcel_has_zero_coverage(self):
        coverage = compute_coverage(
            [self.parcel_a, self.parcel_b, self.parcel_c],
            self.tmpdir,
        )
        self.assertEqual(coverage["B"], 0.0)

    def test_straddling_building_split_between_parcels(self):
        coverage = compute_coverage(
            [self.parcel_a, self.parcel_b, self.parcel_c],
            self.tmpdir,
        )
        # Parcel C gets half the straddling building + the small inside_c building.
        half_straddle = _geodesic_area(self.building_straddling) / 2
        full_inside_c = _geodesic_area(self.building_inside_c)
        expected_c = (half_straddle + full_inside_c) / self.parcel_c.area_m2
        self.assertAlmostEqual(coverage["C"], expected_c, places=2)

        # And the sum of A's and C's straddling fractions ≈ the full building area.
        sum_AC_geodesic = (
            coverage["A"] * self.parcel_a.area_m2
            + coverage["C"] * self.parcel_c.area_m2
        )
        # Includes building_inside_a + full straddle + building_inside_c.
        expected_sum = (
            _geodesic_area(self.building_inside_a)
            + _geodesic_area(self.building_straddling)
            + _geodesic_area(self.building_inside_c)
        )
        self.assertAlmostEqual(sum_AC_geodesic, expected_sum, delta=0.5)

    def test_coverage_clamped_to_unit_interval(self):
        coverage = compute_coverage(
            [self.parcel_a, self.parcel_b, self.parcel_c],
            self.tmpdir,
        )
        for pid, ratio in coverage.items():
            with self.subTest(pid=pid):
                self.assertGreaterEqual(ratio, 0.0)
                self.assertLessEqual(ratio, 1.0)

    def test_decoy_subtype_row_excluded_from_coverage(self):
        # The decoy SUBTYPE_CODE=9999 row uses building_inside_a's geometry.
        # If the filter were broken, parcel A's coverage would double-count it.
        coverage = compute_coverage([self.parcel_a], self.tmpdir)
        full_inside = _geodesic_area(self.building_inside_a)
        half_straddle = _geodesic_area(self.building_straddling) / 2
        expected_a = (full_inside + half_straddle) / self.parcel_a.area_m2
        # If the decoy were counted, expected would be ~2× full_inside + half_straddle.
        self.assertAlmostEqual(coverage["A"], expected_a, places=2)


if __name__ == "__main__":
    unittest.main()
