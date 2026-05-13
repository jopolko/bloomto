import json
import tempfile
import unittest
from pathlib import Path

from tools.io import (
    DEFAULT_NEIGHBORHOOD_COUNT,
    ENTRY_KEYS,
    META_KEYS,
    validate,
    write_atomic,
)


def _make_entry(name="X", score=50):
    return {
        "name": name, "lat": 43.7, "lng": -79.4,
        "score": score, "heatPump": 50, "canopy": 25,
        "walk": 80, "transit": 80, "bike": 80,
        "builtYear": 1970,
        "existing": 5000, "potential": 7500,
    }


def _make_payload(count=DEFAULT_NEIGHBORHOOD_COUNT):
    return {
        "meta": {
            "generatedAt": "2026-05-01T00:00:00Z",
            "sourceVersions": {},
            "scoreFormula": "test",
            "fallbacks": {},
        },
        "neighborhoods": [
            _make_entry(name=f"N{i}", score=100 - i) for i in range(count)
        ],
    }


class ValidateTests(unittest.TestCase):
    def test_accepts_valid_payload(self):
        validate(_make_payload(), expected_count=DEFAULT_NEIGHBORHOOD_COUNT)

    def test_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            validate([], expected_count=0)

    def test_rejects_missing_meta(self):
        p = _make_payload(count=2)
        del p["meta"]
        with self.assertRaisesRegex(ValueError, "meta"):
            validate(p, expected_count=2)

    def test_rejects_missing_neighborhoods(self):
        p = _make_payload(count=2)
        del p["neighborhoods"]
        with self.assertRaisesRegex(ValueError, "neighborhoods"):
            validate(p, expected_count=2)

    def test_rejects_wrong_count(self):
        p = _make_payload(count=2)
        with self.assertRaisesRegex(ValueError, r"\b3\b"):
            validate(p, expected_count=3)

    def test_rejects_missing_meta_subkey(self):
        for key in META_KEYS:
            with self.subTest(key=key):
                p = _make_payload(count=2)
                del p["meta"][key]
                with self.assertRaisesRegex(ValueError, key):
                    validate(p, expected_count=2)

    def test_rejects_missing_entry_key(self):
        for key in ENTRY_KEYS:
            with self.subTest(key=key):
                p = _make_payload(count=1)
                del p["neighborhoods"][0][key]
                with self.assertRaisesRegex(ValueError, key):
                    validate(p, expected_count=1)

    def test_rejects_fallbacks_not_dict(self):
        p = _make_payload(count=1)
        p["meta"]["fallbacks"] = []
        with self.assertRaisesRegex(ValueError, "fallbacks"):
            validate(p, expected_count=1)

    def test_rejects_entry_not_dict(self):
        p = _make_payload(count=2)
        p["neighborhoods"][1] = "not a dict"
        with self.assertRaisesRegex(ValueError, "not a dict"):
            validate(p, expected_count=2)


class WriteAtomicTests(unittest.TestCase):
    def test_valid_payload_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "data" / "neighborhoods.json"
            write_atomic(_make_payload(), out)
            self.assertTrue(out.exists())
            data = json.loads(out.read_text())
            self.assertEqual(len(data["neighborhoods"]), DEFAULT_NEIGHBORHOOD_COUNT)
            self.assertEqual(data["meta"]["scoreFormula"], "test")

    def test_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "deeply" / "nested" / "data" / "neighborhoods.json"
            write_atomic(_make_payload(), out)
            self.assertTrue(out.parent.is_dir())

    def test_invalid_payload_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "data" / "neighborhoods.json"
            with self.assertRaises(ValueError):
                write_atomic(_make_payload(count=2), out)
            self.assertFalse(out.exists())
            # No leftover .tmp files in the parent dir.
            self.assertEqual(list(out.parent.glob("*")), [])

    def test_preexisting_file_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "neighborhoods.json"
            out.write_text('{"stale": true}\n')
            stale_inode = out.stat().st_ino
            write_atomic(_make_payload(), out)
            new_inode = out.stat().st_ino
            self.assertNotEqual(stale_inode, new_inode)  # atomic rename swapped the inode
            data = json.loads(out.read_text())
            self.assertNotIn("stale", data)
            self.assertEqual(len(data["neighborhoods"]), DEFAULT_NEIGHBORHOOD_COUNT)

    def test_invalid_payload_does_not_overwrite_preexisting(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "neighborhoods.json"
            original = '{"original": true}\n'
            out.write_text(original)
            with self.assertRaises(ValueError):
                write_atomic(_make_payload(count=2), out)
            self.assertEqual(out.read_text(), original)

    def test_output_is_pretty_printed_with_trailing_newline(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "neighborhoods.json"
            write_atomic(_make_payload(), out)
            content = out.read_text()
            self.assertTrue(content.endswith("\n"))
            self.assertIn("\n  ", content)  # 2-space indent present

    def test_output_file_mode_0664(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "neighborhoods.json"
            write_atomic(_make_payload(), out)
            self.assertEqual(out.stat().st_mode & 0o777, 0o664)


if __name__ == "__main__":
    unittest.main()
