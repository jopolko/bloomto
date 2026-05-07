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


# Two-tier projection (2026-05-07 binary-gate redesign — replaces the prior
# synthesised `score >= 70` threshold). Every gate is a direct check on a
# city primitive; passing means the parcel is multiplex-eligible. Tunable
# here in one place.
#
# ELITE: tighter gate (transit ≤500m, max_units ≥4, lot ≥350m²) — the
#        cover-list of slam-dunk multiplex candidates.
# BROADER: looser gate (transit ≤1500m, max_units ≥3, lot ≥250m²) — every
#          candidate a dev might still want to flip through.
#
# Both share the binary friction-clear gates (heritage clear, TRCA clear,
# RA/RAC excluded, sane footprint, addressed).

ELITE_TRANSIT_BUFFER_M = 500
ELITE_MIN_LOT_M2 = 350
ELITE_MIN_MAX_UNITS = 4

BROADER_TRANSIT_BUFFER_M = 1500
BROADER_MIN_LOT_M2 = 250
BROADER_MIN_MAX_UNITS = 3

# Curated `parcels-top.json` Path B threshold (sixplex-eligible + comfortable
# lot for 4–6 unit multiplex without site-fitting compromise). Chosen by user
# direction 2026-05-07 — well above the by-law's ~360m² multiplex minimum.
CURATED_PATH_B_MIN_LOT_M2 = 500
# Path B top-N cap. Sixplex-eligible territory (T&EY District + Ward 23) is
# larger than it intuitively feels — full Path B uncapped at 500m² lot is
# ~1,800 parcels, more than the curated front needs. Capping by lot area
# desc gives the dev "the biggest sixplex-eligible lots that are also above
# the 500m² floor." Single-primitive cut, defensible.
CURATED_PATH_B_TOP_N = 200

# 500 m² footprint upper bound — see prior comments. Existing structure
# above this is most likely apartment/mid-rise (would have been caught by
# the ETL's 15m height gate too, but the footprint check belt-and-braces
# protects against Massing data gaps).
SHARED_MAX_FOOTPRINT_M2 = 500

# ── Positive-residential gate (2026-05-07) ─────────────────────────────────
# Up through 2026-05-07, every elite-passing parcel passed *negative*
# filters ("not heritage, not TRCA, not too tall, not parking lot...").
# That left an entire class of false positives — large vacant lots that
# are actually school playing fields, municipal yards, parkettes, ROW
# slivers — slipping through because no single negative filter caught
# them. The structural fix flips the burden: a parcel only enters
# elite if it AFFIRMATIVELY looks residential.
#
# A parcel passes the positive-residential check if either:
#   (a) Has a building footprint (cover ≥ POSRES_MIN_COVER) — confirms
#       a structure exists, AND lot is in normal residential range
#       (≤ POSRES_MAX_LOT_AREA_M2); OR
#   (b) Vacant (cover = 0) BUT lot ≤ POSRES_VACANT_MAX_LOT_AREA_M2
#       (typical residential vacant). Bigger vacant lots are
#       overwhelmingly institutional in Toronto's residential zones.
#
# Real multiplex teardown candidates fit (a). Genuine vacant residential
# lots are rare in Toronto and almost always under 2000 m². Anything
# larger and unbuilt is a school field, parkette, or municipal holding.
POSRES_MAX_LOT_AREA_M2 = 5000         # above this is institutional even with a building
POSRES_VACANT_MAX_LOT_AREA_M2 = 2000  # vacant exception only on small typical
                                      # residential lots (above this, almost
                                      # always institutional / parkette / ROW)

# ── Wealthy-enclave filter (2026-05-07) ────────────────────────────────────
# Layer 2/3 multiplex devs (BloomTO's target cohort) operate $1–3M project
# budgets. Forest Hill / Rosedale / Bridle Path / etc. have $4–10M land
# costs alone — economically incompatible. Showing them wastes the dev's
# time on parcels they structurally can't afford. The list below is the
# conservative set of publicly-known Toronto wealthy enclaves; toggleable
# via the "Show high-cost neighborhoods" UI affordance (frontend).
WEALTHY_ENCLAVE_NEIGHBORHOODS = frozenset({
    "Bridle Path-Sunnybrook-York Mills",
    "Forest Hill North",
    "Forest Hill South",
    "Rosedale-Moore Park",
    "Lawrence Park North",
    "Lawrence Park South",
    "Yonge-St.Clair",
    "Yonge-Eglinton",
    "Casa Loma",
    "Hoggs Hollow",
})


def _passes_positive_residential(props: dict) -> bool:
    """Affirmative check that the parcel looks like a residential lot.

    The decisive signal is **3D Massing recorded height**. A real
    residential building (anything 1+ storey) is in Toronto's 3D Massing
    dataset. A parking-lot kiosk, awning, storage shed, or transit
    pavilion is NOT in Massing — too small to mass — but Building
    Outlines may still tag them as a footprint (giving a non-zero
    coverage ratio). Coverage % alone is unreliable: a small house on a
    big lot legitimately has 12–20% cover, while a 9% cover kiosk on a
    parking lot looks identical numerically.

    The rule:
      - Lot > POSRES_MAX_LOT_AREA_M2 → fail (too big for residential)
      - Has a Building Outlines footprint AND a Massing-recorded height
        → pass (real residential structure exists)
      - Vacant (cover = 0 AND no height) AND lot ≤ POSRES_VACANT_MAX_LOT_AREA_M2
        → pass (typical residential vacant)
      - Otherwise → fail (Massing-less footprint = kiosk / shed / awning,
        or other structural ambiguity)
    """
    cover = props.get("buildingCoverageRatio") or 0
    lot_area = props.get("lotAreaM2") or 0
    has_height = props.get("existingMaxBuildingHeightM") is not None

    # Hard ceiling — no Toronto residential lot is >5000 m² in practice.
    if lot_area > POSRES_MAX_LOT_AREA_M2:
        return False

    # Vacant exception — only OK on small typical residential lots.
    if cover == 0 and not has_height:
        return lot_area <= POSRES_VACANT_MAX_LOT_AREA_M2

    # Active redevelopment / construction-site signature: Outlines
    # reports cover = 0 but Massing has a recorded height. The two
    # datasets capture different snapshots of the parcel; when they
    # disagree like this, the parcel is in flux — newly demolished,
    # newly built, or under active construction. None of those are
    # fresh teardown candidates. See 677 Queen St E (active apartment
    # construction, mid-2026).
    if cover == 0 and has_height:
        return False

    # Has cover but no Massing height → footprint exists but the
    # structure is sub-massing-threshold. That's a kiosk / shed / awning
    # / pavilion — not a real residential building. Reject.
    if not has_height:
        return False

    # Has Massing-recorded height → real building exists. Pass.
    return True


def _is_wealthy_enclave(props: dict) -> bool:
    return (props.get("neighborhood") or "") in WEALTHY_ENCLAVE_NEIGHBORHOODS


def _passes_shared(props: dict) -> bool:
    """Binary-gate eligibility shared by elite + broader.

    Heritage clear, TRCA clear, addressed, RA/RAC excluded, footprint sane.
    None of these are weighted; each is a direct check on a city primitive.
    """
    if props.get("heritageStatus") is not None:
        return False
    if props.get("inRegulatedArea"):
        return False
    # RA / RAC zoning excluded: pure-residential apartment zones (20+ units
    # per lot) are not multiplex territory — different product, different
    # audience. CR / CRE / CL stay (mainstreet mixed-use teardowns).
    if props.get("zoneClass") in ("RA", "RAC"):
        return False
    cover = props.get("buildingCoverageRatio") or 0
    area = props.get("lotAreaM2") or 0
    footprint = cover * area
    if footprint >= SHARED_MAX_FOOTPRINT_M2:
        return False
    if 0 < footprint < 30:
        # Building Outlines digitization gap — a 13 m² "shed" on a 1500 m²
        # addressed lot is more likely a data error than a real vacant-with-
        # shed configuration.
        return False
    addr = props.get("address") or ""
    if not addr or addr == "None None":
        return False
    return True


def _transit_distance_m(props: dict) -> float:
    """Closest transit distance — subway or streetcar, whichever is nearer."""
    sub = props.get("distSubwayStreetcarM")
    if sub is None:
        sub = min(
            props.get("distSubwayM") or 99999,
            props.get("distStreetcarM") or 99999,
        )
    return sub if sub is not None else 99999


def is_elite(props: dict) -> bool:
    """Cover-list tier — slam-dunk multiplex candidates. All gates binary."""
    if not _passes_shared(props):
        return False
    if (props.get("lotAreaM2") or 0) < ELITE_MIN_LOT_M2:
        return False
    if (props.get("maxUnits") or 0) < ELITE_MIN_MAX_UNITS:
        return False
    if _transit_distance_m(props) > ELITE_TRANSIT_BUFFER_M:
        return False
    if not props.get("residential", False):
        return False
    return True


def is_broader(props: dict) -> bool:
    """Back-half tier — every candidate worth flipping through. Strict
    superset of `is_elite` (elite is a subset of broader).
    """
    if not _passes_shared(props):
        return False
    if (props.get("lotAreaM2") or 0) < BROADER_MIN_LOT_M2:
        return False
    if (props.get("maxUnits") or 0) < BROADER_MIN_MAX_UNITS:
        return False
    if _transit_distance_m(props) > BROADER_TRANSIT_BUFFER_M:
        return False
    if not props.get("residential", False):
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
        "loaded %d features (already sorted by lot area desc); "
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

    eligible_features = [f for f in features if is_elite(f.get("properties") or {})]
    broader_features = [f for f in features if is_broader(f.get("properties") or {})]
    _log.info(
        "eligible (broader subset): %d → %d (lot>=%dm², max_units>=%d, "
        "transit<=%dm, heritage-clear, TRCA-clear, footprint<%dm², addressed)",
        total_in, len(eligible_features),
        ELITE_MIN_LOT_M2, ELITE_MIN_MAX_UNITS, ELITE_TRANSIT_BUFFER_M,
        SHARED_MAX_FOOTPRINT_M2,
    )
    _log.info(
        "broader filter:            %d → %d (lot>=%dm², max_units>=%d, "
        "transit<=%dm; superset of eligible)",
        total_in, len(broader_features),
        BROADER_MIN_LOT_M2, BROADER_MIN_MAX_UNITS, BROADER_TRANSIT_BUFFER_M,
    )

    # ── Curated `parcels-top.json` (~250 picks, two-path union) ──
    # Path A: parcel has at least one active CKAN owner-activity signal
    #         (severance / demoPermit / violation / prelimZoning).
    # Path B: sixplexEligible AND lotAreaM2 >= CURATED_PATH_B_MIN_LOT_M2.
    # Union produces the curated front of the listings — every parcel has
    # a labeled "why it's here" reason. Falls back to Path B only when
    # data/signals.json is absent (first-ever rebuild).
    signal_pids = _load_signal_pids(Path("data/signals.json"))
    _log.info(
        "curation: signals layer %s · %d signal-bearing parcelIds",
        "loaded" if signal_pids is not None else "absent (Path B only)",
        len(signal_pids) if signal_pids is not None else 0,
    )

    # 2026-05-07 structural gates (apply to BOTH paths) — replace the
    # whack-a-mole exclusion approach with affirmative inclusion criteria:
    #   - Positive residential signature (building present in residential
    #     range, OR vacant within residential lot-size norm)
    #   - Not in a wealthy enclave (Forest Hill, Rosedale, etc. — economically
    #     incompatible with layer 2/3 multiplex dev budgets)
    eligible_after_structural = [
        f for f in eligible_features
        if _passes_positive_residential(f.get("properties") or {})
        and not _is_wealthy_enclave(f.get("properties") or {})
    ]
    _log.info(
        "structural gates:          %d → %d (positive-residential + non-enclave)",
        len(eligible_features), len(eligible_after_structural),
    )

    # Path A: all signal-bearing eligible parcels (uncapped — every one
    # has a story, count is naturally bounded by CKAN signal volume).
    path_a_features = [
        f for f in eligible_after_structural
        if signal_pids is not None
        and str((f.get("properties") or {}).get("parcelId") or "") in signal_pids
    ]
    # Path B: sixplex-eligible AND lot ≥ CURATED_PATH_B_MIN_LOT_M2, capped
    # at top-N by lot area desc. The eligible list is sorted lot-area-desc
    # upstream so a sliced take preserves that ordering.
    path_b_pool = [
        f for f in eligible_after_structural
        if ((f.get("properties") or {}).get("sixplexEligible") is True
            and ((f.get("properties") or {}).get("lotAreaM2") or 0) >= CURATED_PATH_B_MIN_LOT_M2)
    ]
    path_b_features = path_b_pool[:CURATED_PATH_B_TOP_N]

    # Union, deduplicated by parcelId. Preserve overall ordering by lot
    # area desc (path A items get inserted in their lot-area position).
    path_a_pids = {str((g.get("properties") or {}).get("parcelId") or "")
                   for g in path_a_features}
    path_b_pids = {str((g.get("properties") or {}).get("parcelId") or "")
                   for g in path_b_features}
    union_pids = path_a_pids | path_b_pids
    seen_pids = set()
    curated_features = []
    for f in eligible_after_structural:
        pid = str((f.get("properties") or {}).get("parcelId") or "")
        if not pid or pid in seen_pids:
            continue
        if pid in union_pids:
            curated_features.append(f)
            seen_pids.add(pid)

    overlap = len({str((g.get("properties") or {}).get("parcelId") or "") for g in path_a_features}
                  & {str((g.get("properties") or {}).get("parcelId") or "") for g in path_b_features})
    _log.info(
        "curation union: %d unique picks (%d Path A signal · %d Path B sixplex+lot · %d both)",
        len(curated_features), len(path_a_features), len(path_b_features), overlap,
    )

    curated_payload = parcels_top_io.make_payload(
        curated_features, args.top_n, total_citywide=total_citywide,
    )
    parcels_top_io.write_atomic(curated_payload, args.out)
    curated_kb = args.out.stat().st_size / 1024
    _log.info("DONE curated: %d rows → %s | %.0f KB",
              curated_payload["topN"], args.out, curated_kb)

    broader_payload = parcels_top_io.make_payload(
        broader_features, args.top_n, total_citywide=total_citywide,
    )
    parcels_top_io.write_atomic(broader_payload, args.out_broader)
    broader_kb = args.out_broader.stat().st_size / 1024
    _log.info("DONE broader: %d rows → %s | %.0f KB",
              broader_payload["topN"], args.out_broader, broader_kb)
    return 0


def _load_signal_pids(signals_path: Path) -> set | None:
    """Read `data/signals.json` if present and return the set of parcelIds
    with any active signal. Returns `None` when the file is missing
    (first-ever rebuild) — caller falls back to Path B only.
    """
    if not signals_path.exists():
        return None
    try:
        payload = json.loads(signals_path.read_text(encoding="utf-8"))
    except Exception as e:
        _log.warning("signals.json present but unreadable (%s) — Path B only", e)
        return None
    by_pid = payload.get("byParcelId") or {}
    return {str(pid) for pid, signals in by_pid.items() if signals}


if __name__ == "__main__":
    sys.exit(main())
