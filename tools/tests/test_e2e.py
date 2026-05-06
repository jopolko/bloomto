"""End-to-end test of the orchestrator's assembly contract (Task 36, v1.1).

Loads a 5-polygon neighborhoods fixture, synthesizes per-source metric dicts,
calls `build_neighborhoods.assemble_payload(...)`, and asserts:

  - payload validates against `io.validate` with `expected_count=5`
  - every entry has the 12 v1.1 keys
  - `meta` has the 4 v1.1 keys, `meta.fallbacks` is a dict
  - entries are sorted by score descending
  - assembly is idempotent across two runs (modulo `meta.generatedAt`)
"""

import json
import unittest
from pathlib import Path

from tools import io as bloomto_io
from tools.build_neighborhoods import FALLBACK_SOURCE_ORDER, assemble_payload
from tools.sources.neighborhoods import fetch_neighborhoods

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Per-name metric values chosen to produce a clear sort order and exercise
# the Missing-Middle headroom (potential / existing - 1) on both sides:
# Bridle Path has the highest existing dwelling count and modest headroom;
# Regent Park has dramatic headroom (existing=2000 → potential=8000).
SYNTH = {
    "Yonge-Bay Corridor":              {"hp": 90, "canopy": 10, "walk": 95, "transit": 95, "bike": 70, "by": 1990, "ex": 8000, "pot": 12000},
    "Regent Park":                     {"hp": 60, "canopy": 15, "walk": 80, "transit": 85, "bike": 60, "by": 2010, "ex": 2000, "pot": 8000},
    "High Park North":                 {"hp": 70, "canopy": 35, "walk": 70, "transit": 65, "bike": 50, "by": 1960, "ex": 5000, "pot": 7500},
    "Bridle Path-Sunnybrook-York Mills":{"hp": 50, "canopy": 55, "walk": 25, "transit": 30, "bike": 20, "by": 1970, "ex": 3000, "pot": 4000},
    "Etobicoke City Centre":           {"hp": 75, "canopy": 25, "walk": 55, "transit": 70, "bike": 40, "by": 1980, "ex": 6000, "pot": 9000},
}


def _build_payload():
    nbs = fetch_neighborhoods(FIXTURES, expected_count=len(SYNTH))
    by_name = {n.name: SYNTH[n.name] for n in nbs}

    return assemble_payload(
        nbs,
        heat_pump={n.name: by_name[n.name]["hp"] for n in nbs},
        canopy={n.name: by_name[n.name]["canopy"] for n in nbs},
        built_year={n.name: by_name[n.name]["by"] for n in nbs},
        existing={n.name: by_name[n.name]["ex"] for n in nbs},
        potential={n.name: by_name[n.name]["pot"] for n in nbs},
        transit={n.name: by_name[n.name]["transit"] for n in nbs},
        bike={n.name: by_name[n.name]["bike"] for n in nbs},
        walk={n.name: by_name[n.name]["walk"] for n in nbs},
        fallbacks_by_source={"solar_to": ["Bridle Path-Sunnybrook-York Mills"]},
    )


class TestE2EAssembly(unittest.TestCase):
    def test_validates_with_fixture_count(self):
        payload = _build_payload()
        bloomto_io.validate(payload, expected_count=len(SYNTH))

    def test_entry_has_v11_keys(self):
        payload = _build_payload()
        for entry in payload["neighborhoods"]:
            self.assertEqual(set(entry.keys()), set(bloomto_io.ENTRY_KEYS))
            self.assertIsInstance(entry["score"], int)
            self.assertGreaterEqual(entry["score"], 0)
            self.assertLessEqual(entry["score"], 100)

    def test_meta_has_v11_keys_and_fallbacks_dict(self):
        meta = _build_payload()["meta"]
        self.assertEqual(set(meta.keys()), set(bloomto_io.META_KEYS))
        self.assertIsInstance(meta["fallbacks"], dict)
        self.assertEqual(set(meta["fallbacks"].keys()), set(FALLBACK_SOURCE_ORDER))
        self.assertEqual(meta["fallbacks"]["solar_to"],
                         ["Bridle Path-Sunnybrook-York Mills"])
        self.assertEqual(meta["fallbacks"]["canopy"], [])

    def test_entries_sorted_descending(self):
        scores = [e["score"] for e in _build_payload()["neighborhoods"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_idempotent_modulo_generatedAt(self):
        a = _build_payload()
        b = _build_payload()
        a["meta"]["generatedAt"] = ""
        b["meta"]["generatedAt"] = ""
        self.assertEqual(
            json.dumps(a, sort_keys=True),
            json.dumps(b, sort_keys=True),
        )

    def test_top_neighborhood_is_yonge_bay(self):
        # Sanity check that scoring + sort wired correctly: Yonge-Bay Corridor
        # has the highest synthesized inputs across all 6 components.
        top = _build_payload()["neighborhoods"][0]
        self.assertEqual(top["name"], "Yonge-Bay Corridor")


if __name__ == "__main__":
    unittest.main()
