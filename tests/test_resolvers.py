from __future__ import annotations

import unittest

from trawl.archives.resolvers import (
    UnsupportedHost,
    describe_host,
    name_from_url,
    resolve_candidates,
    scrape_archive_url,
    unsupported_extension_message,
)


class ResolverTests(unittest.TestCase):
    def test_pixeldrain_page_becomes_api_link(self):
        candidates = resolve_candidates("https://pixeldrain.com/u/AbC123")
        self.assertEqual(
            candidates[0], "https://pixeldrain.com/api/file/AbC123?download"
        )

    def test_dropbox_adds_download_flag(self):
        candidates = resolve_candidates(
            "https://www.dropbox.com/s/abc/file.zip?dl=0"
        )
        self.assertIn("dl=1", candidates[0])

    def test_buzzheavier_download_variant(self):
        candidates = resolve_candidates("https://buzzheavier.com/f/abc")
        self.assertEqual(candidates[-1], "https://buzzheavier.com/f/abc/download")

    def test_unsupported_hosts(self):
        for url in (
            "https://drive.google.com/file/d/abc/view",
            "https://mega.nz/file/abc",
        ):
            with self.assertRaises(UnsupportedHost):
                resolve_candidates(url)

    def test_unsupported_extensions(self):
        self.assertIn(
            "7z", unsupported_extension_message("https://example.com/a.7z") or ""
        )
        self.assertIsNone(unsupported_extension_message("https://example.com/a.zip"))

    def test_scrape_direct_link(self):
        page = (
            '<html><a href="/downloads/My%20Book.zip">Get it</a>'
            '<a href="https://cdn.example.org/file.rar">mirror</a></html>'
        )
        target = scrape_archive_url(page, "https://example.com/page")
        self.assertEqual(target, "https://example.com/downloads/My%20Book.zip")

    def test_describe_and_name(self):
        self.assertEqual(describe_host("https://www.example.com/a"), "example.com")
        self.assertEqual(
            name_from_url("https://example.com/Sample%20Project.zip"),
            "Sample Project.zip",
        )


if __name__ == "__main__":
    unittest.main()
