"""Compute `walk` (street-intersection density, normalized 0–100) per neighborhood from
the Toronto Centreline GeoJSON. See `tools/README.md` § Streets Source for the locked
intersection-derivation approach (5m grid-cell clustering, ≥3 distinct line ids).
"""

import json
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import requests
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from .neighborhoods import Neighborhood
from .zoning import Parcel

PACKAGE_ID = "toronto-centreline-tcl"
RESOURCE_ID = "7bc94ccf-7bcf-4a7d-88b1-bdfc8ec5aaf1"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "1d079757-377b-4564-82df-eb5638583bfb/resource/"
    f"{RESOURCE_ID}/download/centreline-version-2-4326.geojson"
)
CACHE_FILENAME = "centreline.geojson"

# 5×10⁻⁵° ≈ 5m at 43.7°N. TCL segments are clipped at intersections, so true
# intersections always have multiple distinct segments terminating within sub-metre
# distance. 5m is well above digitization noise and well below typical 50–200m
# inter-intersection spacing.
GRID_TOL = 5e-5
# Size ≥ 3 distinct line ids = real T-junction or 4-way intersection (size-2 clusters
# are just digitization breaks in a continuous road segment).
MIN_CLUSTER_SIZE = 3

_log = logging.getLogger(__name__)


def _download_with_retries(url: str, dest: Path) -> None:
    backoffs = (0.5, 1.0, 2.0)
    for attempt in range(len(backoffs) + 1):
        try:
            with requests.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                with dest.open("wb") as fp:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fp.write(chunk)
            return
        except (requests.RequestException, OSError) as e:
            if attempt == len(backoffs):
                raise
            wait = backoffs[attempt]
            _log.warning("download %s failed (attempt %d): %s — retrying in %ss",
                         url, attempt + 1, e, wait)
            time.sleep(wait)


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading Centreline GeoJSON (~89 MB) → %s", cached)
    _download_with_retries(RESOURCE_URL, cached)
    return cached


def _grid_cell(lng: float, lat: float) -> tuple[int, int]:
    return (int(lng / GRID_TOL), int(lat / GRID_TOL))


def _extract_endpoints(geom: dict | None) -> list[tuple[float, float]]:
    """Return [(lng, lat), ...] for the start and end of every line component."""
    if not geom:
        return []
    t = geom.get("type")
    coords = geom.get("coordinates")
    if t == "LineString" and coords:
        return [(coords[0][0], coords[0][1]), (coords[-1][0], coords[-1][1])]
    if t == "MultiLineString" and coords:
        out: list[tuple[float, float]] = []
        for part in coords:
            if part:
                out.append((part[0][0], part[0][1]))
                out.append((part[-1][0], part[-1][1]))
        return out
    return []


def compute_walk(neighborhoods: list[Neighborhood], cache_dir: Path
                 ) -> tuple[dict[str, int], list[str]]:
    """Returns `(walk_by_name, fallback_names)`. Per neighborhood: street-intersection
    count divided by `area_km2` to get density (intersections / km²), scaled to 0–100
    by 95th-percentile normalization. Neighborhoods with zero intersections get
    `walk = 0` and appear in `fallback_names`.
    """
    cached = _ensure_cached(Path(cache_dir))
    _log.info("loading Centreline JSON (%d MB on disk)", cached.stat().st_size // (1 << 20))
    with cached.open(encoding="utf-8") as fp:
        data = json.load(fp)

    cell_to_lines: dict[tuple[int, int], set[int]] = defaultdict(set)
    feats = data.get("features") or []
    for line_id, feat in enumerate(feats):
        for lng, lat in _extract_endpoints(feat.get("geometry")):
            cell_to_lines[_grid_cell(lng, lat)].add(line_id)
    _log.info("centreline: %d features, %d unique grid cells", len(feats), len(cell_to_lines))

    intersection_points: list[Point] = []
    for (gx, gy), lines in cell_to_lines.items():
        if len(lines) >= MIN_CLUSTER_SIZE:
            cx = (gx + 0.5) * GRID_TOL
            cy = (gy + 0.5) * GRID_TOL
            intersection_points.append(Point(cx, cy))
    _log.info("centreline: %d intersection clusters (≥%d distinct lines)",
              len(intersection_points), MIN_CLUSTER_SIZE)

    polygons = [n.polygon for n in neighborhoods]
    tree = STRtree(polygons)
    counts = [0] * len(neighborhoods)
    for pt in intersection_points:
        for idx in tree.query(pt):
            if polygons[idx].contains(pt):
                counts[idx] += 1

    density_by_idx = [
        (c / nb.area_km2) if nb.area_km2 > 0 else 0.0
        for c, nb in zip(counts, neighborhoods)
    ]

    sorted_d = sorted(density_by_idx)
    p95_idx = int(0.95 * (len(sorted_d) - 1))
    p95 = sorted_d[p95_idx]
    if p95 == 0:
        _log.error("p95 intersection density is 0 — every neighborhood falls back")
        scale = 1.0
    else:
        scale = p95

    walk_by_name: dict[str, int] = {}
    fallback_names: list[str] = []
    for nb, c, d in zip(neighborhoods, counts, density_by_idx):
        if c == 0:
            walk_by_name[nb.name] = 0
            fallback_names.append(nb.name)
            continue
        score = round(100 * d / scale)
        walk_by_name[nb.name] = max(0, min(100, score))

    _log.info("centreline: p95 density=%.2f /km²; %d fallback names",
              p95, len(fallback_names))
    return walk_by_name, fallback_names


LANEWAY_FEATURE_CODE = 201700  # Toronto Centreline FEATURE_CODE for "Laneway"


def load_centreline_index(
    cache_dir: Path,
) -> tuple[STRtree, list[int], set[int]]:
    """Load the Centreline GeoJSON as `(STRtree, linear_name_ids, laneway_idx)`.

    Each tree element is a LineString / MultiLineString geometry; the parallel
    list carries the feature's `LINEAR_NAME_ID` (a stable numeric street id —
    chosen over `LINEAR_NAME_FULL` because two segments of the same street
    written with different name spellings still share the ID, so the
    corner-lot "≥2 distinct streets" test is robust to digitization noise).

    `laneway_idx` is a set of tree indices for features whose
    `FEATURE_CODE == 201700` (Toronto's "Laneway" code). Toronto laneways
    DO carry valid `LINEAR_NAME_ID` values (e.g., "Lane N of Bloor btwn
    Spadina & Bathurst"), so the laneway flag must come from FEATURE_CODE,
    not from a missing/negative name id. Used by `_abuts_laneway` in
    `build_parcels.py` to flag parcels whose boundary touches a laneway.
    """
    cached = _ensure_cached(Path(cache_dir))
    with cached.open(encoding="utf-8") as fp:
        data = json.load(fp)
    geoms: list[BaseGeometry] = []
    name_ids: list[int] = []
    laneway_idx: set[int] = set()
    for feat in data.get("features") or []:
        geom_dict = feat.get("geometry")
        if not geom_dict:
            continue
        try:
            geom = shape(geom_dict)
        except Exception:
            continue
        if geom.is_empty:
            continue
        props = feat.get("properties") or {}
        nid = props.get("LINEAR_NAME_ID")
        try:
            nid_int = int(nid) if nid is not None else -1
        except (TypeError, ValueError):
            nid_int = -1
        idx = len(geoms)
        geoms.append(geom)
        name_ids.append(nid_int)
        try:
            if int(props.get("FEATURE_CODE") or 0) == LANEWAY_FEATURE_CODE:
                laneway_idx.add(idx)
        except (TypeError, ValueError):
            pass
    _log.info(
        "centreline: %d centreline geometries indexed (%d laneways flagged)",
        len(geoms), len(laneway_idx),
    )
    return STRtree(geoms), name_ids, laneway_idx


def is_corner_lot(
    parcel: Parcel,
    centreline_tree: STRtree,
    centreline_name_ids: list[int],
    *,
    buffer_deg: float = 2.7e-5,
) -> bool:
    """Single-parcel corner-lot test (used by the streaming parcel ETL).

    See `compute_corner_lots` for the rule semantics. This entry point lets the
    orchestrator stream parcels via `iter_parcels` and decide corner status
    inline, instead of materializing a `dict[parcel_id, bool]` for all ~500k
    parcels up front. Callers must hold the centreline STRtree + parallel
    name-id list in scope (load once via `load_centreline_index`).
    """
    boundary = parcel.geometry.boundary
    if boundary.is_empty:
        return False
    buffered = boundary.buffer(buffer_deg)

    seen_named_ids: set[int] = set()
    for idx in centreline_tree.query(buffered):
        line_geom = centreline_tree.geometries[idx]
        if not line_geom.intersects(buffered):
            continue
        nid = centreline_name_ids[idx]
        if nid < 0:
            continue  # laneway / unnamed — skip per docstring
        seen_named_ids.add(nid)
        if len(seen_named_ids) >= 2:
            return True
    return False


def compute_corner_lots(
    parcels: Iterable[Parcel],
    cache_dir: Path,
    *,
    buffer_deg: float = 2.7e-5,
) -> dict[str, bool]:
    """Return `{parcel_id: is_corner}` based on centreline-touching the parcel.

    `buffer_deg = 2.7e-5` ≈ 3 m at Toronto's latitude — tight enough that
    the buffered parcel boundary touches *adjacent* streets only, not the
    next-block-over road across the way.

    Algorithm: for each parcel, buffer the boundary linestring (not the
    interior — interior buffer would catch streets crossing through the lot,
    not abutting it) by `buffer_deg`, query the centreline STRtree, count
    *distinct* `LINEAR_NAME_ID` values among lines whose geometry intersects
    the buffered boundary. ≥ 2 distinct ids → corner lot. Same-street
    digitization breaks share an ID so they collapse to one.

    Laneway-only access (parcels touched by `LINEAR_NAME_ID = -1` lines only)
    is NOT a corner; the function only counts named-street IDs (≥0).

    Prefer `is_corner_lot` in streaming-orchestrator contexts to avoid
    materializing the full parcels list.
    """
    tree, name_ids, _laneway_idx = load_centreline_index(cache_dir)
    return {
        p.parcel_id: is_corner_lot(p, tree, name_ids, buffer_deg=buffer_deg)
        for p in parcels
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    from .neighborhoods import fetch_neighborhoods
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tools/cache")
    nbs = fetch_neighborhoods(cache)
    wk, fb = compute_walk(nbs, cache)
    top = sorted(wk.items(), key=lambda kv: -kv[1])[:5]
    bottom = sorted(wk.items(), key=lambda kv: kv[1])[:5]
    print(f"walk: {len(wk)} entries")
    print(f"  top 5: {top}")
    print(f"  bottom 5: {bottom}")
    print(f"fallbacks: {len(fb)} {fb[:5]}{'...' if len(fb) > 5 else ''}")
