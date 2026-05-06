"""Validate the assembled payload and write data/neighborhoods.json atomically.

`write_atomic` writes to a temp file in the destination's parent directory
(so `os.replace` is POSIX-atomic on a single filesystem), then renames into
place only if validation passes. The on-disk file is never partially written
or replaced by something invalid.
"""

import grp
import json
import os
import tempfile
from pathlib import Path

ENTRY_KEYS = (
    "name", "lat", "lng", "score",
    "heatPump", "canopy", "walk", "transit", "bike",
    "builtYear", "existing", "potential",
)
META_KEYS = ("generatedAt", "sourceVersions", "scoreFormula", "fallbacks")
DEFAULT_NEIGHBORHOOD_COUNT = 158


def validate(payload, *, expected_count: int = DEFAULT_NEIGHBORHOOD_COUNT) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict, got {type(payload).__name__}")

    for key in ("meta", "neighborhoods"):
        if key not in payload:
            raise ValueError(f"payload missing top-level key: {key!r}")

    meta = payload["meta"]
    if not isinstance(meta, dict):
        raise ValueError("meta must be a dict")
    for key in META_KEYS:
        if key not in meta:
            raise ValueError(f"meta missing key: {key!r}")

    neighborhoods = payload["neighborhoods"]
    if not isinstance(neighborhoods, list):
        raise ValueError("neighborhoods must be a list")
    if len(neighborhoods) != expected_count:
        raise ValueError(
            f"neighborhoods has {len(neighborhoods)} entries, expected {expected_count}"
        )

    fallbacks = meta["fallbacks"]
    if not isinstance(fallbacks, dict):
        raise ValueError("meta.fallbacks must be a dict")

    for i, entry in enumerate(neighborhoods):
        if not isinstance(entry, dict):
            raise ValueError(f"neighborhoods[{i}] is not a dict")
        for key in ENTRY_KEYS:
            if key not in entry:
                raise ValueError(f"neighborhoods[{i}] (name={entry.get('name')!r}) missing key: {key!r}")


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
            json.dump(payload, fp, ensure_ascii=False, indent=2, sort_keys=False)
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
