import unittest

from tools.parcel_scoring import (
    BLOOM_FORMULA_TEXT,
    BLOOM_MAX_TREES,
    BLOOM_MIN_LOT_M2,
    BLOOM_TRANSIT_M,
    FORMULA_TEXT,
    LISTED_HERITAGE_FACTOR,
    MIN_BUILDABLE_AREA_M2,
    MULTIPLEX_FLOOR,
    PART_V_HERITAGE_FACTOR,
    TRANSIT_BUFFER_M,
    bloom_flag,
    score,
)


def _bloom_kwargs(**overrides):
    """Default kwargs: a parcel that passes every Bloom gate. Tests override
    the field they want to flip negative."""
    base = dict(
        heritage_status=None,
        dist_subway_streetcar_m=200.0,
        lot_area_m2=750.0,
        sixplex_eligible=True,
        mature_tree_count=0,
        in_regulated_area=False,
    )
    base.update(overrides)
    return base


class ScoreEligibilityTests(unittest.TestCase):
    """Any of residential, Part-IV-heritage, or transit_factor == 0 → score 0."""

    def test_non_residential_zeros(self):
        self.assertEqual(
            score(residential=False, heritage_status=None, dist_m=0, max_units=4),
            0,
        )

    def test_part_iv_zeros(self):
        # Part IV is a hard block; heritage_factor = 0 short-circuits to 0.
        self.assertEqual(
            score(residential=True, heritage_status="part_iv", dist_m=0, max_units=4),
            0,
        )

    def test_no_transit_distance_zeros(self):
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=None, max_units=4),
            0,
        )

    def test_transit_at_boundary_zeros(self):
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=500, max_units=4),
            0,
        )

    def test_transit_beyond_buffer_zeros(self):
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=750, max_units=4),
            0,
        )


class ScoreFormulaTests(unittest.TestCase):
    def test_null_heritage_max_eligible_returns_100(self):
        # All factors at 1.0; expect a perfect score.
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=0, max_units=4),
            100,
        )

    def test_part_v_with_perfect_other_inputs(self):
        # heritage_factor = 0.5 → 100 * 0.5 * 1.0 * 1.0 = 50.
        self.assertEqual(
            score(residential=True, heritage_status="part_v", dist_m=0, max_units=4),
            50,
        )

    def test_listed_with_perfect_other_inputs(self):
        # heritage_factor = 0.85 → 100 * 0.85 * 1.0 * 1.0 = 85.
        self.assertEqual(
            score(residential=True, heritage_status="listed", dist_m=0, max_units=4),
            85,
        )

    def test_mid_range_hand_computed(self):
        # null heritage; transit_factor=0.5; mult=1.0; → 50.
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=250, max_units=4),
            50,
        )

    def test_part_v_with_partial_transit_decay(self):
        # heritage 0.5; transit 0.5; mult 1.0 → 100 * 0.5 * 0.5 * 1.0 = 25.
        self.assertEqual(
            score(residential=True, heritage_status="part_v", dist_m=250, max_units=4),
            25,
        )

    def test_listed_with_partial_transit_decay(self):
        # heritage 0.85; transit 0.6 (dist=200); mult 1.0 → 100 * 0.85 * 0.6 = 51.
        self.assertEqual(
            score(residential=True, heritage_status="listed", dist_m=200, max_units=4),
            51,
        )

    def test_low_unit_cap_scales_score(self):
        # null heritage; max_units=2 → mult=0.5; transit=1.0 → 50.
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=0, max_units=2),
            50,
        )

    def test_high_unit_cap_clamps_to_one(self):
        # multiplier_factor must clamp; score ≤ 100.
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=0, max_units=8),
            100,
        )

    def test_combined_decay(self):
        # null heritage; dist=400 → transit=0.2; mult=1.0 → 20.
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=400, max_units=4),
            20,
        )

    def test_unknown_heritage_status_raises_keyerror(self):
        # Loud-failure: any string outside _HERITAGE_FACTORS keys must raise.
        with self.assertRaises(KeyError):
            score(residential=True, heritage_status="part_xi", dist_m=0, max_units=4)


class ScoreSliverGateTests(unittest.TestCase):
    """`area_m2 < MIN_BUILDABLE_AREA_M2` zeros otherwise-perfect scores."""

    def test_area_below_floor_zeros(self):
        # 10 m² sliver, otherwise-perfect inputs → still 0.
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=0, max_units=4, area_m2=10),
            0,
        )

    def test_area_at_floor_passes(self):
        # The floor is inclusive; a 100 m² lot is the smallest accepted.
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=0, max_units=4, area_m2=MIN_BUILDABLE_AREA_M2),
            100,
        )

    def test_area_none_skips_gate(self):
        # Default-None preserves the legacy 4-arg signature: callers that
        # don't pass area_m2 keep their old behavior.
        self.assertEqual(
            score(residential=True, heritage_status=None, dist_m=0, max_units=4),
            100,
        )


class BloomFlagTests(unittest.TestCase):
    """Bloom requires every multiplex-friction-clear gate to pass."""

    def test_all_gates_pass_returns_true(self):
        self.assertTrue(bloom_flag(**_bloom_kwargs()))

    def test_part_iv_blocks_bloom(self):
        self.assertFalse(bloom_flag(**_bloom_kwargs(heritage_status="part_iv")))

    def test_part_v_blocks_bloom(self):
        self.assertFalse(bloom_flag(**_bloom_kwargs(heritage_status="part_v")))

    def test_listed_blocks_bloom(self):
        self.assertFalse(bloom_flag(**_bloom_kwargs(heritage_status="listed")))

    def test_dist_none_blocks_bloom(self):
        self.assertFalse(bloom_flag(**_bloom_kwargs(dist_subway_streetcar_m=None)))

    def test_dist_at_or_above_threshold_blocks_bloom(self):
        self.assertFalse(bloom_flag(**_bloom_kwargs(dist_subway_streetcar_m=BLOOM_TRANSIT_M)))
        self.assertFalse(bloom_flag(**_bloom_kwargs(dist_subway_streetcar_m=BLOOM_TRANSIT_M + 1)))

    def test_dist_just_inside_threshold_passes(self):
        self.assertTrue(bloom_flag(**_bloom_kwargs(dist_subway_streetcar_m=BLOOM_TRANSIT_M - 1)))

    def test_lot_below_min_blocks_bloom(self):
        self.assertFalse(bloom_flag(**_bloom_kwargs(lot_area_m2=BLOOM_MIN_LOT_M2 - 1)))

    def test_lot_at_min_passes(self):
        self.assertTrue(bloom_flag(**_bloom_kwargs(lot_area_m2=BLOOM_MIN_LOT_M2)))

    def test_lot_none_blocks_bloom(self):
        self.assertFalse(bloom_flag(**_bloom_kwargs(lot_area_m2=None)))

    def test_not_sixplex_eligible_blocks_bloom(self):
        self.assertFalse(bloom_flag(**_bloom_kwargs(sixplex_eligible=False)))

    def test_protected_tree_blocks_bloom(self):
        self.assertFalse(bloom_flag(**_bloom_kwargs(mature_tree_count=BLOOM_MAX_TREES + 1)))

    def test_trca_regulated_blocks_bloom(self):
        self.assertFalse(bloom_flag(**_bloom_kwargs(in_regulated_area=True)))


class FormulaTextTests(unittest.TestCase):
    """Wire-format invariants — these strings are surfaced in `meta.scoreFormula`."""

    def test_formula_text_matches_design(self):
        # Per Req 2.5, the formula text must mention the four multiplicands and
        # name `heritage_factor` (replacing the old `not_heritage`) so a frontend
        # showing `meta.scoreFormula` is self-explanatory.
        self.assertIn("residential", FORMULA_TEXT)
        self.assertIn("heritage_factor", FORMULA_TEXT)
        self.assertNotIn("not_heritage", FORMULA_TEXT)
        self.assertIn("transit_factor", FORMULA_TEXT)
        self.assertIn("multiplier_factor", FORMULA_TEXT)
        self.assertIn("max(0, 1 − dist_m / 500)", FORMULA_TEXT)
        self.assertIn("min(1.0, max_units_per_lot / 4)", FORMULA_TEXT)
        # Sliver gate must be advertised so consumers of `meta.scoreFormula`
        # can explain why a sub-100 m² polygon scored 0.
        self.assertIn("lot_area_m2", FORMULA_TEXT)
        self.assertIn("100", FORMULA_TEXT)

    def test_bloom_formula_text_describes_six_gates(self):
        # 2026-05-06: reframed from "net-zero premium" (solar+subway) to
        # "premium multiplex-friction-clear" (heritage / transit / lot /
        # sixplex / trees / TRCA). The formula text must mention every
        # gate so consumers of `meta.bloomFormula` can explain why a
        # parcel did or didn't pass.
        self.assertIn("heritageStatus is null", BLOOM_FORMULA_TEXT)
        self.assertIn("distSubwayStreetcarM < 500", BLOOM_FORMULA_TEXT)
        self.assertIn("lotAreaM2 >= 600", BLOOM_FORMULA_TEXT)
        self.assertIn("sixplexEligible == True", BLOOM_FORMULA_TEXT)
        self.assertIn("matureTreeCount == 0", BLOOM_FORMULA_TEXT)
        self.assertIn("inRegulatedArea == False", BLOOM_FORMULA_TEXT)


class ConstantsTests(unittest.TestCase):
    def test_constant_values(self):
        self.assertEqual(TRANSIT_BUFFER_M, 500)
        self.assertEqual(MULTIPLEX_FLOOR, 4)
        self.assertEqual(BLOOM_TRANSIT_M, 500)
        self.assertEqual(BLOOM_MIN_LOT_M2, 600)
        self.assertEqual(BLOOM_MAX_TREES, 0)
        self.assertEqual(PART_V_HERITAGE_FACTOR, 0.5)
        self.assertEqual(LISTED_HERITAGE_FACTOR, 0.85)
        self.assertEqual(MIN_BUILDABLE_AREA_M2, 100)


if __name__ == "__main__":
    unittest.main()
