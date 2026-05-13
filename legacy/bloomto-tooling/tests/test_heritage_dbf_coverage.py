"""Coverage regression for the cached HRAP DBF's `STATUS` field.

When a future Toronto Heritage Register schema change introduces a fourth
protection tier (or renames an existing one), the ETL's DBF parser will fail
loudly via `_iter_records_from_zip`'s `ValueError`. This test catches the
gap at CI time rather than ETL time, so the operator sees a clear "extend
the canonical map" signal before they kick off `build_parcels.py`.

Skips cleanly when `tools/cache/heritage.shp.zip` is absent (CI machines
don't carry the cache; the test only runs on workstations where the cache
has been populated). Mirrors the `test_zoning_multipliers.py` pattern that
shipped with the zone-class-coverage fix.
"""

import io
import unittest
import zipfile
from pathlib import Path

import shapefile  # pyshp

from tools.sources.heritage import _DBF_STATUS_MAP

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHED_HERITAGE = PROJECT_ROOT / "tools" / "cache" / "heritage.shp.zip"


class TestHeritageDbfStatusCoverage(unittest.TestCase):
    def test_every_status_in_cache_is_covered(self):
        if not CACHED_HERITAGE.exists():
            self.skipTest(f"heritage cache missing: {CACHED_HERITAGE}")

        with zipfile.ZipFile(CACHED_HERITAGE) as z:
            names = z.namelist()
            shp_name = next(
                (n for n in names if n.lower().endswith(".shp") and not n.lower().endswith(".shp.xml")),
                None,
            )
            self.assertIsNotNone(shp_name, f"no .shp file inside {CACHED_HERITAGE}")
            base = shp_name[:-4]
            shx_buf = io.BytesIO(z.read(base + ".shx"))
            dbf_buf = io.BytesIO(z.read(base + ".dbf"))
            shp_buf = io.BytesIO(z.read(shp_name))

        reader = shapefile.Reader(shp=shp_buf, shx=shx_buf, dbf=dbf_buf)
        seen = {sr.record["STATUS"] for sr in reader.iterShapeRecords()}

        missing = sorted(s for s in seen if s not in _DBF_STATUS_MAP)
        self.assertEqual(
            missing, [],
            msg=(
                f"DBF STATUS values present in {CACHED_HERITAGE.name} but missing "
                f"from tools/sources/heritage._DBF_STATUS_MAP: {missing}. "
                "Add a canonical mapping for each (or remove the legacy entry)."
            ),
        )

        # Symmetric check: every documented mapping is exercised by the cache.
        unused = sorted(s for s in _DBF_STATUS_MAP if s not in seen)
        self.assertEqual(
            unused, [],
            msg=(
                f"_DBF_STATUS_MAP lists {unused} but the cached HRAP no longer "
                "contains records with that STATUS. The mapping may be stale; "
                "verify against the upstream Heritage Register and consider removing."
            ),
        )


if __name__ == "__main__":
    unittest.main()
