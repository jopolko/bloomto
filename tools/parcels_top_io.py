"""Slim flat-JSON projection of parcels.geojson for the front-end table.

The GeoJSON FeatureCollection at `data/parcels.geojson` is the canonical ETL
artifact (validated, provenanced, ~95 MB). The browser doesn't need geometry,
doesn't need GeoJSON ceremony, and doesn't need 95 MB — it needs a flat
sortable table. This module projects the top-N features by score into a
table-friendly shape (one flat object per row, lat/lng inlined, geometry
collapsed) and writes it atomically.

Atomic-write pattern mirrors `tools/parcel_io.py` and `tools/io.py`. The
projection is the full FEATURE_PROPERTIES set as of 2026-05-02 —
`solarScoreRaw` powers the "Shadow-Free" badge, `residential`,
`parcelId`, and `builtYear` are now surfaced for badge gates and
client-side traceability.
"""

import grp
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROW_KEYS = (
    "parcelId",
    "address",
    "neighborhood",
    "builtYear",
    "lat",
    "lng",
    "maxUnits",
    "maxUnitsRationale",
    "zoneString",
    "zoneFsi",
    "zoneMinLotFrontageM",
    "zoneMinLotAreaM2",
    "zoneClass",
    "residential",
    "lotAreaM2",
    "heritageStatus",
    "buildingCoverageRatio",
    "cornerLot",
    "lotAspectRatio",
    "distSubwayM",
    "distSubwayStreetcarM",
    "distStreetcarM",
    "distBusM",
    "abutsLaneway",
    "nearRapidToCorridor",
    "inFloodingStudyArea",
    "inRegulatedArea",
    "permitsRecentCount",
    "permitsRecentValueTotal",
    "permitsRecentMostRecentDate",
    "permitsDenominatorSource",
    "nbPermitMedianCostPerUnit",
    "nbPermitSampleSize",
    "neighborhoodCanopyPct",
    "streetTreeCount",
    "matureTreeCount",
    "distBikeLaneM",
    "sixplexEligible",
    "solarScore",
    "solarScoreRaw",
    "solarShadowQuality",
    "postwarNeighborhood",
    # ── 2026-05-05 architect / dev panel (flattened from GeoJSON nested) ──
    "lotLongAxisM",
    "lotShortAxisM",
    "lotOrientationDeg",
    "neighborHeightNAvgM",
    "neighborHeightSAvgM",
    "neighborHeightEAvgM",
    "neighborHeightWAvgM",
    "existingMaxBuildingHeightM",
    "solarYieldKwhPerYr",
    "pvCapacityKwEstimate",
    "sixplexBonusValueCad",
)

PAYLOAD_KEYS = ("generatedAt", "totalAvailable", "topN", "rows")
# `totalParcels` (citywide count) is set by build_parcels_top.py from
# parcels.geojson `meta.stats.total`. Optional in validate() for backward
# compatibility with payloads written before 2026-05-07.


def project_features(features, top_n):
    """Project `features` (pre-sorted by score desc, GeoJSON Feature shape)
    to a list of flat row dicts. Returns (rows, total_available).

    `lotAreaM2` uses `.get` so pre-2026-05-02 GeoJSON files (built before the
    field was added to FEATURE_PROPERTIES) project to `None` instead of
    KeyError. The UI surfaces null as "—". Re-run `build_parcels.py` to
    populate the field.
    """
    total = len(features)
    rows = []
    for feat in features[:top_n]:
        coords = feat["geometry"]["coordinates"]
        props = feat["properties"]
        rows.append({
            "parcelId": props.get("parcelId"),
            "address": props["address"],
            "neighborhood": props["neighborhood"],
            "builtYear": props.get("builtYear"),
            "lat": coords[1],
            "lng": coords[0],
            "maxUnits": props["maxUnits"],
            "maxUnitsRationale": props.get("maxUnitsRationale", "zone_average"),
            "zoneString": props.get("zoneString", ""),
            "zoneFsi": props.get("zoneFsi"),
            "zoneMinLotFrontageM": props.get("zoneMinLotFrontageM"),
            "zoneMinLotAreaM2": props.get("zoneMinLotAreaM2"),
            "zoneClass": props["zoneClass"],
            "residential": props["residential"],
            "lotAreaM2": props.get("lotAreaM2"),
            "heritageStatus": props["heritageStatus"],
            "buildingCoverageRatio": props["buildingCoverageRatio"],
            "cornerLot": props["cornerLot"],
            "lotAspectRatio": props["lotAspectRatio"],
            "distSubwayM": props["distSubwayM"],
            "distSubwayStreetcarM": props["distSubwayStreetcarM"],
            "distStreetcarM": props.get("distStreetcarM", props["distSubwayStreetcarM"]),
            "distBusM": props.get("distBusM", -1),
            "abutsLaneway": props.get("abutsLaneway", False),
            "nearRapidToCorridor": props.get("nearRapidToCorridor", False),
            "inFloodingStudyArea": props.get("inFloodingStudyArea", False),
            "inRegulatedArea": props.get("inRegulatedArea", False),
            "permitsRecentCount": (props.get("permits") or {}).get("recentCount", 0),
            "permitsRecentValueTotal": (props.get("permits") or {}).get("recentValueTotal", 0),
            "permitsRecentMostRecentDate": (props.get("permits") or {}).get("recentMostRecentDate"),
            "permitsDenominatorSource": (props.get("permits") or {}).get("denominatorSource", "no_joined_permits"),
            "nbPermitMedianCostPerUnit": (props.get("neighborhoodPermitComp") or {}).get("medianCostPerUnit"),
            "nbPermitSampleSize": (props.get("neighborhoodPermitComp") or {}).get("sampleSize", 0),
            "neighborhoodCanopyPct": props.get("neighborhoodCanopyPct"),
            "streetTreeCount": props.get("streetTreeCount", 0),
            "matureTreeCount": props.get("matureTreeCount", 0),
            "distBikeLaneM": props.get("distBikeLaneM"),
            "sixplexEligible": props.get("sixplexEligible", False),
            "solarScore": props["solarScore"],
            "solarScoreRaw": props["solarScoreRaw"],
            "solarShadowQuality": props["solarShadowQuality"],
            "postwarNeighborhood": props["postwarNeighborhood"],
            # ── Architect / dev panel — flattened from nested GeoJSON props.
            # `.get` chains use `or {}` for forward-compat: pre-2026-05-05
            # GeoJSON files (built before these fields existed) project to
            # None gracefully instead of KeyError'ing the whole projection.
            "lotLongAxisM": (props.get("lotGeometry") or {}).get("longAxisM"),
            "lotShortAxisM": (props.get("lotGeometry") or {}).get("shortAxisM"),
            "lotOrientationDeg": (props.get("lotGeometry") or {}).get("orientationDeg"),
            "neighborHeightNAvgM": (props.get("neighborHeights") or {}).get("nAvgM"),
            "neighborHeightSAvgM": (props.get("neighborHeights") or {}).get("sAvgM"),
            "neighborHeightEAvgM": (props.get("neighborHeights") or {}).get("eAvgM"),
            "neighborHeightWAvgM": (props.get("neighborHeights") or {}).get("wAvgM"),
            "existingMaxBuildingHeightM": props.get("existingMaxBuildingHeightM"),
            "solarYieldKwhPerYr": props.get("solarYieldKwhPerYr"),
            "pvCapacityKwEstimate": props.get("pvCapacityKwEstimate"),
            "sixplexBonusValueCad": props.get("sixplexBonusValueCad"),
        })
    return rows, total


def make_payload(features, top_n, *, total_citywide: int | None = None):
    """Build the projected payload.

    `total_citywide` is the count of *every* parcel ever processed by the
    ETL (citywide), not just the ones that scored. The frontend uses it
    for the "filtered from N citywide" copy. Defaults to None when the
    caller doesn't have it; the validator accepts both shapes.
    """
    rows, total = project_features(features, top_n)
    payload = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totalAvailable": total,
        "topN": min(top_n, total),
        "rows": rows,
    }
    if total_citywide is not None:
        payload["totalParcels"] = int(total_citywide)
    return payload


def validate(payload):
    """Raise ValueError on any shape violation."""
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict, got {type(payload).__name__}")
    for key in PAYLOAD_KEYS:
        if key not in payload:
            raise ValueError(f"payload missing top-level key: {key!r}")
    if not isinstance(payload["rows"], list):
        raise ValueError("rows must be a list")
    if not payload["rows"]:
        raise ValueError("rows must be a non-empty list")
    for i, row in enumerate(payload["rows"]):
        if not isinstance(row, dict):
            raise ValueError(f"rows[{i}] is not a dict")
        for key in ROW_KEYS:
            if key not in row:
                raise ValueError(f"rows[{i}] missing key: {key!r}")


def _www_data_gid():
    try:
        return grp.getgrnam("www-data").gr_gid
    except KeyError:
        return None


def write_atomic(payload, out_path):
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
            json.dump(payload, fp, ensure_ascii=False,
                      separators=(",", ":"), sort_keys=False)
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
