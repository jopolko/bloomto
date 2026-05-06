"""Tests for `compute_subway_stops` and `compute_major_transit_stops` (Task 16).

The fixture is a hand-crafted minimal GTFS bundle built programmatically in
`setUp` (no checked-in binaries). It models the three TTC route_types we care
about: 1 = subway, 0 = streetcar, 3 = bus.

Layout:
  - Route LINE1  (route_type=1, subway)    serves stops S1, S2
  - Route 501    (route_type=0, streetcar) serves stops S3
  - Route 7BUS   (route_type=3, bus)       serves stops S4, S5

Stop S1 is served by *two* subway trips to exercise dedup. The expected
returns:
  - compute_subway_stops          → {S1, S2}
  - compute_major_transit_stops   → {S1, S2, S3}
  - bus-only stops S4, S5         → never appear
"""

import csv
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.sources.ttc import (
    CACHE_FILENAME,
    compute_major_transit_stops,
    compute_subway_stops,
)


def _csv_text(rows: list[dict], fieldnames: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _build_gtfs_zip(zip_path: Path) -> None:
    routes = [
        {"route_id": "LINE1", "route_short_name": "LINE1", "route_long_name": "Yonge", "route_type": "1"},
        {"route_id": "501",   "route_short_name": "501",   "route_long_name": "Queen", "route_type": "0"},
        {"route_id": "7BUS",  "route_short_name": "7",     "route_long_name": "Bathurst", "route_type": "3"},
    ]
    trips = [
        {"route_id": "LINE1", "service_id": "WK", "trip_id": "T1"},
        {"route_id": "LINE1", "service_id": "WK", "trip_id": "T2"},  # second subway trip — exercises dedup on S1
        {"route_id": "501",   "service_id": "WK", "trip_id": "T3"},
        {"route_id": "7BUS",  "service_id": "WK", "trip_id": "T4"},
    ]
    stop_times = [
        {"trip_id": "T1", "stop_id": "S1", "stop_sequence": "1"},
        {"trip_id": "T1", "stop_id": "S2", "stop_sequence": "2"},
        {"trip_id": "T2", "stop_id": "S1", "stop_sequence": "1"},  # duplicate stop_id under a different trip
        {"trip_id": "T3", "stop_id": "S3", "stop_sequence": "1"},
        {"trip_id": "T4", "stop_id": "S4", "stop_sequence": "1"},
        {"trip_id": "T4", "stop_id": "S5", "stop_sequence": "2"},
    ]
    stops = [
        {"stop_id": "S1", "stop_name": "Bloor",   "stop_lat": "43.6708", "stop_lon": "-79.3853"},
        {"stop_id": "S2", "stop_name": "St Clair","stop_lat": "43.6878", "stop_lon": "-79.3940"},
        {"stop_id": "S3", "stop_name": "Queen+Spadina", "stop_lat": "43.6485", "stop_lon": "-79.3970"},
        {"stop_id": "S4", "stop_name": "BusStop4","stop_lat": "43.6700", "stop_lon": "-79.4000"},
        {"stop_id": "S5", "stop_name": "BusStop5","stop_lat": "43.6800", "stop_lon": "-79.4100"},
    ]

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("routes.txt", _csv_text(routes, list(routes[0].keys())))
        z.writestr("trips.txt", _csv_text(trips, list(trips[0].keys())))
        z.writestr("stop_times.txt", _csv_text(stop_times, list(stop_times[0].keys())))
        z.writestr("stops.txt", _csv_text(stops, list(stops[0].keys())))


class TtcRouteTypeFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        _build_gtfs_zip(self.tmpdir / CACHE_FILENAME)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_compute_subway_stops_returns_only_subway_served(self):
        points = compute_subway_stops(self.tmpdir)
        # S1 (served by T1 + T2 — both subway) and S2 (T1 — subway).
        self.assertEqual(len(points), 2)
        coords = sorted((round(p.x, 4), round(p.y, 4)) for p in points)
        self.assertEqual(coords, sorted([
            (-79.3853, 43.6708),  # S1 Bloor
            (-79.3940, 43.6878),  # S2 St Clair
        ]))

    def test_compute_major_transit_stops_includes_streetcar(self):
        points = compute_major_transit_stops(self.tmpdir)
        self.assertEqual(len(points), 3)
        coords = sorted((round(p.x, 4), round(p.y, 4)) for p in points)
        self.assertEqual(coords, sorted([
            (-79.3853, 43.6708),  # S1 Bloor (subway)
            (-79.3940, 43.6878),  # S2 St Clair (subway)
            (-79.3970, 43.6485),  # S3 Queen+Spadina (streetcar)
        ]))

    def test_bus_stops_excluded_from_both(self):
        major = compute_major_transit_stops(self.tmpdir)
        subway = compute_subway_stops(self.tmpdir)
        # S4 / S5 are bus-only — must never appear.
        bus_coords = {(-79.4000, 43.6700), (-79.4100, 43.6800)}
        major_coords = {(round(p.x, 4), round(p.y, 4)) for p in major}
        subway_coords = {(round(p.x, 4), round(p.y, 4)) for p in subway}
        self.assertTrue(bus_coords.isdisjoint(major_coords))
        self.assertTrue(bus_coords.isdisjoint(subway_coords))

    def test_subway_stop_dedup_when_served_by_multiple_trips(self):
        # S1 is served by trips T1 and T2; the output must contain it exactly once.
        points = compute_subway_stops(self.tmpdir)
        s1_coords = [(p.x, p.y) for p in points if abs(p.x - -79.3853) < 1e-4]
        self.assertEqual(len(s1_coords), 1)


if __name__ == "__main__":
    unittest.main()
