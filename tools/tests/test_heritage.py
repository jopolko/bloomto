"""Tests for `tools/sources/heritage.py`.

Builds a tiny SHP zip programmatically in `setUp` rather than committing a
binary fixture — the source-of-truth is the test code itself, not a checked-in
SHP file whose vintage might drift from the production reader expectations.
"""

import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import shapefile  # pyshp
from shapely.geometry import Polygon

from tools.sources.heritage import (
    CACHE_FILENAME,
    KNOWN_STATUSES,
    STATUS_LISTED,
    STATUS_PART_IV,
    STATUS_PART_V,
    HeritageIndex,
    _iter_records_from_zip,
    compute_heritage,
    more_restrictive,
    normalize_address,
)

# Hand-placed Toronto-area points, well-separated. Each tuple is (lon, lat,
# DBF STATUS, DBF ADDRESS) — fixtures cover all three known tiers plus the
# address-collision case in TestHeritageAddressCollision below.
HERITAGE_FIXTURES = [
    (-79.3667, 43.6669, "Part IV", "17  SALISBURY AVE"),
    (-79.4000, 43.6500, "Part V",  "16 SOHO ST"),
    (-79.5000, 43.7000, "Listed",  "4 SOUTH KINGSWAY"),
]

# Minimal WGS84 .prj content — real Toronto registry uses GCS_WGS_1984.
_PRJ_BYTES = (
    b'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
    b'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    b'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)


def _build_heritage_zip(zip_path: Path, fixtures=HERITAGE_FIXTURES) -> None:
    """Write a SHP+SHX+DBF+PRJ bundle inside `zip_path` from `fixtures`.

    Each fixture is `(lon, lat, status, address)` and becomes one record.
    """
    shp_io = io.BytesIO()
    shx_io = io.BytesIO()
    dbf_io = io.BytesIO()

    w = shapefile.Writer(shp=shp_io, shx=shx_io, dbf=dbf_io)
    w.field("ADDRESS", "C", 64)
    w.field("STATUS", "C", 32)
    for lon, lat, status, address in fixtures:
        w.point(lon, lat)
        w.record(address, status)
    w.close()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("HRAP_test_OpenData.shp", shp_io.getvalue())
        z.writestr("HRAP_test_OpenData.shx", shx_io.getvalue())
        z.writestr("HRAP_test_OpenData.dbf", dbf_io.getvalue())
        z.writestr("HRAP_test_OpenData.prj", _PRJ_BYTES)


class HeritageZipReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.zip_path = self.tmpdir / CACHE_FILENAME
        _build_heritage_zip(self.zip_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_iter_records_yields_canonical_status_and_raw_address(self):
        records = list(_iter_records_from_zip(self.zip_path))
        self.assertEqual(len(records), len(HERITAGE_FIXTURES))
        for (pt, canonical, raw_addr), (lon, lat, dbf_status, dbf_addr) in zip(
            records, HERITAGE_FIXTURES,
        ):
            self.assertAlmostEqual(pt.x, lon, places=6)
            self.assertAlmostEqual(pt.y, lat, places=6)
            self.assertIn(canonical, KNOWN_STATUSES)
            self.assertEqual(raw_addr, dbf_addr)

    def test_compute_heritage_returns_heritage_index_with_per_tier_records(self):
        idx = compute_heritage(self.tmpdir)
        self.assertIsInstance(idx, HeritageIndex)
        self.assertEqual(len(idx.points), 3)
        self.assertEqual(len(idx.statuses), 3)
        self.assertEqual(len(idx.addresses), 3)
        self.assertEqual(set(idx.statuses), {STATUS_PART_IV, STATUS_PART_V, STATUS_LISTED})
        self.assertEqual(len(idx.point_tree.geometries), 3)

    def test_compute_heritage_normalizes_addresses(self):
        idx = compute_heritage(self.tmpdir)
        # `_build_heritage_zip` wrote `"17  SALISBURY AVE"` (double space);
        # normalize_address collapses runs and leaves AVE alone (already canonical).
        self.assertIn("17 SALISBURY AVE", idx.addresses)
        # `"16 SOHO ST"` is already canonical; should appear unchanged after upper.
        self.assertIn("16 SOHO ST", idx.addresses)

    def test_address_to_status_built_from_normalized_addresses(self):
        idx = compute_heritage(self.tmpdir)
        self.assertEqual(idx.address_to_status.get("17 SALISBURY AVE"), STATUS_PART_IV)
        self.assertEqual(idx.address_to_status.get("16 SOHO ST"), STATUS_PART_V)
        self.assertEqual(idx.address_to_status.get("4 SOUTH KINGSWAY"), STATUS_LISTED)

    def test_strtree_query_polygon_around_point_returns_match(self):
        idx = compute_heritage(self.tmpdir)
        # Bbox around the Cabbagetown (first) fixture point.
        bbox = Polygon([
            (-79.370, 43.666),
            (-79.363, 43.666),
            (-79.363, 43.668),
            (-79.370, 43.668),
            (-79.370, 43.666),
        ])
        hits = list(idx.point_tree.query(bbox))
        contained = [i for i in hits if bbox.contains(idx.points[i])]
        self.assertEqual(len(contained), 1)
        self.assertEqual(idx.statuses[contained[0]], STATUS_PART_IV)

    def test_strtree_query_in_lake_ontario_returns_no_contains_hits(self):
        idx = compute_heritage(self.tmpdir)
        lake = Polygon([
            (-79.450, 43.580),
            (-79.430, 43.580),
            (-79.430, 43.600),
            (-79.450, 43.600),
            (-79.450, 43.580),
        ])
        hits = list(idx.point_tree.query(lake))
        actual = [i for i in hits if lake.contains(idx.points[i])]
        self.assertEqual(actual, [])

    def test_strtree_query_polygon_covering_all_three_returns_all(self):
        idx = compute_heritage(self.tmpdir)
        wide = Polygon([
            (-79.510, 43.640),
            (-79.355, 43.640),
            (-79.355, 43.710),
            (-79.510, 43.710),
            (-79.510, 43.640),
        ])
        hits = list(idx.point_tree.query(wide))
        contained = [i for i in hits if wide.contains(idx.points[i])]
        self.assertEqual(sorted(contained), [0, 1, 2])


class HeritageUnknownStatusTests(unittest.TestCase):
    """Loud-failure: an unrecognized DBF STATUS raises ValueError."""

    def test_unknown_status_raises_value_error(self):
        with tempfile.TemporaryDirectory() as d:
            zip_path = Path(d) / CACHE_FILENAME
            _build_heritage_zip(zip_path, fixtures=[
                (-79.40, 43.65, "Part IV", "1 GOOD ST"),
                (-79.41, 43.65, "ProvincialMonument", "2 BAD ST"),  # unknown
            ])
            with self.assertRaises(ValueError) as ctx:
                list(_iter_records_from_zip(zip_path))
            msg = str(ctx.exception)
            self.assertIn("ProvincialMonument", msg)
            self.assertIn("Part IV", msg)  # the message lists the known set


class HeritageAddressCollisionTests(unittest.TestCase):
    """Most-restrictive precedence: when two records share a normalized address
    but differ in tier, `address_to_status` resolves to the strictest tier."""

    def test_part_iv_wins_over_part_v_on_same_normalized_address(self):
        with tempfile.TemporaryDirectory() as d:
            zip_path = Path(d) / CACHE_FILENAME
            # Two records sharing the same ADDRESS string but different STATUS.
            # Order matters for the test: the Part V record arrives first, then
            # Part IV; we verify the dict still resolves to Part IV.
            _build_heritage_zip(zip_path, fixtures=[
                (-79.40, 43.65, "Part V",  "100 KING ST W"),
                (-79.41, 43.66, "Part IV", "100 KING ST W"),
            ])
            idx = compute_heritage(Path(d))
            self.assertEqual(idx.address_to_status["100 KING ST W"], STATUS_PART_IV)


class HeritageMissingShpTests(unittest.TestCase):
    def test_zip_without_shp_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as d:
            zip_path = Path(d) / "broken.zip"
            with zipfile.ZipFile(zip_path, "w") as z:
                z.writestr("readme.txt", "no shapefile here")
            with self.assertRaisesRegex(RuntimeError, "no .shp file"):
                list(_iter_records_from_zip(zip_path))


class NormalizeAddressTests(unittest.TestCase):
    """Lock the address-join normalizer's contract.

    Per Req 3.6: at least 6 fixture pairs covering case differences,
    abbreviations, and whitespace variations. We exceed that here with 10
    fixture pairs, including: blank input, numeric-only token, multi-token
    street name, and an unmapped street type that passes through.
    """

    def test_uppercases_input(self):
        self.assertEqual(normalize_address("Salisbury Ave"), "SALISBURY AVE")

    def test_collapses_runs_of_whitespace(self):
        self.assertEqual(
            normalize_address("17  SALISBURY  AVE"), "17 SALISBURY AVE",
        )
        self.assertEqual(
            normalize_address("  100   QUEEN  ST  "), "100 QUEEN ST",
        )

    def test_abbreviates_street_to_st(self):
        self.assertEqual(normalize_address("16 SOHO STREET"), "16 SOHO ST")

    def test_abbreviates_avenue_to_ave(self):
        self.assertEqual(normalize_address("17 SALISBURY AVENUE"), "17 SALISBURY AVE")

    def test_abbreviates_crescent_to_cres(self):
        self.assertEqual(normalize_address("1 CEDAR CRESCENT"), "1 CEDAR CRES")

    def test_abbreviates_boulevard_to_blvd(self):
        self.assertEqual(normalize_address("99 LAKE SHORE BOULEVARD"), "99 LAKE SHORE BLVD")

    def test_blank_input_returns_empty(self):
        self.assertEqual(normalize_address(""), "")
        self.assertEqual(normalize_address(None), "")

    def test_numeric_only_token_passes_through(self):
        self.assertEqual(normalize_address("17"), "17")

    def test_multi_token_street_name(self):
        # "WEST QUEEN WEST" is a real Toronto neighborhood; the trailing ST
        # should still abbreviate, but the multi-word name passes through.
        self.assertEqual(
            normalize_address("100 West Queen West Street"),
            "100 WEST QUEEN WEST ST",
        )

    def test_unmapped_suffix_passes_through_uppercased(self):
        # `MEWS` is a Toronto street suffix not in our closed-set; it should
        # pass through after uppercasing rather than crashing or being dropped.
        self.assertEqual(normalize_address("5 PartyMews Mews"), "5 PARTYMEWS MEWS")


class MoreRestrictiveTests(unittest.TestCase):
    """Lock the precedence helper's full Cartesian product.

    Per Req 3.5: 4 inputs (None, listed, part_v, part_iv) × 4 inputs = 16
    ordered pairs. Precedence: Part IV > Part V > Listed > None.
    """

    EXPECTED = {
        (None, None): None,
        (None, STATUS_LISTED): STATUS_LISTED,
        (None, STATUS_PART_V): STATUS_PART_V,
        (None, STATUS_PART_IV): STATUS_PART_IV,
        (STATUS_LISTED, None): STATUS_LISTED,
        (STATUS_LISTED, STATUS_LISTED): STATUS_LISTED,
        (STATUS_LISTED, STATUS_PART_V): STATUS_PART_V,
        (STATUS_LISTED, STATUS_PART_IV): STATUS_PART_IV,
        (STATUS_PART_V, None): STATUS_PART_V,
        (STATUS_PART_V, STATUS_LISTED): STATUS_PART_V,
        (STATUS_PART_V, STATUS_PART_V): STATUS_PART_V,
        (STATUS_PART_V, STATUS_PART_IV): STATUS_PART_IV,
        (STATUS_PART_IV, None): STATUS_PART_IV,
        (STATUS_PART_IV, STATUS_LISTED): STATUS_PART_IV,
        (STATUS_PART_IV, STATUS_PART_V): STATUS_PART_IV,
        (STATUS_PART_IV, STATUS_PART_IV): STATUS_PART_IV,
    }

    def test_all_sixteen_ordered_pairs(self):
        for (a, b), expected in self.EXPECTED.items():
            with self.subTest(a=a, b=b):
                self.assertEqual(more_restrictive(a, b), expected)


class StatusConstantsTests(unittest.TestCase):
    def test_known_statuses_set(self):
        self.assertEqual(KNOWN_STATUSES, {STATUS_PART_IV, STATUS_PART_V, STATUS_LISTED})

    def test_canonical_wire_values(self):
        # These strings are the wire format consumed by the frontend; locking
        # them prevents accidental rename.
        self.assertEqual(STATUS_PART_IV, "part_iv")
        self.assertEqual(STATUS_PART_V, "part_v")
        self.assertEqual(STATUS_LISTED, "listed")


if __name__ == "__main__":
    unittest.main()
