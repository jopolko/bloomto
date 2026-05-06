"""Tests for `streets.compute_corner_lots` (Task 23).

Centreline fixture is built programmatically as a small GeoJSON file in setUp
(no committed binary). Three streets:
  - Yonge St   (north–south, LINEAR_NAME_ID = 100) — runs along x = -79.400
  - Bloor St   (east–west,   LINEAR_NAME_ID = 200) — runs along y = 43.700
  - Lane (laneway, LINEAR_NAME_ID = -1 / unnamed)

Parcels:
  - corner_parcel:   touches both Yonge AND Bloor → is_corner == True
  - midblock_parcel: touches only Yonge          → is_corner == False
  - interior_parcel: far from all streets        → is_corner == False
  - laneway_parcel:  touches Yonge + an unnamed laneway → is_corner == False
                     (the laneway alone doesn't count; only one named street)
  - same_street_segs_parcel: touches two segments of *Yonge* → is_corner == False
                             (digitization breaks share LINEAR_NAME_ID = 100)
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import Polygon, mapping

from tools.sources.streets import CACHE_FILENAME, compute_corner_lots
from tools.sources.zoning import Parcel


def _make_parcel(parcel_id: str, polygon: Polygon) -> Parcel:
    c = polygon.centroid
    return Parcel(
        parcel_id=parcel_id,
        address=None,
        geometry=polygon,
        centroid=(c.x, c.y),
        area_m2=1.0,
    )


def _make_feature(geometry_dict: dict, linear_name_id) -> dict:
    return {
        "type": "Feature",
        "properties": {"LINEAR_NAME_ID": linear_name_id, "LINEAR_NAME_FULL": "x"},
        "geometry": geometry_dict,
    }


def _build_centreline_geojson(out_path: Path) -> None:
    yonge_north = {  # north of intersection
        "type": "LineString",
        "coordinates": [[-79.400, 43.700], [-79.400, 43.710]],
    }
    yonge_south = {  # south of intersection (digitization break — same LINEAR_NAME_ID)
        "type": "LineString",
        "coordinates": [[-79.400, 43.690], [-79.400, 43.700]],
    }
    bloor_east = {
        "type": "LineString",
        "coordinates": [[-79.400, 43.700], [-79.390, 43.700]],
    }
    bloor_west = {
        "type": "LineString",
        "coordinates": [[-79.410, 43.700], [-79.400, 43.700]],
    }
    laneway = {
        "type": "LineString",
        "coordinates": [[-79.4005, 43.694], [-79.4005, 43.696]],
    }

    payload = {
        "type": "FeatureCollection",
        "features": [
            _make_feature(yonge_north, 100),
            _make_feature(yonge_south, 100),
            _make_feature(bloor_east, 200),
            _make_feature(bloor_west, 200),
            _make_feature(laneway, None),  # unnamed → -1 internally
        ],
    }
    out_path.write_text(json.dumps(payload))


class CornerLotTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        _build_centreline_geojson(self.tmpdir / CACHE_FILENAME)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_corner_parcel_touches_two_streets(self):
        # Square parcel covering the SE quadrant of the intersection: extends
        # 0.0005° east of Yonge and 0.0005° south of Bloor. The boundary's
        # 3 m buffer touches both streets.
        corner = _make_parcel("corner", Polygon([
            (-79.40005, 43.69995),
            (-79.39950, 43.69995),
            (-79.39950, 43.70005),
            (-79.40005, 43.70005),
            (-79.40005, 43.69995),
        ]))
        result = compute_corner_lots([corner], self.tmpdir)
        self.assertTrue(result["corner"])

    def test_midblock_parcel_touches_one_street(self):
        # Small parcel sitting east of Yonge but well south of Bloor.
        midblock = _make_parcel("mid", Polygon([
            (-79.40003, 43.6940),
            (-79.39950, 43.6940),
            (-79.39950, 43.6945),
            (-79.40003, 43.6945),
            (-79.40003, 43.6940),
        ]))
        result = compute_corner_lots([midblock], self.tmpdir)
        self.assertFalse(result["mid"])

    def test_interior_parcel_touches_no_streets(self):
        # Far enough east of Yonge and far enough south of Bloor that the
        # 3 m buffer doesn't touch either centreline.
        interior = _make_parcel("interior", Polygon([
            (-79.395, 43.692),
            (-79.394, 43.692),
            (-79.394, 43.693),
            (-79.395, 43.693),
            (-79.395, 43.692),
        ]))
        result = compute_corner_lots([interior], self.tmpdir)
        self.assertFalse(result["interior"])

    def test_laneway_only_does_not_count(self):
        # Parcel touches Yonge to the west and the unnamed laneway to the east.
        # Only Yonge is a named street — laneway alone shouldn't promote to corner.
        laneway_parcel = _make_parcel("lane", Polygon([
            (-79.40004, 43.6948),
            (-79.40046, 43.6948),
            (-79.40046, 43.6952),
            (-79.40004, 43.6952),
            (-79.40004, 43.6948),
        ]))
        result = compute_corner_lots([laneway_parcel], self.tmpdir)
        self.assertFalse(result["lane"])

    def test_same_street_two_segments_does_not_count(self):
        # Tall narrow parcel along Yonge that touches both north and south
        # segments (separated by an intersection). Both have LINEAR_NAME_ID=100,
        # so the distinct-id count is 1 → not a corner.
        long_parcel = _make_parcel("long", Polygon([
            (-79.40005, 43.6985),
            (-79.39990, 43.6985),
            (-79.39990, 43.7015),
            (-79.40005, 43.7015),
            (-79.40005, 43.6985),
        ]))
        # NB: this parcel ALSO touches Bloor (which crosses 43.700) at its
        # midpoint — so it actually IS a corner. Use a parcel that only spans
        # below the intersection, on Yonge's segment-break side.
        below_parcel = _make_parcel("below", Polygon([
            (-79.40005, 43.6989),
            (-79.39995, 43.6989),
            (-79.39995, 43.6995),
            (-79.40005, 43.6995),
            (-79.40005, 43.6989),
        ]))
        # This parcel touches only Yonge segments; same LINEAR_NAME_ID=100.
        result = compute_corner_lots([below_parcel], self.tmpdir)
        self.assertFalse(result["below"])


if __name__ == "__main__":
    unittest.main()
