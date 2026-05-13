"""Coverage regression for `tools/zoning_multipliers.json` vs. the live zoning data.

When a future Toronto by-law amendment introduces a new ZN_ZONE code that the
multiplier table does not list, `lookup_multiplier` raises `KeyError`. This
test catches the gap at CI time rather than ETL time, so the operator sees a
clear "extend the JSON" signal before they kick off `build_parcels.py`.

Skips cleanly when `tools/cache/zoning_area.geojson` is absent (CI machines
don't carry the ~50 MB cache; the test only runs on workstations where the
cache has been populated).
"""

import json
import unittest
from pathlib import Path

from tools.sources.zoning import _load_multipliers, lookup_multiplier

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHED_ZONING = PROJECT_ROOT / "tools" / "cache" / "zoning_area.geojson"


class TestZoningMultiplierCoverage(unittest.TestCase):
    def test_every_zn_zone_in_cache_is_covered(self):
        if not CACHED_ZONING.exists():
            self.skipTest(f"zoning cache missing: {CACHED_ZONING}")

        multipliers = _load_multipliers()

        with CACHED_ZONING.open(encoding="utf-8") as fp:
            data = json.load(fp)

        seen: set[str] = set()
        for feat in data.get("features") or []:
            zn = (feat.get("properties") or {}).get("ZN_ZONE")
            if zn:
                seen.add(zn)

        missing = sorted(zn for zn in seen if zn not in multipliers)
        self.assertEqual(
            missing, [],
            msg=(
                f"ZN_ZONE classes present in {CACHED_ZONING.name} but missing from "
                f"tools/zoning_multipliers.json: {missing}. "
                "Add an entry for each (max_units_per_lot=0 if non-residential)."
            ),
        )


class TestLookupMultiplier(unittest.TestCase):
    def test_empty_zone_class_returns_zero(self):
        # Empty string = parcel sits outside any zoning polygon (a known
        # no-op state, not an unknown-code state).
        self.assertEqual(lookup_multiplier("", {"RD": 4}), 0)

    def test_known_class_returns_table_value(self):
        self.assertEqual(lookup_multiplier("RD", {"RD": 4, "RA": 20}), 4)

    def test_unknown_class_raises_keyerror_with_helpful_message(self):
        with self.assertRaises(KeyError) as ctx:
            lookup_multiplier("XXX", {"RD": 4})
        msg = str(ctx.exception)
        self.assertIn("XXX", msg)
        self.assertIn("zoning_multipliers.json", msg)


if __name__ == "__main__":
    unittest.main()
