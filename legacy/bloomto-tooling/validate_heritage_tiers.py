"""Post-ETL validation of the heritage-tier factor defaults.

Per Req 6.1–6.3: after `tools/build_parcels.py` produces a fresh
`data/parcels.geojson` with the new `heritageStatus` enum and per-tier scoring,
this script asserts that the chosen `PART_V_HERITAGE_FACTOR = 0.5` actually
surfaces a useful number of Part V parcels in the top 5 Heritage Conservation
Districts (South/North Rosedale, three Cabbagetowns).

The script is **not** part of the unittest suite — it requires the real cached
heritage SHP zip and a generated `data/parcels.geojson`. Run it manually after
each ETL:

    python3 tools/validate_heritage_tiers.py
    python3 tools/validate_heritage_tiers.py --parcels data/parcels.geojson \\
        --heritage-cache tools/cache/heritage.shp.zip

Exit codes:
    0  — all 5 HCDs surface ≥ 10 parcels at score ≥ 1; Part IV invariant holds.
    1  — at least one HCD falls below the floor (factor likely too aggressive).
    2  — Part IV invariant violated (a Part IV parcel has score > 0; this is a
         scoring-formula bug, not a tuning issue — escalate before re-running).
"""

import argparse
import io
import json
import logging
import sys
import zipfile
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shapefile  # pyshp

from tools.sources.heritage import (
    STATUS_LISTED,
    STATUS_PART_IV,
    STATUS_PART_V,
    _DBF_STATUS_MAP,
    normalize_address,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARCELS = PROJECT_ROOT / "data" / "parcels.geojson"
DEFAULT_HERITAGE_CACHE = PROJECT_ROOT / "tools" / "cache" / "heritage.shp.zip"

# Top 5 HCDs by record count per the requirements doc. The strings are matched
# verbatim against the DBF's `HTG_CONSER` field.
TOP_HCDS = (
    "South Rosedale",
    "North Rosedale",
    "Cabbagetown North",
    "Cabbagetown Southwest",
    "Cabbagetown South",
)

# Floor on per-HCD chip-on parcels at score >= 1. Per Req 6.1: each of these
# HCDs holds 400+ heritage records; a chip-on view that surfaces fewer than 10
# candidates per district is empirically too sparse.
PER_HCD_FLOOR = 10

_log = logging.getLogger("bloomto.validate_heritage_tiers")


def _load_part_v_address_to_hcd(heritage_zip: Path) -> dict[str, str]:
    """Re-read the heritage SHP zip's DBF and return `{normalized_address: HCD}`
    for Part V records only. The HCD is the `HTG_CONSER` field, which is
    not on the parcel wire format so we recover it here.
    """
    with zipfile.ZipFile(heritage_zip) as z:
        names = z.namelist()
        shp_name = next(
            (n for n in names if n.lower().endswith(".shp") and not n.lower().endswith(".shp.xml")),
            None,
        )
        if shp_name is None:
            raise RuntimeError(f"no .shp file inside {heritage_zip}")
        base = shp_name[:-4]
        shp_buf = io.BytesIO(z.read(shp_name))
        shx_buf = io.BytesIO(z.read(base + ".shx"))
        dbf_buf = io.BytesIO(z.read(base + ".dbf"))

    reader = shapefile.Reader(shp=shp_buf, shx=shx_buf, dbf=dbf_buf)
    out: dict[str, str] = {}
    for sr in reader.iterShapeRecords():
        raw_status = sr.record["STATUS"]
        if _DBF_STATUS_MAP.get(raw_status) != STATUS_PART_V:
            continue
        raw_address = sr.record["ADDRESS"] or ""
        hcd = sr.record["HTG_CONSER"] or ""
        if not raw_address or not hcd:
            continue
        normalized = normalize_address(raw_address)
        if normalized:
            out[normalized] = hcd
    return out


def _summarize_parcels(parcels_path: Path) -> tuple[list[dict], dict]:
    with parcels_path.open(encoding="utf-8") as fp:
        payload = json.load(fp)
    features = payload.get("features") or []
    stats = (payload.get("meta") or {}).get("stats") or {}
    return features, stats


def _count_per_hcd_score_pos(features, address_to_hcd) -> Counter:
    counts: Counter = Counter()
    for f in features:
        props = f.get("properties") or {}
        if props.get("heritageStatus") != STATUS_PART_V:
            continue
        if (props.get("score") or 0) < 1:
            continue
        addr = props.get("address") or ""
        normalized = normalize_address(addr)
        hcd = address_to_hcd.get(normalized)
        if hcd:
            counts[hcd] += 1
    return counts


def _count_score_breakdown(features) -> dict[str, int]:
    out = {
        "part_iv_score_zero": 0,
        "part_iv_score_pos": 0,
        "part_v_score_pos": 0,
        "listed_score_pos": 0,
    }
    for f in features:
        props = f.get("properties") or {}
        status = props.get("heritageStatus")
        score = props.get("score") or 0
        if status == STATUS_PART_IV:
            if score > 0:
                out["part_iv_score_pos"] += 1
            else:
                out["part_iv_score_zero"] += 1
        elif status == STATUS_PART_V and score >= 1:
            out["part_v_score_pos"] += 1
        elif status == STATUS_LISTED and score >= 1:
            out["listed_score_pos"] += 1
    return out


def _parse_args(argv):
    p = argparse.ArgumentParser(
        description="Validate per-HCD Part V chip-on surface rate after ETL.",
    )
    p.add_argument("--parcels", type=Path, default=DEFAULT_PARCELS,
                   help=f"path to data/parcels.geojson (default: {DEFAULT_PARCELS})")
    p.add_argument("--heritage-cache", type=Path, default=DEFAULT_HERITAGE_CACHE,
                   help=f"path to heritage SHP zip (default: {DEFAULT_HERITAGE_CACHE})")
    p.add_argument("--per-hcd-floor", type=int, default=PER_HCD_FLOOR,
                   help=f"min chip-on parcels per HCD at score>=1 (default: {PER_HCD_FLOOR})")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not args.parcels.exists():
        _log.error("parcels file missing: %s — run tools/build_parcels.py first", args.parcels)
        return 2
    if not args.heritage_cache.exists():
        _log.error("heritage cache missing: %s", args.heritage_cache)
        return 2

    _log.info("loading Part V → HCD map from %s", args.heritage_cache)
    address_to_hcd = _load_part_v_address_to_hcd(args.heritage_cache)
    _log.info("loaded %d Part V address→HCD mappings", len(address_to_hcd))

    _log.info("loading parcels from %s", args.parcels)
    features, stats = _summarize_parcels(args.parcels)
    _log.info("loaded %d features", len(features))

    breakdown = _count_score_breakdown(features)

    print("=" * 60)
    print("Heritage tier breakdown")
    print("=" * 60)
    print(f"  Part IV / score == 0:  {breakdown['part_iv_score_zero']}  (expected: all)")
    print(f"  Part IV / score  > 0:  {breakdown['part_iv_score_pos']}   (expected: 0 — invariant)")
    print(f"  Part V  / score >= 1:  {breakdown['part_v_score_pos']}")
    print(f"  Listed  / score >= 1:  {breakdown['listed_score_pos']}")
    if stats:
        print()
        print("Stats from meta.stats:")
        for k in ("heritagePartIV", "heritagePartV", "heritageListed", "heritageUnjoined"):
            if k in stats:
                print(f"  {k}: {stats[k]}")

    # Hard-block invariant.
    if breakdown["part_iv_score_pos"] > 0:
        _log.error(
            "INVARIANT VIOLATED: %d Part IV parcels have score > 0 — Part IV is "
            "supposed to be a hard block (heritage_factor=0). Investigate "
            "tools/parcel_scoring.py before re-running with different factors.",
            breakdown["part_iv_score_pos"],
        )
        return 2

    # Per-HCD floor.
    counts = _count_per_hcd_score_pos(features, address_to_hcd)
    print()
    print("=" * 60)
    print(f"Top 5 HCDs: chip-on parcels with score >= 1 (floor {args.per_hcd_floor})")
    print("=" * 60)
    failed_hcds: list[str] = []
    for hcd in TOP_HCDS:
        n = counts.get(hcd, 0)
        marker = "OK" if n >= args.per_hcd_floor else "FAIL"
        print(f"  {marker:5s} {hcd:30s} {n:5d}")
        if n < args.per_hcd_floor:
            failed_hcds.append(hcd)

    if failed_hcds:
        print()
        print("=" * 60)
        print("TUNING RECOMMENDATION")
        print("=" * 60)
        print(
            f"  {len(failed_hcds)} HCD(s) below the {args.per_hcd_floor}-parcel "
            "floor. Try lowering PART_V_HERITAGE_FACTOR in "
            "tools/parcel_scoring.py toward 0.6–0.7 (current: 0.5) in 0.05 "
            "increments, then re-run build_parcels.py and this script. "
            "Failed HCDs: " + ", ".join(failed_hcds),
        )
        return 1

    print()
    print("All 5 HCDs surface >= floor. Factor defaults are acceptable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
