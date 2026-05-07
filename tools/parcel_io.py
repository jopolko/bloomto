"""Validate the assembled parcel payload and write data/parcels.geojson atomically.

Mirrors `tools/io.py` (v1.1 neighborhood I/O) with the GeoJSON-shaped contract
required by Component 5 of the parcel-multiplex-readiness design:

  - top-level FeatureCollection: `type`, `meta`, `features`
  - 20 FEATURE_PROPERTIES per Feature, none missing
  - 7 META_KEYS, none missing
  - the `solarScore is None` ↔ `solarShadowQuality == "unavailable"` invariant,
    enforced at validate-time so a regression in the shadow-analysis stage
    can never silently emit a polluted feature

The atomic-write pattern (`tempfile.mkstemp` + `os.replace`, `0o664` mode,
`www-data` group chown) is the same as v1.1; copying the function instead of
re-exporting keeps the parcel pipeline independently runnable even if the
neighborhood orchestrator's `tools/io.py` shape evolves.
"""

import grp
import json
import os
import tempfile
from pathlib import Path

FEATURE_PROPERTIES = (
    "parcelId",
    "address",
    "zoneClass",
    "maxUnits",
    "maxUnitsRationale",
    "zoneString",
    "zoneFsi",
    "zoneMinLotFrontageM",
    "zoneMinLotAreaM2",
    "residential",
    "heritageStatus",
    "distSubwayStreetcarM",
    "distSubwayM",
    "distStreetcarM",
    "distBusM",
    "neighborhood",
    "builtYear",
    "cornerLot",
    "abutsLaneway",
    "nearRapidToCorridor",
    "inFloodingStudyArea",
    "inRegulatedArea",
    "permits",
    "neighborhoodPermitComp",
    "neighborhoodCanopyPct",
    "streetTreeCount",
    "matureTreeCount",
    "distBikeLaneM",
    "sixplexEligible",
    "lotAreaM2",
    "lotAspectRatio",
    "buildingCoverageRatio",
    "solarScoreRaw",
    "solarScore",
    "solarShadowQuality",
    "postwarNeighborhood",
    # ── 2026-05-05 architect / dev panel ──
    # Nested objects: keys validated as a presence check. Sub-key contracts
    # live in `parcels_top_io.project_features()` (which flattens for the
    # frontend) and the synthetic test fixtures.
    "lotGeometry",        # { longAxisM, shortAxisM, orientationDeg }
    "neighborHeights",    # { nAvgM, sAvgM, eAvgM, wAvgM }  (each may be None)
    "existingMaxBuildingHeightM",  # float m or None — tallest 3D Massing
                                   # building substantially overlapping the
                                   # parcel; gates apartment-exclusion at 15m
    "solarYieldKwhPerYr",   # int kWh, the un-shadowed best-rooftop figure
    "pvCapacityKwEstimate", # float kW, derived from solarYieldKwhPerYr
    "sixplexBonusValueCad", # int CAD, or None when ineligible / no comp
)

META_KEYS = (
    "generatedAt",
    "sourceVersions",
    "solarMethodology",
    "shadowAnalysis",
    "permits",
    "stats",
)

# Required `meta.stats` keys. The four heritage counters replaced the legacy
# `heritageFlagged` integer when the heritage-tiered-status spec landed; the
# validator rejects any payload still carrying the old key (no half-migrated
# state on the wire).
REQUIRED_STATS_KEYS = frozenset({
    "totalParcels",
    "heritagePartIV",
    "heritagePartV",
    "heritageListed",
    "heritageUnjoined",
    "residential",
    "cornerLot",
    "postwar",
    "skippedNoNeighborhood",
    "skippedNonBuildable",
    "skippedInstitutional",
    "skippedInstitutionalByCategory",
    "skippedOsmLanduse",
    "skippedTallExistingBuilding",
    "abutsLaneway",
    "nearRapidToCorridor",
    "inFloodingStudyArea",
    "inRegulatedArea",
    "matureTrees",
    "sixplexEligible",
})

LEGACY_STATS_KEY = "heritageFlagged"
LEGACY_FEATURE_KEY = "heritage"

SOLAR_SHADOW_QUALITIES = frozenset({"measured", "estimated", "unavailable"})

HERITAGE_STATUSES = frozenset({"part_iv", "part_v", "listed", None})


def validate(payload) -> None:
    """Validate the parcel FeatureCollection shape + invariants.

    Raises `ValueError` with a contextual message on any violation. The caller
    (orchestrator) is expected to let this propagate to a non-zero exit per
    Req 12.5 — there is no silent fallback.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict, got {type(payload).__name__}")

    for key in ("type", "meta", "features"):
        if key not in payload:
            raise ValueError(f"payload missing top-level key: {key!r}")

    if payload["type"] != "FeatureCollection":
        raise ValueError(
            f"payload.type must be 'FeatureCollection', got {payload['type']!r}"
        )

    meta = payload["meta"]
    if not isinstance(meta, dict):
        raise ValueError("meta must be a dict")
    for key in META_KEYS:
        if key not in meta:
            raise ValueError(f"meta missing key: {key!r}")

    stats = meta.get("stats")
    if not isinstance(stats, dict):
        raise ValueError("meta.stats must be a dict")
    if LEGACY_STATS_KEY in stats:
        raise ValueError(
            f"meta.stats contains legacy key {LEGACY_STATS_KEY!r}; this build "
            "expects the four per-tier counts (heritagePartIV, heritagePartV, "
            "heritageListed, heritageUnjoined). Re-run build_parcels.py."
        )
    for key in REQUIRED_STATS_KEYS:
        if key not in stats:
            raise ValueError(f"meta.stats missing required key: {key!r}")

    features = payload["features"]
    if not isinstance(features, list):
        raise ValueError("features must be a list")
    if not features:
        raise ValueError("features must be a non-empty list")

    for i, feat in enumerate(features):
        _validate_feature(i, feat)


def _validate_feature(i: int, feat) -> None:
    if not isinstance(feat, dict):
        raise ValueError(f"features[{i}] is not a dict")

    for key in ("type", "geometry", "properties"):
        if key not in feat:
            raise ValueError(f"features[{i}] missing key: {key!r}")

    if feat["type"] != "Feature":
        raise ValueError(
            f"features[{i}].type must be 'Feature', got {feat['type']!r}"
        )

    geometry = feat["geometry"]
    if not isinstance(geometry, dict):
        raise ValueError(f"features[{i}].geometry must be a dict")
    if geometry.get("type") != "Point":
        raise ValueError(
            f"features[{i}].geometry.type must be 'Point', got {geometry.get('type')!r}"
        )

    props = feat["properties"]
    if not isinstance(props, dict):
        raise ValueError(f"features[{i}].properties must be a dict")
    if LEGACY_FEATURE_KEY in props:
        raise ValueError(
            f"features[{i}] (address={props.get('address')!r}) contains legacy "
            f"{LEGACY_FEATURE_KEY!r} key; this build expects 'heritageStatus'. "
            "Re-run build_parcels.py to regenerate the payload."
        )
    for key in FEATURE_PROPERTIES:
        if key not in props:
            raise ValueError(
                f"features[{i}] (address={props.get('address')!r}) missing property: {key!r}"
            )

    heritage_status = props["heritageStatus"]
    if heritage_status not in HERITAGE_STATUSES:
        raise ValueError(
            f"features[{i}].properties.heritageStatus {heritage_status!r} "
            f"not in {sorted(s for s in HERITAGE_STATUSES if s is not None)} ∪ {{null}}"
        )

    quality = props["solarShadowQuality"]
    if quality not in SOLAR_SHADOW_QUALITIES:
        raise ValueError(
            f"features[{i}].properties.solarShadowQuality {quality!r} "
            f"not in {sorted(SOLAR_SHADOW_QUALITIES)}"
        )

    solar_score = props["solarScore"]
    if quality == "unavailable":
        if solar_score is not None:
            raise ValueError(
                f"features[{i}].properties.solarScore must be None when "
                f"solarShadowQuality == 'unavailable', got {solar_score!r}"
            )
    else:
        if solar_score is None:
            raise ValueError(
                f"features[{i}].properties.solarScore is None but "
                f"solarShadowQuality == {quality!r} (only 'unavailable' may have null)"
            )


def _www_data_gid() -> int | None:
    try:
        return grp.getgrnam("www-data").gr_gid
    except KeyError:
        return None


def write_atomic(payload: dict, out_path: Path) -> None:
    out_path = Path(out_path)
    validate(payload)

    out_path.parent.mkdir(parents=True, exist_ok=True, mode=0o775)

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=out_path.parent,
        prefix=out_path.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
            # No `indent` and a compact `separators` — the file is machine-consumed
            # by index.html. Pretty-printing 50k+ features added ~2–3 MB of pure
            # whitespace (~300–500 KB after gzip). Compact form trims that.
            json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=False)
            fp.write("\n")
        os.chmod(tmp_name, 0o664)
        os.replace(tmp_name, out_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    gid = _www_data_gid()
    if gid is not None:
        try:
            os.chown(out_path, -1, gid)
        except PermissionError:
            pass
