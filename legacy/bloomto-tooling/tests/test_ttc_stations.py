"""Smoke + correctness tests for `tools.sources.ttc_stations`.

The exclusion gate uses real TTC subway-stop coordinates (not synthetic
fixtures) because the buffered geometry is the actual product behavior.
The 22 Chester Ave coordinates from the 2026-05-05 audit are the canary:
they MUST be excluded by the gate. A single-point true-residential parcel
several km from any subway stop MUST NOT be excluded.
"""

import unittest
from pathlib import Path

from shapely.geometry import Point, Polygon

from tools.sources import ttc_stations


CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


class TtcStationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Skip if the GTFS cache hasn't been hydrated — these tests are
        # integration-flavoured and need the real TTC data.
        if not (CACHE_DIR / "ttc_gtfs.zip").exists():
            raise unittest.SkipTest("ttc_gtfs.zip not in cache; skipping")
        cls.index = ttc_stations.compute_station_exclusion_index(CACHE_DIR)

    def test_index_is_nonempty(self):
        # GTFS has 148 subway stops; exclusion index should match that count.
        self.assertGreater(len(self.index.geometries), 100)

    def test_22_chester_ave_excluded(self):
        # Canary case: lng=-79.35259, lat=43.67823 — Chester Station, the
        # parcel that ranked #1 (score 99) in the 2026-05-05 elite set
        # because TTC stations weren't in the existing institutional ETL.
        # Buffered exclusion geometry must contain a point at this location.
        parcel = Polygon([
            (-79.35265, 43.67815),
            (-79.35253, 43.67815),
            (-79.35253, 43.67830),
            (-79.35265, 43.67830),
        ])
        self.assertTrue(
            ttc_stations.is_ttc_station(parcel, self.index),
            "22 Chester Ave should be flagged as TTC infrastructure",
        )

    def test_far_from_subway_not_excluded(self):
        # Lat/lng deep in Scarborough where there's no subway line.
        # Should never trip the exclusion gate.
        parcel = Polygon([
            (-79.18000, 43.78000),
            (-79.17995, 43.78000),
            (-79.17995, 43.78005),
            (-79.18000, 43.78005),
        ])
        self.assertFalse(ttc_stations.is_ttc_station(parcel, self.index))


if __name__ == "__main__":
    unittest.main()
