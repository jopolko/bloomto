import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from tools.sources._http import download_with_retries


class _MockResponse:
    """Stand-in for `requests.get(..., stream=True)` returning bytes in chunks."""

    def __init__(self, *, status_code: int = 200, body: bytes = b"", chunk_size: int = 1024):
        self.status_code = status_code
        self._body = body
        self._chunk_size = chunk_size

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} Server Error")
            err.response = self
            raise err

    def iter_content(self, chunk_size: int = 64 * 1024):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class _Sequence:
    """Callable that returns scripted responses on successive calls."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


class DownloadWithRetriesTests(unittest.TestCase):
    def test_success_first_try(self):
        body = b"hello, world\n" * 10_000  # ~130 KB to exercise multi-chunk write
        seq = _Sequence(_MockResponse(body=body))
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.bin"
            with patch("tools.sources._http.requests.get", seq):
                download_with_retries("http://example/x", dest)
            self.assertEqual(seq.calls, 1)
            self.assertEqual(dest.read_bytes(), body)
            self.assertEqual(list(dest.parent.glob("*.tmp")), [])

    def test_retries_then_succeeds(self):
        seq = _Sequence(
            _MockResponse(status_code=503),
            _MockResponse(status_code=503),
            _MockResponse(body=b"ok"),
        )
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.bin"
            with patch("tools.sources._http.requests.get", seq), \
                 patch("tools.sources._http.time.sleep") as sleep_mock:
                download_with_retries("http://example/x", dest, backoffs=(0.01, 0.02, 0.04))
            self.assertEqual(seq.calls, 3)
            self.assertEqual(dest.read_bytes(), b"ok")
            self.assertEqual(list(dest.parent.glob("*.tmp")), [])
            self.assertEqual([c.args[0] for c in sleep_mock.call_args_list], [0.01, 0.02])

    def test_raises_after_all_retries_exhausted(self):
        # 4 attempts total = 1 initial + 3 backoffs
        seq = _Sequence(
            _MockResponse(status_code=503),
            _MockResponse(status_code=503),
            _MockResponse(status_code=503),
            _MockResponse(status_code=503),
        )
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.bin"
            with patch("tools.sources._http.requests.get", seq), \
                 patch("tools.sources._http.time.sleep"):
                with self.assertRaises(requests.HTTPError):
                    download_with_retries("http://example/x", dest, backoffs=(0.01, 0.01, 0.01))
            self.assertEqual(seq.calls, 4)
            self.assertFalse(dest.exists())
            self.assertEqual(list(dest.parent.glob("*.tmp")), [])

    def test_connection_error_is_retried(self):
        # Mix transport-level errors with eventual success.
        seq = _Sequence(
            requests.ConnectionError("dropped"),
            _MockResponse(body=b"recovered"),
        )
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.bin"
            with patch("tools.sources._http.requests.get", seq), \
                 patch("tools.sources._http.time.sleep"):
                download_with_retries("http://example/x", dest, backoffs=(0.01, 0.01))
            self.assertEqual(seq.calls, 2)
            self.assertEqual(dest.read_bytes(), b"recovered")

    def test_no_partial_file_on_failure(self):
        # A pre-existing dest must not be overwritten when all attempts fail.
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.bin"
            dest.write_bytes(b"existing-content")
            seq = _Sequence(
                _MockResponse(status_code=500),
                _MockResponse(status_code=500),
            )
            with patch("tools.sources._http.requests.get", seq), \
                 patch("tools.sources._http.time.sleep"):
                with self.assertRaises(requests.HTTPError):
                    download_with_retries("http://example/x", dest, backoffs=(0.01,))
            self.assertEqual(dest.read_bytes(), b"existing-content")
            self.assertEqual(list(dest.parent.glob("*.tmp")), [])

    def test_creates_parent_dir(self):
        seq = _Sequence(_MockResponse(body=b"data"))
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "deeply" / "nested" / "out.bin"
            with patch("tools.sources._http.requests.get", seq):
                download_with_retries("http://example/x", dest)
            self.assertTrue(dest.parent.is_dir())
            self.assertEqual(dest.read_bytes(), b"data")

    def test_chunked_body_roundtrip(self):
        body = bytes(range(256)) * 1000  # 256 KB across chunk boundaries
        seq = _Sequence(_MockResponse(body=body, chunk_size=37))  # awkward chunk size
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "out.bin"
            with patch("tools.sources._http.requests.get", seq):
                download_with_retries("http://example/x", dest)
            self.assertEqual(dest.read_bytes(), body)


if __name__ == "__main__":
    unittest.main()
