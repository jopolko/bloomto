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
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import parcels_top_io


# Luxury-street blacklist — (street, neighborhood) tuples identified via
# Street View visual classification (2026-05-12). Same data lives in
# index.html as the `LUXURY_STREETS` JS Set; both consult the canonical
# tools/luxury_streets.json. Belt-and-suspenders gate for the page.
_LUXURY_FILE = Path(__file__).resolve().parent / "luxury_streets.json"
try:
    with _LUXURY_FILE.open(encoding="utf-8") as _fp:
        _LUXURY_STREETS = frozenset(
            tuple(t) for t in json.load(_fp).get("luxury_streets", [])
        )
except FileNotFoundError:
    _LUXURY_STREETS = frozenset()

_STREET_RE = re.compile(r"^\d+[A-Za-z]?\s+(.+?)$")


def _street_part(addr: str | None) -> str | None:
    if not addr:
        return None
    m = _STREET_RE.match(addr.strip())
    return m.group(1) if m else None

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
# Lowered 2026-05-11 from 350 → 250 m² to include classic downtown
# Toronto 25-ft-frontage lots (~232-280 m²) common in Trinity-Bellwoods,
# Little Italy, Roncesvalles, Riverside, Leslieville. These are highly
# laneway-active neighbourhoods + walkable transit + multiplex-friendly
# zoning — the prior 350 m² floor was structurally excluding the
# best-aligned-with-Toronto-net-zero parcels in favour of inner-suburban
# bigger lots that have no laneway access.
ELITE_MIN_LOT_M2 = 250
ELITE_MIN_MAX_UNITS = 4

BROADER_TRANSIT_BUFFER_M = 1500
BROADER_MIN_LOT_M2 = 200  # was 250 — keep elite as a strict subset
BROADER_MIN_MAX_UNITS = 3

# Existing-structure gate (2026-05-07). Elite is "spicy" — only parcels where a
# multiplex teardown is structurally clean (detached) or trivially feasible
# (vacant). Broader extends to semis (badged in the UI for the niche dev with a
# party-wall play). Row/townhouse and unknown are excluded everywhere — row
# multiplex is non-economic (two party walls + ~14ft frontage); unknown means
# the side-yard classifier failed and we'd rather drop than mislead.
ELITE_STRUCTURE_TYPES = frozenset({"detached", "vacant"})
BROADER_STRUCTURE_TYPES = frozenset({"detached", "vacant", "semi"})

# Curated `parcels-top.json` Path B threshold + cap.
#
# 2026-05-11 — lowered min lot 500 → 250 m² so classic Toronto 25-ft-
# frontage downtown lots (Trinity-Bellwoods, Little Italy, Roncesvalles,
# Riverside, Leslieville — laneway-rich, multiplex-active areas) can
# enter the elite cohort. Sixplex math IS tighter on smaller lots but
# the by-law accommodates them. The prior 500 m² floor was structurally
# excluding the most net-zero-aligned parcels in favour of inner-suburban
# 700+ m² lots that have no laneway access.
#
# TOP_N bumped 200 → 500 since the smaller-lot pool expands the candidate
# set significantly. Sort still lot-area DESC so the dev sees biggest
# lots first when browsing.
CURATED_PATH_B_MIN_LOT_M2 = 250
CURATED_PATH_B_TOP_N = 500

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

# ── Wealthy-estate structural filter (2026-05-07 evening) ──────────────────
# The named-neighborhood enclave list above only catches the obvious 10
# wealthy hoods. It misses estate-shaped properties scattered through
# nominally-mixed neighborhoods (1 Grenadier Hts in High Park-Swansea,
# Old Colony / Northdale / Truman in St. Andrew-Windfields, the Cummer
# Ave cluster in Newtonbrook East, etc.). These don't redevelop into
# multiplex regardless of zoning permission — they're owner-occupied
# heritage-grade homes on $5M+ lots.
#
# Structural signature: large lot + low coverage + a recorded structure
# (Massing-confirmed, not just a Building Outlines polygon). 1 Grenadier:
# 2,593 m² lot, 8.3 % coverage, 16.8 m height. Spares legitimate teardown
# candidates (~25–35 % coverage on normal-sized RD lots) and vacant lots
# (which go through the POSRES_VACANT_MAX gate, not this one).
WEALTHY_ESTATE_MIN_LOT_M2 = 2000
WEALTHY_ESTATE_MAX_COVERAGE = 0.15

# ── Mansion filter (2026-05-08) ────────────────────────────────────────────
# Catches wealthy-area mansions that the wealthy-estate filter misses (lot
# under 2000 m² + coverage above 15 %) and that the neighborhood-value gate
# misses ($2 M median is too high to catch gentrified-with-mansions areas
# like Annex / Humewood-Cedarvale / The Beaches).
#
# Combined signal: a 1,200 m² gross floor area (footprint × storeys ≈
# height/3) on a parcel in a neighborhood with $1 M+ average dwelling value.
# Both signals together — big building AND wealthy area — cleanly separate
# mansions from genuine multiplex teardowns. A 1,200 m² floor area on a
# normal lot is structurally a 5-storey big-house — not a 4-plex teardown
# target. The neighborhood avg-value gate confirms the area can sustain
# mansion pricing.
#
# Verified 2026-05-08: caught the user-flagged 39 Glen Oak Dr (East End-
# Danforth, $4M+ mansion) plus 17 similar parcels including 1 Bastedo Ave
# (Woodbine Corridor), Annex 5 Admiral Rd / 16 Howland Ave, four Humewood-
# Cedarvale parcels, three Dufferin Grove Rusholme Rd parcels.
MANSION_MIN_FLOOR_AREA_M2 = 1200
MANSION_MIN_NB_AVG_DWELLING_VALUE = 1_000_000

# ── Dwelling-value gate (2026-05-07 evening) ───────────────────────────────
# Direct-affordability threshold from NPP 2021 owner-reported median dwelling
# value. Catches whole neighborhoods whose median home price exceeds the L2/3
# dev's lot-acquisition envelope. At $2.0M median, the lot alone is ~$1.5–1.8M
# — that's half the L2/3 dev's $1–3M project budget gone before any
# construction. Filter targets:
#   - 6 hoods at $2.0M median: Bridle Path, Forest Hill S, St.Andrew-Windfields,
#     Bedford Park-Nortown, Lawrence Park S, Forest Hill N
#   - Catches St.Andrew-Windfields cleanly (income filter missed it: avg $220K,
#     below the $250K cut, but median dwelling $2.4M)
#   - Catches Forest Hill N where median income ($90K) was misleadingly low
#     because the polygon includes Eglinton condo renters
# Zero curated parcel impact at $2.0M (verified by audit before commit).
DWELLING_VALUE_CEILING_CAD = 2_000_000


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


def _looks_like_wealthy_estate(props: dict) -> bool:
    """Mansion-on-a-yard structural signature: large lot, low coverage,
    AND a Massing-recorded existing structure. Vacant lots fall through
    (no Massing height) — those are gated separately by
    POSRES_VACANT_MAX_LOT_AREA_M2.
    """
    lot = props.get("lotAreaM2") or 0
    cov = props.get("buildingCoverageRatio") or 0
    has_h = props.get("existingMaxBuildingHeightM") is not None
    return (
        lot > WEALTHY_ESTATE_MIN_LOT_M2
        and cov < WEALTHY_ESTATE_MAX_COVERAGE
        and has_h
    )


def _looks_like_mansion(props: dict) -> bool:
    """Big-building-in-wealthy-area signature: gross floor area (footprint
    × storeys) ≥ 1,200 m² AND neighborhood avg-dwelling-value ≥ $1 M.
    Catches mansions that the wealthy-estate filter misses (smaller lot,
    higher coverage) and that the $2 M median-value gate misses (gentrified
    mid-tier neighborhoods like Annex / Humewood-Cedarvale / The Beaches).
    """
    cov = props.get("buildingCoverageRatio") or 0
    lot = props.get("lotAreaM2") or 0
    height = props.get("existingMaxBuildingHeightM") or 0
    if cov <= 0 or lot <= 0 or height <= 0:
        return False
    storeys = max(1, round(height / 3))
    floor_area = cov * lot * storeys
    nb_avg_val = props.get("nbAvgDwellingValue") or 0
    return (
        floor_area >= MANSION_MIN_FLOOR_AREA_M2
        and nb_avg_val >= MANSION_MIN_NB_AVG_DWELLING_VALUE
    )


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
    # Estate filter — mansion-on-a-yard properties don't redevelop into
    # multiplex regardless of zoning permission. Catches 1 Grenadier Hts
    # (2,593 m² · 8.3 % cov) plus the St.Andrew-Windfields / Kingsway /
    # Bedford Park clusters that the named-enclave list misses.
    if _looks_like_wealthy_estate(props):
        return False
    # Mansion filter — big-building-in-wealthy-area. Catches what the
    # estate filter misses (smaller lot, higher coverage) and what the
    # $2M median-value gate misses (gentrified mid-tier areas).
    if _looks_like_mansion(props):
        return False
    # Named-enclave filter (2026-05-07 evening — moved from curated-only
    # path into the shared gate so broader inherits it too). Previously the
    # broader cohort had Forest Hill / Casa Loma / Rosedale parcels in it
    # because the enclave gate only fired during curation; now both elite
    # and broader exclude these.
    if _is_wealthy_enclave(props):
        return False
    # Dwelling-value gate — neighborhoods whose median home price exceeds
    # the L2/3 dev's lot-acquisition envelope. Property is annotated with
    # `nbMedDwellingValue` upstream (in main(), via neighborhoods.json
    # lookup). Treat 0 / missing as pass-through so a missing data row
    # doesn't silently exclude a parcel — fallback nbhds keep their
    # existing structural-filter coverage.
    nb_med_value = props.get("nbMedDwellingValue") or 0
    if nb_med_value > DWELLING_VALUE_CEILING_CAD:
        return False
    # Luxury-street blacklist (added 2026-05-12). 36 (street, neighborhood)
    # tuples where Street View visual inspection + multiplex-math math (6-unit
    # acquisition ceiling ~$2.1-2.5M) showed the parcel is too expensive to
    # underwrite as a teardown play. Mirrored in index.html `LUXURY_STREETS`
    # const; source of truth: tools/luxury_streets.json.
    street = _street_part(props.get("address"))
    nb = props.get("neighborhood")
    if street and nb and (street, nb) in _LUXURY_STREETS:
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
    # Back-lot residue exclusion (added 2026-05-11 — 1030 Danforth /
    # 1558 Davenport pattern). When a parcel abuts a laneway AND its
    # representative point sits ≥15 m from the nearest street centreline,
    # the parcel is geometrically behind the frontage — accessed only via
    # the laneway, not the street. These are residue lots, not
    # underwriting-grade frontage parcels. The frontend was already
    # surfacing `back_lot_candidate` in meta.stats; this gate uses the
    # same signal as a hard-exclusion for the elite cohort.
    if props.get("abutsLaneway") and (props.get("addrToStreetM") or 0) >= 15:
        return False
    # Geometry-suspect exclusion (added 2026-05-11 — 807 Glencairn /
    # 177 Symons pattern). Set by ETL when height-attribution looks
    # like a catastrophic polygon mis-draw — tall (≥12 m) on narrow
    # (<20% cov), or exactly matches a neighbour-height within 0.1 m.
    # Three rebuild iterations couldn't fix these algorithmically (the
    # source polygons in Property Boundaries are wrong), so we hard-
    # exclude from elite to keep cover-list trust 100%. Affected parcels
    # remain in broader for devs who want to investigate.
    if props.get("geometrySuspect"):
        return False
    # Active-demolition exclusion (added 2026-05-12). When the parcel
    # already has a residential-construction permit ≥1 unit created
    # filed in the last 18 months, someone has bought it, gotten
    # permits, and is tearing down. The teardown is happening — just
    # not by us. Per the page's "Multiplex Plays" framing, this parcel
    # is no longer an acquisition opportunity; it's competitive intel
    # better surfaced via the nearby-multiplex-permit comp on
    # SURROUNDING parcels. Affected parcels remain in broader for devs
    # who want to track the dev cluster.
    permits = props.get("permits") or {}
    recent_count = permits.get("recentCount") or 0
    if recent_count >= 1:
        # Any recent unit-creating permit means redev is in motion.
        # The permits payload aggregates only multiplex-relevant rows
        # (DWELLING_UNITS_CREATED ≥ 1), so a non-zero count is enough.
        return False
    # Address-drift exclusion (added 2026-05-11 — 106 Eastwood Rd
    # pattern). Parcel's stated address didn't match any Address Point
    # inside its polygon. Can't underwrite what you can't physically
    # locate. Reject from elite; broader keeps them with a frontend
    # caution.
    if props.get("addressDriftSuspect"):
        return False
    # Apartment-block masquerade exclusion (added 2026-05-12 — 191 Dunn Ave
    # pattern). 5+ storey detached residences on Toronto's 25%+ coverage
    # lots are almost universally converted multi-unit (Parkdale, Annex,
    # High Park, Beaches Victorian conversions) — NOT multiplex-by-law
    # candidates. Even if some are real luxury mansions, teardown of a
    # $5-10M structure for a 6-unit multiplex doesn't pencil either way.
    # Page is called "Multiplex Plays" → every elite row should actually
    # be a multiplex play. Reject; broader keeps them for devs hunting
    # value-add / refinance opportunities outside the multiplex pathway.
    height = props.get("existingMaxBuildingHeightM") or 0
    coverage = props.get("buildingCoverageRatio") or 0
    approx = props.get("existingUnitsApprox") or 0
    ap = props.get("addressPointCount") or 0
    if height >= 14.0 and coverage >= 0.25 and approx >= 10 and (approx - ap) >= 5:
        return False
    # Storey ceiling for the elite cohort (added 2026-05-12 — 304 Indian /
    # 92 Pine / Parkdale Victorian conversions class). 4+ storey "detached"
    # buildings in Toronto are functionally never single-family residences;
    # they are converted multi-unit apartment buildings (Parkdale / Annex /
    # High Park / Beaches Victorians converted decades ago). Even when the
    # classifier and 3D Massing data say "detached", the structure carries
    # tenants and significant improvement value — not a multiplex teardown.
    # 3-storey and shorter detached homes stay in elite regardless of
    # footprint; we trust developers to judge the teardown math themselves
    # given honest data. Vacant lots have no structure so always pass.
    if props.get("existingStructureType") != "vacant" and height > 0:
        storeys = max(1, round(height / 3))
        if storeys >= 4:
            return False
    structure_type = props.get("existingStructureType", "unknown")
    if structure_type not in ELITE_STRUCTURE_TYPES:
        return False
    # 2026-05-09 — elite requires ground-truth-grade structure type.
    # Acceptable sources for non-vacant parcels:
    #   "permit"         — Toronto building-permit STRUCTURE_TYPE record
    #                      (city-recorded, ~32% citywide coverage)
    #   "osm"            — OpenStreetMap volunteer-mapped `building=*` tag
    #                      (~12% additional coverage, 96% agreement w/ permits)
    #   "address_points" — Toronto Address Points spatial join
    #                      (city-recorded address registry)
    #   "classifier" + AP=1 — heuristic detached + Address Points dataset
    #                      confirms exactly 1 dwelling on the parcel polygon.
    #                      The AP=1 ground-truth flips the source from
    #                      "guess" to "city has registered exactly 1 address
    #                      here" — bulletproof on the no-shared-walls /
    #                      no-shared-addresses question. Lifts coverage in
    #                      suburban wards where permit data is sparse;
    #                      addresses the central-Toronto bias previously
    #                      caused by the strict permit/osm gate (86% of
    #                      elite was clustering in T&EY + Ward 23).
    # Vacant parcels exempt — no structure to verify, source is
    # deterministic absence of a Building-Outline polygon.
    if structure_type != "vacant":
        src = props.get("existingStructureSource")
        ap = int(props.get("addressPointCount") or 0)
        if src in ("permit", "osm", "address_points"):
            pass  # ground-truth-source ok
        elif src == "classifier" and structure_type == "detached" and ap == 1:
            pass  # AP=1 confirmation upgrades classifier-detached to elite
        else:
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
    if props.get("existingStructureType", "unknown") not in BROADER_STRUCTURE_TYPES:
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

    # 2026-05-07 evening — annotate each feature with per-neighborhood
    # context (income, dwelling value, permit rate per 1k dwellings) from
    # data/neighborhoods.json. The wealth gate in `_passes_shared` reads
    # `nbMedDwellingValue` from props directly, so this annotation must
    # happen BEFORE is_elite / is_broader filtering. Frontend also reads
    # the same fields via `parcels_top_io.project_features` for the detail
    # panel + sort dimensions.
    nbhds_path = Path("data/neighborhoods.json")
    nb_lookup: dict[str, dict] = {}
    if nbhds_path.exists():
        nb_payload = json.loads(nbhds_path.read_text(encoding="utf-8"))
        for n in nb_payload.get("neighborhoods", []):
            nb_lookup[n["name"]] = n
        _log.info(
            "annotating: %d neighborhoods loaded from %s "
            "(income, dwelling value, permits per 1k dwellings)",
            len(nb_lookup), nbhds_path,
        )
    else:
        _log.warning(
            "neighborhoods.json not found at %s — wealth filter will pass-through "
            "everything (treats nbMedDwellingValue as 0)", nbhds_path,
        )

    # Mutate each feature's props in place. `0` for missing data so the
    # wealth filter's `> CEILING` comparison falls open (pass-through),
    # not closed (silent exclusion of fallback nbhds).
    for f in features:
        p = f.setdefault("properties", {})
        nb = nb_lookup.get(p.get("neighborhood")) or {}
        p["nbMedHouseholdIncome"] = nb.get("medHouseholdIncome", 0)
        p["nbAvgHouseholdIncome"] = nb.get("avgHouseholdIncome", 0)
        p["nbMedDwellingValue"] = nb.get("medDwellingValue", 0)
        p["nbAvgDwellingValue"] = nb.get("avgDwellingValue", 0)
        p["nbPermitsPer1kDwellings"] = nb.get("permitsPer1kDwellings", 0)

    # Laneway-suite-permit back-derivation (added 2026-05-12). Toronto
    # Centreline + OSM both have gaps on some Roncesvalles / Caledonia-
    # Fairbank back-lanes; the laneway-suite permit set is independent
    # ground truth (every laneway suite must abut a real lane by the
    # 2018 by-law). OR the signal into `abutsLaneway`. Done at projection
    # time so we don't need a full ETL rebuild to ship this fix.
    #
    # Two-stage flip:
    #   Stage A — exact-address match: 1303 permits → ~6 elite flips
    #   Stage B — same-street block propagation: any parcel on the same
    #             street within ±LANE_PROP_RANGE house numbers of a
    #             laneway-suite permit also flips. A Toronto residential
    #             block typically spans ±25 house numbers (each side).
    #             So a permit at 164 Macdonell would flag every Macdonell
    #             address from 139-189 as having lane access. This loses
    #             some precision (catches parcels on adjacent blocks of
    #             short streets) in exchange for much higher recall on
    #             blocks where city/OSM data has lane gaps.
    LANE_PROP_RANGE = 25
    try:
        import re as _re
        from collections import defaultdict as _dd
        from tools.sources import building_permits as _permits_src
        from tools.sources._address import normalize_address as _norm_addr
        from pathlib import Path as _Path
        _cache_dir = _Path(__file__).resolve().parent / "cache"
        _laneway_suite_addrs = _permits_src.compute_laneway_suite_address_set(_cache_dir)

        # Stage A: exact-address match.
        _flipped_exact = 0
        for f in features:
            p = f.setdefault("properties", {})
            addr = p.get("address") or ""
            if not addr or p.get("abutsLaneway"):
                continue
            if _norm_addr(addr) in _laneway_suite_addrs:
                p["abutsLaneway"] = True
                p["lanewayPermitOnParcel"] = True  # transparency for the row
                _flipped_exact += 1

        # Stage B: extract (street, LO_NUM) from each permit address →
        # build a same-street number-set lookup. Then for each parcel,
        # check if any permit's LO_NUM is within ±LANE_PROP_RANGE of
        # the parcel's LO_NUM on the same street.
        _NUM_RE = _re.compile(r'^(\d+)[A-Z]?\s+(.+?)$')
        _street_to_nums: dict[str, set] = _dd(set)
        for addr in _laneway_suite_addrs:
            m = _NUM_RE.match(addr)
            if m:
                _street_to_nums[m.group(2)].add(int(m.group(1)))

        _flipped_block = 0
        for f in features:
            p = f.setdefault("properties", {})
            if p.get("abutsLaneway"):
                continue
            addr = p.get("address") or ""
            if not addr:
                continue
            m = _NUM_RE.match(_norm_addr(addr))
            if not m:
                continue
            lo_num = int(m.group(1))
            street = m.group(2)
            perm_nums = _street_to_nums.get(street)
            if not perm_nums:
                continue
            # Any permit number within ±LANE_PROP_RANGE on the same street
            if any(abs(lo_num - pn) <= LANE_PROP_RANGE for pn in perm_nums):
                p["abutsLaneway"] = True
                _flipped_block += 1

        _log.info(
            "laneway-suite back-derivation: %d exact-address flips, "
            "%d same-street block-prop flips (±%d house numbers)",
            _flipped_exact, _flipped_block, LANE_PROP_RANGE,
        )
    except Exception as _e:
        _log.warning("laneway-suite back-derivation skipped: %s", _e)

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

    # 2026-05-07 evening: wealthy-enclave + dwelling-value gates moved into
    # _passes_shared so broader inherits them too. The remaining structural
    # gate here is positive-residential — that one stays curated-only because
    # it would over-cut broader (vacant-on-big-lot is sometimes a real
    # multiplex play in broader, less so in curated).
    eligible_after_structural = [
        f for f in eligible_features
        if _passes_positive_residential(f.get("properties") or {})
    ]
    _log.info(
        "structural gates:          %d → %d (positive-residential)",
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

    # Archive the previous rebuild's parcels-top.json (if any) BEFORE
    # writing the new one. The frontend reads `parcels-top-prev.json` to
    # compute "this week" deltas (added picks since last refresh).
    # Idempotent — does nothing on the first-ever rebuild. Added 2026-05-09.
    _archive_previous(args.out)

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


def _archive_previous(out_path: Path) -> None:
    """If `out_path` exists, copy it to `<stem>-prev<suffix>` so the
    frontend can diff this rebuild vs the last for the weekly-freshness
    strip. Uses copy (not rename) so a build failure mid-archive doesn't
    blow away the live file.
    """
    if not out_path.exists():
        return
    prev_path = out_path.with_name(out_path.stem + "-prev" + out_path.suffix)
    try:
        prev_path.write_bytes(out_path.read_bytes())
        _log.info("archived previous %s → %s", out_path.name, prev_path.name)
    except Exception as e:
        _log.warning("could not archive %s to %s: %s", out_path, prev_path, e)


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
