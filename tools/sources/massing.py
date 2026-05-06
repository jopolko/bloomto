"""Toronto 3D Massing source — building footprints + heights for shadow analysis.

Per Task 3 of the parcel-multiplex-readiness spec, the CKAN dataset ships annual
Shapefile snapshots in 19 resources; we use the 2025 vintage (the latest, no
2024 snapshot exists). See `tools/README.md` § 3D Massing Source for the
trade-off vs. the Multipatch sibling and the **EPSG:3857-trap** the filename
hides.

Loaded data is **ETL-only** — the 3D footprints + heights inform per-parcel
`solarScore` shadow attenuation in `tools/shadow_analysis.py` and never reach
the wire format. The browser sees a single shadow-adjusted scalar per parcel,
not the underlying geometry. (Per CLAUDE.md: "the 'no 3D' rule is about the
wire format and the browser, not the workstation ETL.")

Geometry is reprojected from EPSG:3857 (Web Mercator metres) → EPSG:4326
on ingest so the resulting polygons align with parcels (already in WGS84).
Without this reprojection, every shadow query would silently miss because the
coordinate magnitudes don't overlap.
"""

import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path

import shapefile  # pyshp
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from . import _http

PACKAGE_ID = "3d-massing"
RESOURCE_ID = "667237d6-4d3c-4cf3-8cb7-e91c48d59375"
RESOURCE_URL = (
    "https://ckan0.cf.opendata.inter.prod-toronto.ca/dataset/"
    "387b2e3b-2a76-4199-8b3b-0b7d22e2ec10/resource/"
    f"{RESOURCE_ID}/download/3dmassingshapefile_2025_wgs84.zip"
)
CACHE_FILENAME = "massing.shp.zip"

# Field names per the README_Metadata.xlsx that ships inside the SHP zip.
# Note: the metadata spreadsheet has typos (`SURV_ELEV` for actual `SURF_ELEV`)
# and lists fields that don't appear in the .dbf (`OBJECTID`, `SHAPE_AREA`).
# Trust the .dbf, not the spreadsheet — Task 3's README investigation enumerates
# the 9 fields actually present.
MIN_HEIGHT_FIELD = "MIN_HEIGHT"
MAX_HEIGHT_FIELD = "MAX_HEIGHT"
AVG_HEIGHT_FIELD = "AVG_HEIGHT"

_log = logging.getLogger(__name__)


@dataclass
class Building:
    """One 3D Massing record, reprojected to WGS84.

    `geometry` is the building footprint polygon (per-vertex Z dropped — we
    use the scalar height fields, not vertex Z which is uniformly 0 in the
    2D Shapefile resource). `height_m` is `None` when both `MAX_HEIGHT` and
    `AVG_HEIGHT` are 0 or missing — `tools/shadow_analysis.py` uses this to
    classify the building as Tier 3 ("unavailable") at parcel time.
    """
    geometry: BaseGeometry
    height_m: float | None


def _ensure_cached(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / CACHE_FILENAME
    if cached.exists() and cached.stat().st_size > 0:
        _log.info("using cached %s", cached)
        return cached
    _log.info("downloading 3D Massing SHP zip (~81 MB) → %s", cached)
    _http.download_with_retries(RESOURCE_URL, cached)
    return cached


def _resolve_height(record_dict: dict) -> float | None:
    """Pick the best non-zero height from the source's three height fields.

    Source quirks (Task 3 sample): a small fraction of records have
    `MAX_HEIGHT == 0.0` despite a non-zero `AVG_HEIGHT`. Treat MAX as primary
    and fall back to AVG; if both are 0/missing, return None.
    """
    def _val(key: str) -> float:
        v = record_dict.get(key)
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    max_h = _val(MAX_HEIGHT_FIELD)
    if max_h > 0:
        return max_h
    avg_h = _val(AVG_HEIGHT_FIELD)
    if avg_h > 0:
        return avg_h
    return None


def _open_zip_streams(zip_path: Path) -> tuple[io.BytesIO, io.BytesIO, io.BytesIO]:
    """Materialize the .shp / .shx / .dbf bytes from the zip into BytesIO streams.

    pyshp needs seekable file-likes; `zipfile.ZipFile.open()` returns
    non-seekable streams, so we read each member fully. Peak memory: ~300 MB
    for the 2025 vintage (210 MB SHP + 80 MB DBF + 3 MB SHX). After
    `Reader.iterShapeRecords()` finishes, the BytesIOs go out of scope.
    """
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        shp_name = next(
            (n for n in names if n.lower().endswith(".shp") and not n.lower().endswith(".shp.xml")),
            None,
        )
        if shp_name is None:
            raise RuntimeError(f"massing: no .shp file inside {zip_path}; got {names!r}")
        base = shp_name[:-4]

        def _find(ext: str) -> str:
            for cand in (base + ext, base + ext.upper()):
                if cand in names:
                    return cand
            raise RuntimeError(f"massing: no {ext} file inside {zip_path}")

        shp_buf = io.BytesIO(z.read(shp_name))
        shx_buf = io.BytesIO(z.read(_find(".shx")))
        dbf_buf = io.BytesIO(z.read(_find(".dbf")))
    return shp_buf, shx_buf, dbf_buf


def load_massing_index(cache_dir: Path) -> tuple[STRtree, list[Building]]:
    """Load + reproject the 2025 3D Massing snapshot.

    Returns `(STRtree, buildings)` where `tree.geometries[i]` is the polygon
    of `buildings[i].geometry` (parallel arrays). Geometries are WGS84.
    Records with degenerate footprints (< 3 unique vertices) are dropped and
    counted in the log line; records with no usable height keep their geometry
    and carry `height_m = None` for downstream Tier 2/3 classification.
    """
    zip_path = _ensure_cached(Path(cache_dir))
    shp_buf, shx_buf, dbf_buf = _open_zip_streams(zip_path)
    transformer = Transformer.from_crs(3857, 4326, always_xy=True)

    reader = shapefile.Reader(shp=shp_buf, shx=shx_buf, dbf=dbf_buf)
    field_names = [f[0] for f in reader.fields[1:]]  # skip DeletionFlag

    buildings: list[Building] = []
    seen = 0
    skipped_degenerate = 0
    with_footprint = 0
    with_height = 0

    for shape_rec in reader.iterShapeRecords():
        seen += 1
        shape = shape_rec.shape
        if not shape.points or len(shape.points) < 3:
            skipped_degenerate += 1
            continue

        xs = [p[0] for p in shape.points]
        ys = [p[1] for p in shape.points]
        try:
            lons, lats = transformer.transform(xs, ys)
        except Exception:
            skipped_degenerate += 1
            continue

        try:
            polygon = Polygon(zip(lons, lats))
            if polygon.is_empty or not polygon.is_valid:
                # Try a buffer(0) cleanup for self-intersecting rings; if it
                # still fails, drop the record.
                polygon = polygon.buffer(0)
                if polygon.is_empty:
                    skipped_degenerate += 1
                    continue
        except Exception:
            skipped_degenerate += 1
            continue

        with_footprint += 1
        record_dict = dict(zip(field_names, list(shape_rec.record)))
        height_m = _resolve_height(record_dict)
        if height_m is not None:
            with_height += 1

        buildings.append(Building(geometry=polygon, height_m=height_m))

    geoms = [b.geometry for b in buildings]
    tree = STRtree(geoms)

    with_both = sum(1 for b in buildings if b.height_m is not None)  # geometry already present
    _log.info(
        "massing: %d records loaded (%d with usable footprint, %d with height, %d with both); "
        "skipped %d degenerate geometries",
        seen, with_footprint, with_height, with_both, skipped_degenerate,
    )

    return tree, buildings
