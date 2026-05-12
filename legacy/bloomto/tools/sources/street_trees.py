"""Toronto Street Tree General — per-parcel mature-canopy signal.

City of Toronto's "Street Tree Data" CKAN dataset is a per-tree point cloud
of every city-owned street tree (~700K points), each carrying its botanical
name and DBH_TRUNK (diameter at breast height, in cm). For BloomTO's
parcel-level developer use case, DBH ≥ 30 cm matters because:

- Toronto's Tree Bylaw (Municipal Code Chapter 813) makes any tree of DBH
  ≥ 30 cm on private property a permit gate for removal — a developer
  intending to demolish the existing structure typically needs a Section 7
  permit to remove a regulated street tree adjacent to the work site.
- City-owned trees with DBH ≥ 40 cm on city land qualify as "heritage trees"
  with stronger protection.

We cache the 343 MB GeoJSON once, stream-parse via ijson (no full load), and
emit a STRtree of points + a parallel DBH list. `build_parcels.py` queries
trees inside the parcel polygon's expanded bounding box, counts those within
a 6-metre buffer of the polygon (close enough to be the parcel's "frontage
canopy"), and emits two per-parcel fields:
  - streetTreeCount: total trees on/adjacent to the lot
  - matureTreeCount: subset with DBH ≥ 30 cm (Tree Bylaw protection)

This is a TRUE per-parcel canopy signal, not the dissolved-by-neighborhood
proxy from the FLC dataset. Private-side trees are not in this feed (Toronto
Open Data only inventories city-owned trees), so a parcel with 0 hits may
still have private mature canopy — the field is "city-side mature canopy
on/near the lot," not "all canopy."
"""

import logging
from pathlib import Path
from typing import NamedTuple

import ijson
from shapely.geometry import Point
from shapely.strtree import STRtree

from . import _http

CACHE_FILENAME = "street_trees.geojson"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "6ac4569e-fd37-4cbc-ac63-db3624c5f6a2/resource/"
    "d6089672-bdf7-4857-8ea8-90da826fcfa1/download/"
    "street-tree-data-4326.geojson"
)

# Toronto Tree Bylaw protection trigger (Municipal Code Ch. 813). Trees with
# DBH at or above this value require permits for removal on private property
# adjacent to the work site.
MATURE_DBH_CM = 30

_log = logging.getLogger(__name__)


class StreetTreeIndex(NamedTuple):
    tree: STRtree
    points: list[Point]
    dbh_cm: list[int]   # 1:1 with points; 0 when source value missing


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading Street Tree Data GeoJSON (~343 MB) → %s", cached)
    _http.download_with_retries(RESOURCE_URL, cached)
    return cached


def compute_street_trees(cache_dir: Path) -> StreetTreeIndex:
    """Stream-parse the GeoJSON into an STRtree + parallel DBH list."""
    path = _ensure_cached(Path(cache_dir))
    points: list[Point] = []
    dbh_cm: list[int] = []
    skipped_geom = 0
    skipped_dbh = 0
    with path.open("rb") as fp:
        for feat in ijson.items(fp, "features.item"):
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates")
            # Source uses MultiPoint with one coordinate per feature; flatten
            # that to a single Point (the point of interest is unambiguous).
            if geom.get("type") == "MultiPoint" and coords:
                lon, lat = coords[0][0], coords[0][1]
            elif geom.get("type") == "Point" and coords:
                lon, lat = coords[0], coords[1]
            else:
                skipped_geom += 1
                continue
            try:
                pt = Point(float(lon), float(lat))
            except (TypeError, ValueError):
                skipped_geom += 1
                continue
            props = feat.get("properties") or {}
            try:
                dbh = int(float(props.get("DBH_TRUNK") or 0))
            except (TypeError, ValueError):
                dbh = 0
                skipped_dbh += 1
            points.append(pt)
            dbh_cm.append(dbh)
    _log.info(
        "street_trees: %d trees indexed (%d mature ≥%dcm DBH; skipped: geom=%d dbh=%d)",
        len(points),
        sum(1 for d in dbh_cm if d >= MATURE_DBH_CM),
        MATURE_DBH_CM,
        skipped_geom, skipped_dbh,
    )
    return StreetTreeIndex(tree=STRtree(points), points=points, dbh_cm=dbh_cm)


def count_for_parcel(parcel_geom, idx: StreetTreeIndex,
                     buffer_deg: float = 5.4e-5) -> tuple[int, int]:
    """Return (streetTreeCount, matureTreeCount) for one parcel.

    `buffer_deg = 5.4e-5` ≈ 6 metres at Toronto's latitude — captures street
    trees on the immediate curb fronting the parcel without bleeding into
    next-door lots' contributions.
    """
    buffered = parcel_geom.buffer(buffer_deg)
    total = 0
    mature = 0
    for i in idx.tree.query(buffered):
        if buffered.contains(idx.points[i]):
            total += 1
            if idx.dbh_cm[i] >= MATURE_DBH_CM:
                mature += 1
    return total, mature
