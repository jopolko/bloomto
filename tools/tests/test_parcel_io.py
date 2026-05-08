import json
import tempfile
import unittest
from pathlib import Path

from tools.parcel_io import (
    FEATURE_PROPERTIES,
    HERITAGE_STATUSES,
    LEGACY_FEATURE_KEY,
    LEGACY_STATS_KEY,
    META_KEYS,
    REQUIRED_STATS_KEYS,
    SOLAR_SHADOW_QUALITIES,
    validate,
    write_atomic,
)


def _make_props(*, quality: str = "measured", solar_score=80,
                heritage_status=None, **overrides):
    base = {
        "parcelId": "TEST-1",
        "address": "123 Test St",
        "zoneClass": "RD",
        "maxUnits": 4,
        "maxUnitsRationale": "zone_average",
        "zoneString": "RD (f6.0; a200)",
        "zoneFsi": None,
        "zoneMinLotFrontageM": 6.0,
        "zoneMinLotAreaM2": 200,
        "residential": True,
        "heritageStatus": heritage_status,
        "distSubwayStreetcarM": 200,
        "distSubwayM": 600,
        "distStreetcarM": 200,
        "distBusM": 100,
        "neighborhood": "Test Neighborhood",
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
        "lotAreaM2": 500,
        "lotAspectRatio": 1.6,
        "buildingCoverageRatio": 0.35,
        "solarScoreRaw": 90,
        "solarScore": solar_score,
        "solarShadowQuality": quality,
        "postwarNeighborhood": False,
        # 2026-05-05 architect / dev panel additions.
        "lotGeometry": {"longAxisM": 18.5, "shortAxisM": 6.2, "orientationDeg": 75.0},
        "neighborHeights": {"nAvgM": 6.5, "sAvgM": 9.0, "eAvgM": None, "wAvgM": 7.2},
        "existingMaxBuildingHeightM": 7.5,
        "existingStructureType": "detached",
        "existingStructureSource": "classifier",
        "solarYieldKwhPerYr": 22000,
        "pvCapacityKwEstimate": 19.1,
        "sixplexBonusValueCad": None,
    }
    base.update(overrides)
    return base


def _make_feature(**props_overrides):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-79.4, 43.7]},
        "properties": _make_props(**props_overrides),
    }


def _make_stats():
    """Stats block satisfying REQUIRED_STATS_KEYS for the validator."""
    return {
        "totalParcels": 1,
        "heritagePartIV": 0,
        "heritagePartV": 0,
        "heritageListed": 0,
        "heritageUnjoined": 0,
        "residential": 1,
        "cornerLot": 0,
        "postwar": 0,
        "skippedNoNeighborhood": 0,
        "skippedNonBuildable": 0,
        "skippedInstitutional": 0,
        "skippedInstitutionalByCategory": {},
        "skippedOsmLanduse": 0,
        "skippedTaxExempt": 0,
        "skippedTallExistingBuilding": 0,
        "abutsLaneway": 0,
        "nearRapidToCorridor": 0,
        "inFloodingStudyArea": 0,
        "inRegulatedArea": 0,
        "matureTrees": 0,
        "sixplexEligible": 0,
        "existingStructureType": {"detached": 1, "semi": 0, "row": 0, "vacant": 0, "unknown": 0},
    }


def _make_payload(features=None):
    return {
        "type": "FeatureCollection",
        "meta": {
            "generatedAt": "2026-05-01T00:00:00Z",
            "sourceVersions": {},
            "solarMethodology": "test",
            "shadowAnalysis": {},
            "permits": {
                "totalPermitsKept": 0,
                "joinedByAddress": 0,
                "joinedBySpatialFallback": 0,
                "unjoined": 0,
                "freshnessYears": 5,
                "sanityCeilingCad": 50_000_000,
                "minNeighborhoodSampleSize": 10,
                "denominatorLabel": "declared_construction_cost_cad",
                "denominatorPerUnit": True,
                "notes": "test",
            },
            "stats": _make_stats(),
        },
        "features": features if features is not None else [_make_feature()],
    }


class ValidateTopLevelTests(unittest.TestCase):
    def test_accepts_valid_payload(self):
        validate(_make_payload())

    def test_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            validate([])

    def test_rejects_missing_type(self):
        p = _make_payload()
        del p["type"]
        with self.assertRaisesRegex(ValueError, "type"):
            validate(p)

    def test_rejects_wrong_type(self):
        p = _make_payload()
        p["type"] = "Feature"
        with self.assertRaisesRegex(ValueError, "FeatureCollection"):
            validate(p)

    def test_rejects_missing_meta(self):
        p = _make_payload()
        del p["meta"]
        with self.assertRaisesRegex(ValueError, "meta"):
            validate(p)

    def test_rejects_missing_each_meta_key(self):
        for key in META_KEYS:
            with self.subTest(key=key):
                p = _make_payload()
                del p["meta"][key]
                with self.assertRaisesRegex(ValueError, key):
                    validate(p)

    def test_rejects_empty_features(self):
        with self.assertRaisesRegex(ValueError, "non-empty"):
            validate(_make_payload(features=[]))

    def test_rejects_features_not_list(self):
        p = _make_payload()
        p["features"] = "not a list"
        with self.assertRaisesRegex(ValueError, "list"):
            validate(p)


class ValidateFeatureTests(unittest.TestCase):
    def test_rejects_feature_not_dict(self):
        with self.assertRaisesRegex(ValueError, r"features\[0\]"):
            validate(_make_payload(features=["bad"]))

    def test_rejects_wrong_feature_type(self):
        feat = _make_feature()
        feat["type"] = "FeatureCollection"
        with self.assertRaisesRegex(ValueError, "Feature"):
            validate(_make_payload(features=[feat]))

    def test_rejects_non_point_geometry(self):
        feat = _make_feature()
        feat["geometry"]["type"] = "Polygon"
        with self.assertRaisesRegex(ValueError, "Point"):
            validate(_make_payload(features=[feat]))

    def test_rejects_missing_each_property(self):
        for key in FEATURE_PROPERTIES:
            with self.subTest(key=key):
                feat = _make_feature()
                del feat["properties"][key]
                with self.assertRaisesRegex(ValueError, key):
                    validate(_make_payload(features=[feat]))


class ValidateSolarInvariantTests(unittest.TestCase):
    """The accuracy-over-completeness invariant:
    solarScore is None ↔ solarShadowQuality == 'unavailable'."""

    def test_unavailable_quality_with_null_score_ok(self):
        validate(_make_payload(features=[
            _make_feature(quality="unavailable", solar_score=None)
        ]))

    def test_measured_quality_with_score_ok(self):
        validate(_make_payload(features=[
            _make_feature(quality="measured", solar_score=85)
        ]))

    def test_estimated_quality_with_score_ok(self):
        validate(_make_payload(features=[
            _make_feature(quality="estimated", solar_score=70)
        ]))

    def test_rejects_measured_with_null_score(self):
        with self.assertRaisesRegex(ValueError, "solarScore"):
            validate(_make_payload(features=[
                _make_feature(quality="measured", solar_score=None)
            ]))

    def test_rejects_unavailable_with_score(self):
        with self.assertRaisesRegex(ValueError, "solarScore"):
            validate(_make_payload(features=[
                _make_feature(quality="unavailable", solar_score=50)
            ]))

    def test_rejects_unknown_quality_value(self):
        with self.assertRaisesRegex(ValueError, "solarShadowQuality"):
            validate(_make_payload(features=[
                _make_feature(quality="approximate", solar_score=50)
            ]))


class ValidateHeritageStatusTests(unittest.TestCase):
    """The four-value heritageStatus enum + legacy-key rejection."""

    def test_accepts_each_legal_status(self):
        for status in (None, "part_iv", "part_v", "listed"):
            with self.subTest(status=status):
                validate(_make_payload(features=[
                    _make_feature(heritage_status=status)
                ]))

    def test_rejects_string_outside_known_set(self):
        with self.assertRaisesRegex(ValueError, "heritageStatus"):
            validate(_make_payload(features=[
                _make_feature(heritage_status="part_xi")
            ]))

    def test_rejects_legacy_heritage_key_in_feature(self):
        feat = _make_feature()
        feat["properties"][LEGACY_FEATURE_KEY] = False
        with self.assertRaisesRegex(ValueError, LEGACY_FEATURE_KEY):
            validate(_make_payload(features=[feat]))


class ValidateStatsTests(unittest.TestCase):
    """meta.stats per-tier counts replace the legacy heritageFlagged integer."""

    def test_rejects_missing_each_required_stats_key(self):
        for key in REQUIRED_STATS_KEYS:
            with self.subTest(key=key):
                p = _make_payload()
                del p["meta"]["stats"][key]
                with self.assertRaisesRegex(ValueError, key):
                    validate(p)

    def test_rejects_legacy_heritage_flagged_key(self):
        p = _make_payload()
        p["meta"]["stats"][LEGACY_STATS_KEY] = 5
        with self.assertRaisesRegex(ValueError, LEGACY_STATS_KEY):
            validate(p)

    def test_rejects_non_dict_stats(self):
        p = _make_payload()
        p["meta"]["stats"] = "not a dict"
        with self.assertRaisesRegex(ValueError, "stats"):
            validate(p)


class WriteAtomicTests(unittest.TestCase):
    def test_writes_valid_payload(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "data" / "parcels.geojson"
            write_atomic(_make_payload(), out)
            self.assertTrue(out.exists())
            data = json.loads(out.read_text())
            self.assertEqual(data["type"], "FeatureCollection")
            self.assertEqual(len(data["features"]), 1)

    def test_invalid_payload_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "data" / "parcels.geojson"
            with self.assertRaises(ValueError):
                write_atomic(_make_payload(features=[]), out)
            self.assertFalse(out.exists())
            self.assertEqual(list(out.parent.glob("*.tmp")), [])

    def test_invalid_payload_does_not_overwrite_preexisting(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "parcels.geojson"
            original = '{"original": true}\n'
            out.write_text(original)
            with self.assertRaises(ValueError):
                # Invalid: missing 'measured' score
                bad = _make_payload(features=[
                    _make_feature(quality="measured", solar_score=None)
                ])
                write_atomic(bad, out)
            self.assertEqual(out.read_text(), original)

    def test_output_mode_is_0664(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "parcels.geojson"
            write_atomic(_make_payload(), out)
            self.assertEqual(out.stat().st_mode & 0o777, 0o664)

    def test_output_has_trailing_newline(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "parcels.geojson"
            write_atomic(_make_payload(), out)
            self.assertTrue(out.read_text().endswith("\n"))

    def test_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "deeply" / "nested" / "parcels.geojson"
            write_atomic(_make_payload(), out)
            self.assertTrue(out.parent.is_dir())


if __name__ == "__main__":
    unittest.main()
