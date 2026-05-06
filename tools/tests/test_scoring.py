import unittest

from tools.scoring import (
    BIKE_WEIGHT,
    CANOPY_SCALE,
    CANOPY_WEIGHT,
    DEFAULT_WEIGHTS,
    ENERGY_WEIGHT,
    FORMULA_TEXT,
    MM_SCALE,
    MM_WEIGHT,
    TRANSIT_WEIGHT,
    WALK_WEIGHT,
    score,
)


def s(**overrides):
    base = dict(
        heat_pump=0, canopy_pct=0, walk=0, transit=0, bike=0,
        existing=1, potential=1,
    )
    base.update(overrides)
    return score(**base)


class ScoreTests(unittest.TestCase):
    def test_all_zero_inputs_give_zero(self):
        self.assertEqual(s(), 0)

    def test_all_max_inputs_clamp_to_100(self):
        # heatPump=100, canopy=50 (×2 = 100), walk/transit/bike = 100,
        # potential = 3×existing → MM headroom = 100. Each term hits ceiling.
        self.assertEqual(
            s(heat_pump=100, canopy_pct=50, walk=100, transit=100, bike=100,
              existing=1, potential=3),
            100,
        )

    def test_realistic_mid_range(self):
        # 0.30·70 + 0.20·min(100,40) + 0.15·80 + 0.15·85 + 0.10·70 + 0.10·30
        #  = 21 + 8 + 12 + 12.75 + 7 + 3 = 63.75 → 64
        self.assertEqual(
            s(heat_pump=70, canopy_pct=20, walk=80, transit=85, bike=70,
              existing=5000, potential=8000),
            64,
        )

    def test_canopy_50pct_hits_ceiling(self):
        # canopy=50 × scale 2 = 100 (no clamp); 0.20 × 100 = 20
        self.assertEqual(s(canopy_pct=50), 20)

    def test_canopy_clamps_above_50pct(self):
        # canopy=80 × 2 = 160 → clamp to 100; same contribution as 50%
        self.assertEqual(s(canopy_pct=80), 20)

    def test_mm_headroom_zero(self):
        # potential == existing → ratio 1.0 → headroom 0
        self.assertEqual(s(existing=1000, potential=1000), 0)

    def test_mm_headroom_50(self):
        # potential = 2× existing → ratio 2 → headroom 50 → 0.10 × 50 = 5
        self.assertEqual(s(existing=1000, potential=2000), 5)

    def test_mm_headroom_clamps_at_100(self):
        # potential ≥ 3× existing → headroom clamped to 100 → 0.10 × 100 = 10
        self.assertEqual(s(existing=1000, potential=3000), 10)
        self.assertEqual(s(existing=1000, potential=10000), 10)

    def test_mm_headroom_negative_clamps_to_zero(self):
        # potential < existing should not contribute negatively
        self.assertEqual(s(existing=1000, potential=500), 0)

    def test_existing_zero_does_not_divide_by_zero(self):
        # max(existing, 1) guards against ZeroDivisionError; headroom maxes
        self.assertEqual(s(existing=0, potential=1000), 10)

    def test_weight_override_isolated_to_energy(self):
        self.assertEqual(
            s(heat_pump=100,
              weights={"energy": 1.0, "canopy": 0, "walk": 0, "transit": 0,
                       "bike": 0, "mm": 0}),
            100,
        )

    def test_partial_weight_override_falls_back_to_defaults(self):
        # Override only energy weight; other weights stay at module defaults.
        # 0.50·100 + 0.20·0 + 0.15·0 + 0.15·0 + 0.10·0 + 0.10·0 = 50
        self.assertEqual(
            s(heat_pump=100, weights={"energy": 0.5}),
            50,
        )

    def test_constants(self):
        self.assertEqual(ENERGY_WEIGHT, 0.30)
        self.assertEqual(CANOPY_WEIGHT, 0.20)
        self.assertEqual(WALK_WEIGHT, 0.15)
        self.assertEqual(TRANSIT_WEIGHT, 0.15)
        self.assertEqual(BIKE_WEIGHT, 0.10)
        self.assertEqual(MM_WEIGHT, 0.10)
        self.assertEqual(CANOPY_SCALE, 2)
        self.assertEqual(MM_SCALE, 50)

    def test_default_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(DEFAULT_WEIGHTS.values()), 1.0)

    def test_formula_text_canonical_wording(self):
        expected = (
            "score = round("
            "0.30 × heatPump "
            "+ 0.20 × min(100, canopy × 2) "
            "+ 0.15 × walk "
            "+ 0.15 × transit "
            "+ 0.10 × bike "
            "+ 0.10 × min(100, max(0, (potential / max(existing, 1) - 1) × 50)))"
        )
        self.assertEqual(FORMULA_TEXT, expected)

    def test_negative_inputs_floor_to_zero(self):
        self.assertEqual(
            s(heat_pump=-50, canopy_pct=-10, walk=-100, transit=-100, bike=-100),
            0,
        )

    def test_keyword_only_signature(self):
        # Positional arg should fail since score() is keyword-only
        with self.assertRaises(TypeError):
            score(50, 25, 80, 80, 80, 1, 1)


if __name__ == "__main__":
    unittest.main()
