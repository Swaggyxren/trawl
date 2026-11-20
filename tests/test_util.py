from __future__ import annotations

import unittest
from datetime import datetime

from trawl.util import (
    archive_folder_name,
    human_size,
    normalize_entry_path,
    parse_header_lines,
    strip_common_root,
    UnsafePath,
)


class FakeEntry:
    def __init__(self, path, is_dir=False):
        self.path = path
        self.is_dir = is_dir


class HumanSizeTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(999), "999 B")
        self.assertEqual(human_size(1024), "1.00 KB")

    def test_large(self):
        self.assertIn("GB", human_size(3 * 1024**3))
        self.assertIn("MB", human_size(5 * 1024**2))

    def test_invalid(self):
        self.assertEqual(human_size(-1), "—")
        self.assertEqual(human_size("nope"), "—")


class EntryPathTests(unittest.TestCase):
    def test_plain_path(self):
        self.assertEqual(normalize_entry_path("docs/readme.txt"), "docs/readme.txt")

    def test_backslashes_and_dot_prefix(self):
        self.assertEqual(normalize_entry_path(".\\docs\\readme.txt"), "docs/readme.txt")
        self.assertEqual(normalize_entry_path("./docs/readme.txt"), "docs/readme.txt")

    def test_absolute_is_made_relative(self):
        self.assertEqual(normalize_entry_path("/etc/passwd"), "etc/passwd")

    def test_traversal_rejected(self):
        with self.assertRaises(UnsafePath):
            normalize_entry_path("../evil")
        with self.assertRaises(UnsafePath):
            normalize_entry_path("docs/../../evil")

    def test_drive_letter_rejected(self):
        with self.assertRaises(UnsafePath):
            normalize_entry_path("C:/Windows/system32")

    def test_nul_rejected(self):
        with self.assertRaises(UnsafePath):
            normalize_entry_path("bad\x00name")

    def test_empty_rejected(self):
        with self.assertRaises(UnsafePath):
            normalize_entry_path("   ")


class StripRootTests(unittest.TestCase):
    def test_strips_shared_folder(self):
        entries = [
            FakeEntry("Project/a.txt"),
            FakeEntry("Project/sub/b.txt"),
            FakeEntry("Project/sub", is_dir=True),
        ]
        strip_common_root(entries)
        self.assertEqual(entries[0].path, "a.txt")
        self.assertEqual(entries[1].path, "sub/b.txt")
        self.assertEqual(entries[2].path, "sub")

    def test_does_not_strip_multiple_roots(self):
        entries = [FakeEntry("one/a.txt"), FakeEntry("two/b.txt")]
        strip_common_root(entries)
        self.assertEqual(entries[0].path, "one/a.txt")

    def test_does_not_strip_root_file(self):
        entries = [FakeEntry("readme.txt"), FakeEntry("Project/a.txt")]
        strip_common_root(entries)
        self.assertEqual(entries[0].path, "readme.txt")


class HeaderTests(unittest.TestCase):
    def test_parses_headers(self):
        headers = parse_header_lines("Referer: https://example.com\nCookie: a=b")
        self.assertEqual(headers["Referer"], "https://example.com")
        self.assertEqual(headers["Cookie"], "a=b")

    def test_rejects_managed_headers(self):
        for name in ("Host", "Range", "Content-Length", "Accept-Encoding"):
            with self.assertRaises(ValueError):
                parse_header_lines(f"{name}: value")

    def test_rejects_bad_syntax(self):
        with self.assertRaises(ValueError):
            parse_header_lines("no colon here")
        with self.assertRaises(ValueError):
            parse_header_lines("Bad Name: value")
        with self.assertRaises(ValueError):
            parse_header_lines("X-Test: value\r\nInjected: yes")


class FolderNameTests(unittest.TestCase):
    def test_known_suffixes(self):
        self.assertEqual(archive_folder_name("Sample Project.zip"), "Sample Project")
        self.assertEqual(archive_folder_name("backup.part1.rar"), "backup")
        self.assertEqual(archive_folder_name("game.ISO"), "game")

    def test_generic_suffix(self):
        self.assertEqual(archive_folder_name("photos.cbz"), "photos")

    def test_no_suffix(self):
        self.assertEqual(archive_folder_name("archive"), "archive")


if __name__ == "__main__":
    unittest.main()
