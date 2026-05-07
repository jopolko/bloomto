"""End-to-end test for `tools/build_parcels.py:assemble_parcel_payload` (Task 27).

All fixtures are built inline (no committed binaries — same convention as
`test_heritage.py`, `test_corner_lots.py`, etc.). The fixture set deliberately
exercises every eligibility / quality branch:

  - parcel A: residential, near transit, has solar rooftop, no heritage      → eligible, kept
  - parcel B: residential, near transit, has heritage point inside (Part IV) → ineligible (heritage gate)
  - parcel C: residential, no transit nearby (~700m away)                    → eligible (within 1500m wide window), kept on wire
  - parcel D: outside any neighborhood                                       → skipped (counted in stats)
"""

import unittest
from datetime import datetime, timezone

from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

from tools import parcel_io
from tools.build_parcels import assemble_parcel_payload
from tools.sources.heritage import HeritageIndex
from tools.sources.massing import Building
from tools.sources.neighborhoods import Neighborhood
from tools.sources.zoning import Parcel, ZoneRecord


def _zone_record(zone_class: str) -> ZoneRecord:
    """Minimal ZoneRecord for fixtures: only `zone_class` matters for the
    multiplier-table fallback path; all other by-law parameters are absent."""
    return ZoneRecord(
        zone_class=zone_class,
        zone_string=zone_class,
        units=None,
        fsi=None,
        min_lot_frontage_m=None,
        min_lot_area_m2=None,
        coverage_max=None,
        pct_residential=None,
    )


def _square(lon: float, lat: float, side: float) -> Polygon:
    return Polygon([
        (lon, lat),
        (lon + side, lat),
        (lon + side, lat + side),
        (lon, lat + side),
        (lon, lat),
    ])


def _heritage_index(*, points, statuses, addresses, address_to_status):
    """Build a HeritageIndex with the address_to_indices reverse map auto-derived.

    Keeps the e2e fixtures concise — tests only need to specify the four
    primary lists, the reverse index is computed from `addresses`.
    """
    address_to_indices: dict[str, list[int]] = {}
    for i, addr in enumerate(addresses):
        if addr:
            address_to_indices.setdefault(addr, []).append(i)
    return HeritageIndex(
        point_tree=STRtree(points),
        points=list(points),
        statuses=list(statuses),
        addresses=list(addresses),
        address_to_status=dict(address_to_status),
        address_to_indices=address_to_indices,
    )


def _make_neighborhood(name: str, polygon: Polygon, *, area_km2: float = 1.0) -> Neighborhood:
    c = polygon.centroid
    return Neighborhood(
        name=name,
        polygon=polygon,
        centroid_lat=c.y,
        centroid_lng=c.x,
        area_km2=area_km2,
    )


def _make_parcel(parcel_id: str, polygon: Polygon, *, address: str | None = None) -> Parcel:
    c = polygon.centroid
    return Parcel(
        parcel_id=parcel_id,
        address=address,
        geometry=polygon,
        centroid=(c.x, c.y),
        area_m2=500.0,  # synthetic; not exercised by the formula
    )


class ParcelE2ETests(unittest.TestCase):
    def setUp(self):
        # Neighborhood polygon covering parcels A/B/C (lon -79.410..-79.390,
        # lat 43.695..43.715). Parcel D sits far outside, exercising the
        # skipped-no-neighborhood stats counter.
        self.nb_main = _make_neighborhood(
            "Test Hood",
            _square(-79.410, 43.695, 0.020),
        )
        self.neighborhoods = [self.nb_main]

        # Parcels (each ~50 m × 50 m at 0.0005°)
        self.parcel_a = _make_parcel("A", _square(-79.4000, 43.7000, 0.0005), address="100 A St")
        self.parcel_b = _make_parcel("B", _square(-79.4010, 43.7000, 0.0005), address="200 B St")
        # Parcel C is 700+ m east of the transit stops → falls outside the
        # 500 m TRANSIT_BUFFER_M gate.
        self.parcel_c = _make_parcel("C", _square(-79.3920, 43.7000, 0.0005), address="300 C St")
        self.parcel_d = _make_parcel("D", _square(-79.300, 43.500, 0.0005), address="999 D St")
        self.parcels = [self.parcel_a, self.parcel_b, self.parcel_c, self.parcel_d]

        # Heritage: one Part IV record inside parcel B (point-in-parcel match,
        # no address-join hit because parcel B's address differs).
        heritage_pt = Point(-79.40075, 43.70025)
        self.heritage_index = _heritage_index(
            points=[heritage_pt],
            statuses=["part_iv"],
            addresses=["999 NONMATCH ST"],  # not equal to any parcel address
            address_to_status={"999 NONMATCH ST": "part_iv"},
        )

        # Zone index: one residential zone covering A/B/C.
        zone_polygon = _square(-79.405, 43.6995, 0.020)
        self.zone_index = (STRtree([zone_polygon]), [_zone_record("RD")])
        self.multipliers = {"RD": 4}

        # Transit: streetcar/subway stops within 25 m of parcels A and B.
        # 1° lon ≈ 80 km at this latitude — 0.0001° ≈ 8 m.
        # Stops trees are projected to EPSG:26917 metres via
        # `_build_stops_tree_m` so `_distance_to_nearest_stop_m` can rank
        # nearest stops in true metres rather than degree-space planar.
        from tools.build_parcels import _build_stops_tree_m
        streetcar_stop = Point(-79.40005, 43.7003)
        subway_stop = Point(-79.40005, 43.7003)
        self.streetcar_tree = _build_stops_tree_m([streetcar_stop])
        self.subway_tree = _build_stops_tree_m([subway_stop])

        # Massing: one small low building near parcel A so shadow analysis
        # has a tier1 candidate (but the parcel still scores well above 80).
        building_b = Building(
            geometry=_square(-79.4002, 43.6997, 0.0001),
            height_m=5.0,
        )
        self.massing_index = (STRtree([building_b.geometry]), [building_b])

        # Building outlines for coverage: one building inside parcel A.
        bo_geom = _square(-79.39995, 43.70005, 0.0002)
        self.building_geoms = [bo_geom]
        self.building_tree = STRtree([bo_geom])

        # Solar rooftop point inside parcel A with a high kWh.
        solar_pt = Point(-79.39990, 43.70005)
        self.solar_tree = STRtree([solar_pt])
        self.solar_kwh = [10000.0]
        self.solar_p95 = 10000.0

        # Centreline streets making parcel A a corner: one east-west and one
        # north-south line touching parcel A's NE-corner buffer (3 m).
        # Parcel A occupies lon [-79.4000, -79.3995], lat [43.7000, 43.7005].
        # Place lines just outside its boundary so the buffer test triggers.
        from shapely.geometry import LineString
        ew_line = LineString([(-79.4002, 43.70055), (-79.3993, 43.70055)])  # north of A
        ns_line = LineString([(-79.39945, 43.6998), (-79.39945, 43.7007)])  # east of A
        # Both must have distinct LINEAR_NAME_IDs.
        self.centreline_index = (STRtree([ew_line, ns_line]), [100, 200], set())
        self.built_year_by_name = {"Test Hood": 1955}  # postwar window

    def _build(self, *, include_non_eligible: bool = False):
        # Empty institutions / flood / rapidto / per-mode-transit indices —
        # synthetic fixtures don't exercise these layers; real rebuilds load
        # the live datasets via the relevant compute_* factories.
        from tools.sources.institutions import InstitutionsIndex
        from tools.sources.flood import FloodIndex
        from tools.sources.trca_floodplain import TrcaIndex
        from tools.sources.building_permits import PermitIndex
        from tools.sources.street_trees import StreetTreeIndex
        from tools.sources.sixplex_district import SixplexIndex
        from datetime import date
        empty_institutions = InstitutionsIndex(
            tree=STRtree([]), geometries=[], categories=[], is_polygon=[],
        )
        empty_flood = FloodIndex(tree=STRtree([]), polygons=[])
        empty_trca = TrcaIndex(tree=STRtree([]), polygons=[])
        empty_tree = STRtree([])
        empty_permits = PermitIndex(
            permits=[], address_to_indices={}, spatial_tree=STRtree([]),
            centroids=[], claimed=set(),
        )
        empty_street_trees = StreetTreeIndex(
            tree=STRtree([]), points=[], dbh_cm=[],
        )
        empty_sixplex = SixplexIndex(
            tey_polygons=[], tey_tree=STRtree([]),
        )
        return assemble_parcel_payload(
            neighborhoods=self.neighborhoods,
            parcels=self.parcels,
            heritage_index=self.heritage_index,
            institutions_index=empty_institutions,
            ttc_station_index=empty_tree,
            landuse_index=(empty_tree, []),
            flood_index=empty_flood,
            trca_index=empty_trca,
            rapidto_tree=empty_tree,
            zone_index=self.zone_index,
            multipliers=self.multipliers,
            transit_subway_tree=self.subway_tree,
            transit_streetcar_only_tree=empty_tree,
            transit_bus_tree=empty_tree,
            massing_index=self.massing_index,
            building_geoms=self.building_geoms,
            building_tree=self.building_tree,
            solar_tree=self.solar_tree,
            solar_kwh=self.solar_kwh,
            solar_p95=self.solar_p95,
            centreline_index=self.centreline_index,
            built_year_by_name=self.built_year_by_name,
            permit_index=empty_permits,
            permit_freshness_cutoff=date(2021, 1, 1),
            bike_tree=empty_tree,
            bike_lines=[],
            street_tree_index=empty_street_trees,
            sixplex_index=empty_sixplex,
            nb_canopy_by_name={n.name: 30 for n in self.neighborhoods},
            include_non_eligible=include_non_eligible,
        )

    def test_payload_validates_against_parcel_io(self):
        payload = self._build()
        # Should not raise.
        parcel_io.validate(payload)

    def test_default_skips_ineligible_parcels(self):
        payload = self._build()
        addresses = [f["properties"]["address"] for f in payload["features"]]
        # Parcel A: residential, near transit, no heritage → eligible.
        # Parcel B: Part IV heritage → blocked by eligibility gate.
        # Parcel C: ~700m from transit — within the wide ELIGIBLE buffer
        #   (1500m) so it stays on the wire; downstream projection will
        #   apply the tighter 500m ELITE gate.
        # Parcel D: outside neighborhood polygon → skipped before eligibility.
        self.assertIn("100 A St", addresses)
        self.assertNotIn("200 B St", addresses)
        self.assertIn("300 C St", addresses)
        self.assertNotIn("999 D St", addresses)

    def test_include_non_eligible_keeps_part_iv(self):
        payload = self._build(include_non_eligible=True)
        addresses = [f["properties"]["address"] for f in payload["features"]]
        # B (Part IV) reappears; D still excluded (no neighborhood).
        self.assertIn("100 A St", addresses)
        self.assertIn("200 B St", addresses)
        self.assertIn("300 C St", addresses)
        self.assertNotIn("999 D St", addresses)

    def test_features_sorted_by_lot_area_desc(self):
        payload = self._build(include_non_eligible=True)
        areas = [f["properties"].get("lotAreaM2") or 0 for f in payload["features"]]
        self.assertEqual(areas, sorted(areas, reverse=True))

    def test_meta_has_expected_keys(self):
        payload = self._build()
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertIn("solarMethodology", payload["meta"])
        self.assertIn("shadowAnalysis", payload["meta"])
        self.assertIn("stats", payload["meta"])
        # Synthesised-formula meta keys removed 2026-05-07.
        self.assertNotIn("scoreFormula", payload["meta"])
        self.assertNotIn("bloomFormula", payload["meta"])

    def test_stats_count_skipped_no_neighborhood(self):
        payload = self._build(include_non_eligible=True)
        # Parcel D is outside the neighborhood polygon and should be counted as skipped.
        self.assertEqual(payload["meta"]["stats"]["skippedNoNeighborhood"], 1)

    def test_parcel_a_no_heritage_when_friction_clear(self):
        # Successor to the prior bloom test. Verify the city primitives that
        # used to feed bloom are present + correct on parcel A. Bloom field
        # itself was dropped 2026-05-07.
        payload = self._build()
        feats_by_addr = {f["properties"]["address"]: f for f in payload["features"]}
        a = feats_by_addr["100 A St"]
        self.assertIsNone(a["properties"]["heritageStatus"])
        self.assertLess(a["properties"]["distSubwayStreetcarM"], 500)
        self.assertFalse(a["properties"]["inRegulatedArea"])
        # Synthetic fixture: not in sixplex district.
        self.assertFalse(a["properties"]["sixplexEligible"])
        # Bloom field removed.
        self.assertNotIn("bloom", a["properties"])

    def test_part_iv_parcel_when_included_shows_status(self):
        payload = self._build(include_non_eligible=True)
        feats_by_addr = {f["properties"]["address"]: f for f in payload["features"]}
        b = feats_by_addr["200 B St"]
        self.assertEqual(b["properties"]["heritageStatus"], "part_iv")
        # Score / bloom fields removed; verify they're not in the wire.
        self.assertNotIn("score", b["properties"])
        self.assertNotIn("bloom", b["properties"])

    def test_parcel_c_far_from_transit_records_distance(self):
        payload = self._build(include_non_eligible=True)
        feats_by_addr = {f["properties"]["address"]: f for f in payload["features"]}
        c = feats_by_addr["300 C St"]
        # No major-transit within 500m → distSubwayStreetcarM should be large.
        self.assertGreaterEqual(c["properties"]["distSubwayStreetcarM"], 500)

    def test_postwar_neighborhood_flag_set_for_1955_hood(self):
        payload = self._build()
        a = payload["features"][0]
        self.assertTrue(a["properties"]["postwarNeighborhood"])

    def test_idempotent_across_two_runs(self):
        # Two assembled payloads should be byte-identical modulo `meta.generatedAt`.
        p1 = self._build()
        p2 = self._build()
        p1["meta"]["generatedAt"] = ""
        p2["meta"]["generatedAt"] = ""
        self.assertEqual(p1, p2)

    def test_unknown_zone_class_raises_loudly(self):
        # Replace the recognized "RD" zone label with an unrecognized "XXX".
        # The orchestrator must not silently fall through to a default — see
        # zone-class-coverage bug analysis.
        zone_polygon = self.zone_index[0].geometries[0]
        self.zone_index = (STRtree([zone_polygon]), [_zone_record("XXX")])
        with self.assertRaises(KeyError) as ctx:
            self._build()
        self.assertIn("XXX", str(ctx.exception))
        self.assertIn("zoning_multipliers.json", str(ctx.exception))

    def test_part_v_parcel_records_status(self):
        # 2026-05-07 — score formula dropped. Heritage tier is now just a
        # surfaced city primitive; downstream projections (build_parcels_top)
        # use it as a binary "heritage clear" gate, not a score multiplier.
        a_pt = Point(-79.39998, 43.70025)  # inside parcel A
        self.heritage_index = _heritage_index(
            points=[a_pt],
            statuses=["part_v"],
            addresses=["999 NONMATCH ST"],
            address_to_status={"999 NONMATCH ST": "part_v"},
        )
        payload = self._build()
        feats_by_addr = {f["properties"]["address"]: f for f in payload["features"]}
        a = feats_by_addr["100 A St"]
        self.assertEqual(a["properties"]["heritageStatus"], "part_v")

    def test_listed_parcel_records_status(self):
        a_pt = Point(-79.39998, 43.70025)
        self.heritage_index = _heritage_index(
            points=[a_pt],
            statuses=["listed"],
            addresses=["999 NONMATCH ST"],
            address_to_status={"999 NONMATCH ST": "listed"},
        )
        payload = self._build()
        feats_by_addr = {f["properties"]["address"]: f for f in payload["features"]}
        a = feats_by_addr["100 A St"]
        self.assertEqual(a["properties"]["heritageStatus"], "listed")

    def test_part_iv_parcel_excluded_by_default(self):
        # Same as the Part V/Listed cases but Part IV → hard block.
        a_pt = Point(-79.39998, 43.70025)
        self.heritage_index = _heritage_index(
            points=[a_pt],
            statuses=["part_iv"],
            addresses=["999 NONMATCH ST"],
            address_to_status={"999 NONMATCH ST": "part_iv"},
        )
        payload = self._build(include_non_eligible=True)
        feats_by_addr = {f["properties"]["address"]: f for f in payload["features"]}
        a = feats_by_addr["100 A St"]
        self.assertEqual(a["properties"]["heritageStatus"], "part_iv")
        # In default-build mode (without include_non_eligible) Part IV
        # is excluded by the eligibility gate. Verify here:
        default = self._build()
        default_addrs = [f["properties"]["address"] for f in default["features"]]
        self.assertNotIn("100 A St", default_addrs)

    def test_address_join_takes_precedence_over_point_in_parcel(self):
        # The classic subdivision-edge case: a heritage record's address
        # matches parcel A, but its geocoded point fell on parcel B's polygon
        # (perhaps the original lot was subdivided after the register was
        # last updated). Per Req 3.2, parcel A (address match) MUST receive
        # the status, and parcel B (false-positive geometry hit) MUST NOT —
        # the address-join is authoritative and `claimed` blocks the
        # point-in-parcel fallback from re-flagging B.
        b_pt = Point(-79.40075, 43.70025)  # inside parcel B
        self.heritage_index = _heritage_index(
            points=[b_pt],
            statuses=["part_v"],
            addresses=["100 A ST"],  # matches parcel A's normalized address
            address_to_status={"100 A ST": "part_v"},
        )
        # Parcel order matters here: parcel A is processed first (parcels[0])
        # so its address-join claims the record before B's point-in-parcel
        # branch runs. The orchestrator's `claimed` set is shared across
        # parcels; B sees the record already claimed and skips it.
        payload = self._build(include_non_eligible=True)
        feats_by_addr = {f["properties"]["address"]: f for f in payload["features"]}
        a = feats_by_addr["100 A St"]
        b = feats_by_addr["200 B St"]
        self.assertEqual(a["properties"]["heritageStatus"], "part_v")
        self.assertIsNone(b["properties"]["heritageStatus"])

    def test_meta_stats_has_per_tier_counts_no_legacy_key(self):
        payload = self._build(include_non_eligible=True)
        stats = payload["meta"]["stats"]
        for k in ("heritagePartIV", "heritagePartV", "heritageListed", "heritageUnjoined"):
            self.assertIn(k, stats)
        self.assertNotIn("heritageFlagged", stats)
        # Default fixture has 1 Part IV record and parcel B should pick it up.
        self.assertEqual(stats["heritagePartIV"], 1)
        self.assertEqual(stats["heritagePartV"], 0)
        self.assertEqual(stats["heritageListed"], 0)


if __name__ == "__main__":
    unittest.main()
