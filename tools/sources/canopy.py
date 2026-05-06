"""Compute `canopy` (tree-cover share, expressed 0–100) per neighborhood from
Toronto's 2018 Tree Canopy Study File Geodatabase.

The dataset arrives as a vector File Geodatabase whose 1,812 multipolygons are
already pre-dissolved by (source-hood × land-cover class). Each polygon carries
`Shape_Area` in the source CRS (NAD83(CSRS) / MTM zone 10, metres) — the exact
area of the patch — so we aggregate in vector form rather than rasterizing.

The intermediate cache is a small WGS84 GeoJSON of *just* one representative
point per source polygon plus its `gridcode` and `Shape_Area`. Full geometries
balloon to ~4 GB as GeoJSON because of high vertex counts on multi-part polygons
and aren't needed for hood attribution: a representative point + the precomputed
m² is enough for `tree_m² / total_landcover_m² × 100` per canonical neighborhood.

See `tools/README.md` § Canopy Source for dataset URLs, the gridcode table, and
the rationale for the vector route over rasterization.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests
from shapely.geometry import shape
from shapely.strtree import STRtree

from .neighborhoods import Neighborhood

PACKAGE_ID = "forest-and-land-cover"
RESOURCE_ID = "69419e11-2dfa-4bcc-bed0-43a9dd2d0973"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "61642048-56bb-4050-b7c3-f569fcf94527/resource/"
    f"{RESOURCE_ID}/download/landcover2018_gdb.zip"
)
ZIP_CACHE = "canopy_gdb.zip"
GDB_DIR = "LandCover2018.gdb"
LAYER_NAME = "LandCover2018"
GEOJSON_CACHE = "canopy_centroids_4326.geojson"

# OpenFileGDB needs PROJ_DATA pointed at a real proj.db for `-t_srs` to work.
# The conda-forge GDAL install we use ships its own copy.
PROJ_DATA_DEFAULT = "/opt/micromamba/envs/gdal/share/proj"

GRIDCODE_FIELD = "gridcode"
TREE_GRIDCODE = 1
SHAPE_AREA_FIELD = "Shape_Area"

# Toronto-wide canopy share is ~28%; use 30 as a neutral fallback for hoods that
# end up with zero attributed land-cover area (shouldn't happen — citywide tiling —
# but defends against future boundary changes).
FALLBACK_CANOPY = 30

_log = logging.getLogger(__name__)


def _download_with_retries(url: str, dest: Path) -> None:
    backoffs = (0.5, 1.0, 2.0)
    for attempt in range(len(backoffs) + 1):
        try:
            with requests.get(url, stream=True, timeout=600) as r:
                r.raise_for_status()
                with dest.open("wb") as fp:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
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


def _ensure_gdb(cache_dir: Path) -> Path:
    gdb_path = cache_dir / GDB_DIR
    if gdb_path.is_dir() and any(gdb_path.iterdir()):
        _log.info("using cached %s", gdb_path)
        return gdb_path

    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / ZIP_CACHE
    if not (zip_path.exists() and zip_path.stat().st_size > 0):
        _log.info("downloading canopy GDB zip (~436 MB) → %s", zip_path)
        _download_with_retries(RESOURCE_URL, zip_path)

    _log.info("unzipping %s", zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(cache_dir)

    if not gdb_path.is_dir():
        raise RuntimeError(f"expected {gdb_path} after unzip, missing")
    return gdb_path


def _ensure_extracted_geojson(cache_dir: Path) -> Path:
    out_path = cache_dir / GEOJSON_CACHE
    if out_path.exists() and out_path.stat().st_size > 0:
        _log.info("using cached %s", out_path)
        return out_path

    gdb_path = _ensure_gdb(cache_dir)

    if not shutil.which("ogr2ogr"):
        raise RuntimeError(
            "ogr2ogr not on PATH. Install GDAL ≥ 3.7 (e.g. via conda-forge) and "
            "ensure `ogr2ogr` resolves; canopy ETL needs it once to extract the "
            "FileGDB to WGS84 GeoJSON."
        )

    env = dict(os.environ)
    env.setdefault("PROJ_DATA", PROJ_DATA_DEFAULT)

    _log.info("extracting (gridcode, Shape_Area, ST_PointOnSurface) from %s → %s "
              "(one-time, ~1-2 min)",
              gdb_path.name, out_path.name)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    try:
        subprocess.run(
            [
                "ogr2ogr",
                "-t_srs", "EPSG:4326",
                "-dialect", "SQLite",
                "-sql", (
                    "SELECT gridcode, Shape_Area, "
                    f"ST_PointOnSurface(Shape) AS geom FROM {LAYER_NAME}"
                ),
                "-nlt", "POINT",
                "-f", "GeoJSON",
                str(tmp_path),
                str(gdb_path),
            ],
            check=True,
            env=env,
        )
        os.replace(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

    return out_path


def compute_canopy(neighborhoods: list[Neighborhood], cache_dir: Path
                   ) -> tuple[dict[str, int], list[str]]:
    """Returns `(canopy_by_name, fallback_names)`. Per neighborhood: percentage of
    its classified land cover that's tree (gridcode=1), rounded to 0–100. Source
    areas are taken from the dataset's `Shape_Area` field (m² in MTM zone 10);
    polygons are attributed to a canonical neighborhood by representative point.
    Hoods with zero attributed area fall back to `canopy = FALLBACK_CANOPY` and
    appear in `fallback_names`.
    """
    cache = Path(cache_dir)
    geojson_path = _ensure_extracted_geojson(cache)

    _log.info("loading canopy centroids from %s", geojson_path.name)
    with geojson_path.open(encoding="utf-8") as fp:
        data = json.load(fp)
    features = data.get("features") or []
    _log.info("canopy: %d source polygons (one centroid each)", len(features))

    polygons = [n.polygon for n in neighborhoods]
    nb_tree = STRtree(polygons)

    tree_m2 = [0.0] * len(neighborhoods)
    total_m2 = [0.0] * len(neighborhoods)
    attributed = 0
    no_neighborhood = 0
    skipped_geom = 0
    skipped_props = 0

    for feat in features:
        geom = feat.get("geometry")
        if not geom or geom.get("type") != "Point":
            skipped_geom += 1
            continue
        try:
            pt = shape(geom)
        except Exception:
            skipped_geom += 1
            continue

        props = feat.get("properties") or {}
        area = props.get(SHAPE_AREA_FIELD)
        gridcode = props.get(GRIDCODE_FIELD)
        if area is None or gridcode is None:
            skipped_props += 1
            continue

        for ni in nb_tree.query(pt):
            if polygons[ni].contains(pt):
                total_m2[ni] += area
                if gridcode == TREE_GRIDCODE:
                    tree_m2[ni] += area
                attributed += 1
                break
        else:
            no_neighborhood += 1

    _log.info(
        "canopy: %d attributed, %d no-neighborhood, %d skipped-geom, %d skipped-props",
        attributed, no_neighborhood, skipped_geom, skipped_props,
    )

    canopy_by_name: dict[str, int] = {}
    fallback_names: list[str] = []
    for nb, tree_a, total_a in zip(neighborhoods, tree_m2, total_m2):
        if total_a <= 0:
            canopy_by_name[nb.name] = FALLBACK_CANOPY
            fallback_names.append(nb.name)
            continue
        pct = round(100 * tree_a / total_a)
        canopy_by_name[nb.name] = max(0, min(100, pct))

    _log.info("canopy: %d fallback names (zero-area neighborhoods)",
              len(fallback_names))
    return canopy_by_name, fallback_names


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    from .neighborhoods import fetch_neighborhoods
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tools/cache")
    nbs = fetch_neighborhoods(cache)
    canopy, fb = compute_canopy(nbs, cache)
    top = sorted(canopy.items(), key=lambda kv: -kv[1])[:5]
    bottom = sorted(canopy.items(), key=lambda kv: kv[1])[:5]
    print(f"canopy: {len(canopy)} entries")
    print(f"  top 5:    {top}")
    print(f"  bottom 5: {bottom}")
    print(f"fallbacks: {len(fb)} {fb[:5]}{'...' if len(fb) > 5 else ''}")
