from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trawl.net import (
    HTTPRangeReader,
    HTTPStatusError,
    RangeUnsupported,
    fetch_page,
    filename_from_info,
    normalize_url,
)

from support import RangeRequestHandler, serve


class UrlTests(unittest.TestCase):
    def test_adds_https(self):
        self.assertEqual(normalize_url("example.com/a.zip"), "https://example.com/a.zip")

    def test_rejects_other_schemes(self):
        with self.assertRaises(ValueError):
            normalize_url("ftp://example.com/a.zip")

    def test_filename_from_disposition(self):
        name = filename_from_info(
            "https://example.com/123", 'attachment; filename="holiday.zip"'
        )
        self.assertEqual(name, "holiday.zip")


class RangeReaderTests(unittest.TestCase):
    def test_probe_and_range_reads(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            payload = bytes(range(256)) * 40
            (directory / "blob.bin").write_bytes(payload)
            with serve(directory) as base:
                with HTTPRangeReader(f"{base}/blob.bin", {}) as reader:
                    info = reader.probe()
                    self.assertEqual(info.size, len(payload))
                    self.assertEqual(info.filename, "blob.bin")
                    self.assertTrue(info.accepts_ranges)
                    self.assertEqual(reader.read_range(0, 16), payload[:16])
                    self.assertEqual(reader.read_range(9990, 40), payload[9990:10030])
                    chunks = list(reader.iter_range(10, 700, chunk_size=100))
                    self.assertEqual(b"".join(chunks), payload[10:710])

    def test_custom_headers_are_sent(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "a.bin").write_bytes(b"abcdef")
            with serve(directory) as base:
                handler_requests = []
                # serve() installs a fresh handler class; attach to the server's
                # class through the running thread instead of global state.
                with HTTPRangeReader(
                    f"{base}/a.bin", {"Referer": "https://example.org/page"}
                ) as reader:
                    reader.probe()
                    reader.read_range(0, 2)
                # The base handler records on its class; subclasses inherit it.
                handler_requests.extend(RangeRequestHandler.requests)
                self.assertTrue(handler_requests)
                self.assertTrue(
                    any(call[2].get("Referer") == "https://example.org/page" for call in handler_requests)
                )

    def test_no_range_support(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "a.bin").write_bytes(b"abcdef" * 100)
            with serve(directory, ignore_ranges=True) as base:
                with HTTPRangeReader(f"{base}/a.bin", {}) as reader:
                    with self.assertRaises(RangeUnsupported) as caught:
                        reader.probe()
                    self.assertEqual(caught.exception.info.size, 600)
                    self.assertFalse(caught.exception.info.accepts_ranges)

    def test_404_is_friendly(self):
        with TemporaryDirectory() as temp:
            with serve(Path(temp)) as base:
                with HTTPRangeReader(f"{base}/missing.zip", {}) as reader:
                    with self.assertRaises(HTTPStatusError) as caught:
                        reader.probe()
                    self.assertEqual(caught.exception.status, 404)

    def test_redirect_and_html_page(self):
        with TemporaryDirectory() as temp:
            with serve(
                Path(temp),
                redirects={"/old.zip": "/page"},
                html_pages={"/page": "<html><body>landing page</body></html>"},
            ) as base:
                with HTTPRangeReader(f"{base}/old.zip", {}) as reader:
                    with self.assertRaises(RangeUnsupported) as caught:
                        reader.probe()
                    self.assertTrue(caught.exception.info.is_html)
                text, final_url = fetch_page(f"{base}/old.zip")
                self.assertIn("landing page", text)
                self.assertTrue(final_url.endswith("/page"))


if __name__ == "__main__":
    unittest.main()
