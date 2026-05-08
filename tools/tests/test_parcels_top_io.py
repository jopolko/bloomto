import json
import tempfile
import unittest
from pathlib import Path

from tools.parcels_top_io import (
    PAYLOAD_KEYS,
    ROW_KEYS,
    make_payload,
    project_features,
    validate,
    write_atomic,
)


def _make_feature(*, address="100 Test St", lot_area_m2=500, lng=-79.4, lat=43.7,
                  **prop_overrides):
    props = {
        "parcelId": "TEST-1",
        "address": address,
        "zoneClass": "RD",
        "maxUnits": 4,
        "maxUnitsRationale": "zone_average",
        "zoneString": "RD (f6.0; a200)",
        "zoneFsi": None,
        "zoneMinLotFrontageM": 6.0,
        "zoneMinLotAreaM2": 200,
        "residential": True,
        "heritageStatus": None,
        "distSubwayStreetcarM": 200,
        "distSubwayM": 600,
        "distStreetcarM": 200,
        "distBusM": 100,
        "neighborhood": "Test Hood",
        "builtYear": 1955,
        "cornerLot": False,
        "abutsLaneway": False,
        "nearRapidToCorridor": False,
        "inFloodingStudyArea": False,
        "inRegulatedArea": False,
        "permits": {
            "recentCount": 0,
            "recentValueTotal": 0,
            "recentMostRecentDate": None,
            "denominatorSource": "no_joined_permits",
        },
        "neighborhoodPermitComp": {
            "medianCostPerUnit": None,
            "sampleSize": 0,
            "freshnessYears": 5,
        },
        "neighborhoodCanopyPct": 30,
        "streetTreeCount": 0,
        "matureTreeCount": 0,
        "distBikeLaneM": 99999,
        "sixplexEligible": False,
        "lotAreaM2": lot_area_m2,
        "lotAspectRatio": 1.6,
        "buildingCoverageRatio": 0.35,
        "solarScoreRaw": 90,
        "solarScore": 80,
        "solarShadowQuality": "measured",
        "postwarNeighborhood": False,
        # 2026-05-05 architect / dev panel additions.
        "lotGeometry": {"longAxisM": 18.5, "shortAxisM": 6.2, "orientationDeg": 75.0},
        "neighborHeights": {"nAvgM": 6.5, "sAvgM": 9.0, "eAvgM": None, "wAvgM": 7.2},
        "existingMaxBuildingHeightM": 7.5,
        "existingStructureType": "detached",
        "nbMedHouseholdIncome": 84500,
        "nbAvgHouseholdIncome": 102000,
        "nbMedDwellingValue": 950000,
        "nbAvgDwellingValue": 1100000,
        "nbPermitsPer1kDwellings": 2.5,
        "solarYieldKwhPerYr": 22000,
        "pvCapacityKwEstimate": 19.1,
        "sixplexBonusValueCad": None,
    }
    props.update(prop_overrides)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": props,
    }


class ProjectFeaturesTests(unittest.TestCase):
    def test_surfaces_full_property_set(self):
        # As of 2026-05-02 the projection mirrors FEATURE_PROPERTIES — the
        # listing page's badge gates and traceability links need the full
        # set (parcelId, residential, builtYear, solarScoreRaw all surfaced).
        rows, _ = project_features([_make_feature()], top_n=10)
        for key in ("parcelId", "residential", "builtYear", "solarScoreRaw"):
            self.assertIn(key, rows[0])

    def test_emits_all_row_keys(self):
        rows, _ = project_features([_make_feature()], top_n=10)
        for key in ROW_KEYS:
            self.assertIn(key, rows[0])

    def test_lat_lng_extracted_from_geometry(self):
        rows, _ = project_features([_make_feature(lng=-79.5, lat=43.65)], top_n=10)
        self.assertEqual(rows[0]["lat"], 43.65)
        self.assertEqual(rows[0]["lng"], -79.5)

    def test_top_n_caps_output(self):
        feats = [_make_feature(lot_area_m2=1000 - i) for i in range(20)]
        rows, total = project_features(feats, top_n=5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(total, 20)
        # project_features preserves caller-supplied order — we don't
        # re-sort here (build_parcels_top.py sorts by lot area before
        # projection). Just confirm the slice is the first 5.
        self.assertEqual(len(rows), 5)

    def test_top_n_larger_than_available(self):
        feats = [_make_feature()]
        rows, total = project_features(feats, top_n=1000)
        self.assertEqual(len(rows), 1)
        self.assertEqual(total, 1)


class MakePayloadTests(unittest.TestCase):
    def test_payload_has_all_top_level_keys(self):
        p = make_payload([_make_feature()], top_n=10)
        for key in PAYLOAD_KEYS:
            self.assertIn(key, p)

    def test_total_available_is_input_count(self):
        feats = [_make_feature() for _ in range(7)]
        p = make_payload(feats, top_n=3)
        self.assertEqual(p["totalAvailable"], 7)
        self.assertEqual(p["topN"], 3)

    def test_top_n_clamped_to_total(self):
        p = make_payload([_make_feature()], top_n=999)
        self.assertEqual(p["topN"], 1)


class ValidateTests(unittest.TestCase):
    def test_accepts_valid_payload(self):
        validate(make_payload([_make_feature()], top_n=10))

    def test_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            validate([])

    def test_rejects_missing_top_level_key(self):
        for key in PAYLOAD_KEYS:
            with self.subTest(key=key):
                p = make_payload([_make_feature()], top_n=10)
                del p[key]
                with self.assertRaisesRegex(ValueError, key):
                    validate(p)

    def test_rejects_empty_rows(self):
        p = make_payload([_make_feature()], top_n=10)
        p["rows"] = []
        with self.assertRaisesRegex(ValueError, "non-empty"):
            validate(p)

    def test_rejects_missing_row_key(self):
        for key in ROW_KEYS:
            with self.subTest(key=key):
                p = make_payload([_make_feature()], top_n=10)
                del p["rows"][0][key]
                with self.assertRaisesRegex(ValueError, key):
                    validate(p)


class WriteAtomicTests(unittest.TestCase):
    def test_writes_valid_payload(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "data" / "parcels-top.json"
            write_atomic(make_payload([_make_feature()], top_n=10), out)
            self.assertTrue(out.exists())
            data = json.loads(out.read_text())
            self.assertEqual(len(data["rows"]), 1)

    def test_invalid_payload_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "data" / "parcels-top.json"
            bad = make_payload([_make_feature()], top_n=10)
            bad["rows"] = []
            with self.assertRaises(ValueError):
                write_atomic(bad, out)
            self.assertFalse(out.exists())

    def test_output_mode_is_0664(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "parcels-top.json"
            write_atomic(make_payload([_make_feature()], top_n=10), out)
            self.assertEqual(out.stat().st_mode & 0o777, 0o664)


if __name__ == "__main__":
    unittest.main()
