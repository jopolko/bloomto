"""Post-process: read data/parcels.geojson and write data/parcels-top.json.

Standalone script — does NOT re-run the ETL. Reads the canonical GeoJSON the
ETL produced, projects the top-N features (already sorted by score desc) into
flat table rows, writes atomically. Runs in seconds.

Usage:
    python3 tools/build_parcels_top.py
    python3 tools/build_parcels_top.py --top-n 5000
    python3 tools/build_parcels_top.py --in data/parcels.geojson --out data/parcels-top.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import parcels_top_io

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_log = logging.getLogger("bloomto.build_parcels_top")


# Two-tier elite system (2026-05-04 user direction: focused-by-default with
# depth-on-demand — the magazine reading pattern). Default view = ELITE
# (cover article). Toggle = BROADER (back-half browse — every candidate the
# user might still want to consider). Tunable here in one place.
ELITE_MIN_SCORE = 70
ELITE_MIN_LOT_M2 = 350

BROADER_MIN_SCORE = 60       # opens score 60-69 parcels (3-unit zones, 250-400m transit)
BROADER_MIN_LOT_M2 = 250     # opens inner-city T&EY small-lot multiplex (sixplex territory)

# Both tiers share these gates (so the broader set still respects buy/no-buy).
# 500 m² is roughly the upper bound of a typical Toronto detached / duplex /
# triplex existing structure. Above this, the existing build is most likely
# a small apartment / mid-rise commercial / plaza — too costly to teardown
# vs what a 4-plex or 6-plex would replace it with. NOT an "8-plex" — there
# is no such category in Toronto's multiplex by-law (citywide cap is 4u,
# T&EY/Ward 23 cap is 6u as of June 2025). The 500 m² is a footprint
# heuristic, not a unit-count target.
SHARED_MAX_FOOTPRINT_M2 = 500


def _passes_shared(props: dict) -> bool:
    """Gates true for both tiers — heritage-clear, TRCA-clear, addressed,
    teardown-cheap. The score and lot thresholds differentiate elite vs
    broader.

    Footprint sanity gate (added 2026-05-04): reject 0 < footprint < 30 m².
    This range almost always means the Building Outlines dataset is missing
    the actual structure (digitization gap) — a 13 m² "shed" on a 1,500 m²
    addressed lot is far more likely a data error than a real vacant-with-
    shed configuration. Truly vacant lots (footprint = 0) are kept.
    """
    if props.get("heritageStatus") is not None:
        return False
    if props.get("inRegulatedArea"):
        return False
    # RA / RAC zoning excluded 2026-05-06: pure-residential apartment zones
    # (20+ units per lot per zoning_multipliers.json) are not multiplex
    # territory. A dev evaluating these lots will build a small apartment,
    # not a 4-6 unit multiplex — different product, different financing,
    # different audience. The wire was surfacing RA parcels with normal-
    # looking civic addresses (e.g., 2439 Finch Ave W = vacant RA lot,
    # Humbermede neighborhood with 4 permits in 5 years = dead market) and
    # devs were asking why apartment land showed up as multiplex picks.
    # CR / CRE / CL stay — those mainstreet mixed-use lots are real
    # multiplex teardowns (single-storey storefront → 6-plex above retail).
    if props.get("zoneClass") in ("RA", "RAC"):
        return False
    cover = props.get("buildingCoverageRatio") or 0
    area = props.get("lotAreaM2") or 0
    footprint = cover * area
    if footprint >= SHARED_MAX_FOOTPRINT_M2:
        return False
    if 0 < footprint < 30:
        # Suspect: tiny accessory structure with no real building. Either a
        # data gap or a true vacant-with-shed (rare). Drop either way.
        return False
    addr = props.get("address") or ""
    if not addr or addr == "None None":
        return False
    return True


def is_elite(props: dict) -> bool:
    """Cover-article tier — slam-dunk multiplex candidates."""
    if not _passes_shared(props):
        return False
    if (props.get("score") or 0) < ELITE_MIN_SCORE:
        return False
    if (props.get("lotAreaM2") or 0) < ELITE_MIN_LOT_M2:
        return False
    return True


def is_broader(props: dict) -> bool:
    """Back-half tier — every candidate worth flipping through. Strict
    superset of `is_elite` (the elite are a subset of the broader set).
    """
    if not _passes_shared(props):
        return False
    if (props.get("score") or 0) < BROADER_MIN_SCORE:
        return False
    if (props.get("lotAreaM2") or 0) < BROADER_MIN_LOT_M2:
        return False
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Project parcels.geojson → parcels-top.json + parcels-broader.json"
    )
    parser.add_argument("--in", dest="in_path", type=Path,
                        default=Path("data/parcels.geojson"))
    parser.add_argument("--out", type=Path,
                        default=Path("data/parcels-top.json"),
                        help="elite-set output path (default data/parcels-top.json)")
    parser.add_argument("--out-broader", type=Path,
                        default=Path("data/parcels-broader.json"),
                        help="broader-tier output path (lazy-loaded behind 'Show all candidates'; "
                             "default data/parcels-broader.json)")
    parser.add_argument("--top-n", type=int, default=20000,
                        help="max rows in EACH output file (default 20000)")
    parser.add_argument("--no-elite", action="store_true",
                        help="skip tier filtering (legacy: emit all top-N by score, no broader file)")
    args = parser.parse_args()

    if not args.in_path.exists():
        _log.error("input not found: %s — run build_parcels.py first", args.in_path)
        return 1

    _log.info("reading %s", args.in_path)
    geojson = json.loads(args.in_path.read_text(encoding="utf-8"))
    features = geojson["features"]
    total_in = len(features)
    # Citywide total (every parcel processed, not just score-positive ones)
    # — sourced from the master GeoJSON's meta. The frontend uses this for
    # the "filtered from ~N citywide" copy so the figure stays current as
    # subdivisions / lot merges shift the parcel count over time.
    total_citywide = (
        (geojson.get("meta") or {}).get("stats", {}).get("totalParcels")
    )
    _log.info(
        "loaded %d features (already sorted by max(score, softScore) desc); "
        "citywide total = %s",
        total_in, total_citywide,
    )

    if args.no_elite:
        # Legacy single-file path — kept for parity with downstream tools.
        payload = parcels_top_io.make_payload(features, args.top_n, total_citywide=total_citywide)
        parcels_top_io.write_atomic(payload, args.out)
        out_size_kb = args.out.stat().st_size / 1024
        _log.info("DONE (legacy): %d rows → %s | %.0f KB",
                  payload["topN"], args.out, out_size_kb)
        return 0

    elite_features = [f for f in features if is_elite(f.get("properties") or {})]
    broader_features = [f for f in features if is_broader(f.get("properties") or {})]
    _log.info(
        "elite filter:   %d → %d (score>=%d, lot>=%dm², heritage-clear, TRCA-clear, "
        "footprint<%dm², addressed)",
        total_in, len(elite_features),
        ELITE_MIN_SCORE, ELITE_MIN_LOT_M2, SHARED_MAX_FOOTPRINT_M2,
    )
    _log.info(
        "broader filter: %d → %d (score>=%d, lot>=%dm²; superset of elite)",
        total_in, len(broader_features),
        BROADER_MIN_SCORE, BROADER_MIN_LOT_M2,
    )

    elite_payload = parcels_top_io.make_payload(
        elite_features, args.top_n, total_citywide=total_citywide,
    )
    parcels_top_io.write_atomic(elite_payload, args.out)
    elite_kb = args.out.stat().st_size / 1024
    _log.info("DONE elite:   %d rows → %s | %.0f KB",
              elite_payload["topN"], args.out, elite_kb)

    broader_payload = parcels_top_io.make_payload(
        broader_features, args.top_n, total_citywide=total_citywide,
    )
    parcels_top_io.write_atomic(broader_payload, args.out_broader)
    broader_kb = args.out_broader.stat().st_size / 1024
    _log.info("DONE broader: %d rows → %s | %.0f KB",
              broader_payload["topN"], args.out_broader, broader_kb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
