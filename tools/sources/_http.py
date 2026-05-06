"""Shared download helper for v1.2 source modules.

`download_with_retries` streams an HTTP response to a temp file beside the
destination, then atomically renames into place via `os.replace`. If the
network drops mid-stream or the request fails after all retries, the temp
file is unlinked and `dest` is left untouched (or absent, if it didn't
already exist) — never partially overwritten.

The eight v1.1 source modules each carry their own copy of the verbatim
retry pattern (see e.g. `neighborhoods.py:_download_with_retries`). This
helper is the lifted form. v1.2 modules (`heritage.py`, `building_outlines.py`,
`massing.py`) consume it directly. The v1.1 duplicates stay put for this
release — they're slated for cleanup in v1.3 and refactoring them now would
require re-running the 39-test suite for no v1.2 benefit.
"""

import logging
import os
import tempfile
import time
from pathlib import Path

import requests

_log = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024


def download_with_retries(
    url: str,
    dest: Path,
    *,
    timeout: int = 60,
    backoffs: tuple = (0.5, 1.0, 2.0),
) -> None:
    """Stream `url` to `dest` with exponential-backoff retries and atomic write.

    Total attempts = `len(backoffs) + 1` (one initial + one per backoff).
    On success, `dest` exists with the full body and no `*.tmp` siblings.
    On final failure, raises the underlying `requests.RequestException` /
    `OSError` and leaves `dest` unchanged.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    total_attempts = len(backoffs) + 1
    last_exc: BaseException | None = None

    for attempt in range(total_attempts):
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=dest.parent,
            prefix=dest.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "wb") as fp:
                with requests.get(url, stream=True, timeout=timeout) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=_CHUNK_SIZE):
                        if chunk:
                            fp.write(chunk)
            os.replace(tmp_name, dest)
            return
        except (requests.RequestException, OSError) as e:
            last_exc = e
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

            if attempt == total_attempts - 1:
                break
            wait = backoffs[attempt]
            _log.warning(
                "download %s failed (attempt %d/%d): %s — retrying in %ss",
                url, attempt + 1, total_attempts, e, wait,
            )
            time.sleep(wait)

    assert last_exc is not None
    raise last_exc
