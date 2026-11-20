from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trawl.archives import open_archive
from trawl.archives.base import ArchiveError
from trawl.net import HTTPRangeReader

from fixtures import make_iso, make_rar4, make_rar5, make_zip, make_zip_with_unsafe_entry
from support import serve


class ArchiveTests(unittest.TestCase):
    def _open(self, base: str, name: str):
        reader = HTTPRangeReader(f"{base}/{name}", {})
        info = reader.probe()
        archive = open_archive(reader, info)
        archive.read_index()
        return archive

    def test_zip_listing_and_extraction(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            expected = make_zip(directory / "sample.zip")
            with serve(directory) as base:
                archive = self._open(base, "sample.zip")
                self.assertEqual(archive.kind, "ZIP")
                self.assertEqual(len(archive.files), 3)
                paths = {entry.path for entry in archive.files}
                self.assertEqual(paths, set(expected))
                for entry in archive.files:
                    self.assertTrue(entry.selectable)
                    self.assertEqual(b"".join(archive.iter_data(entry)), expected[entry.path])
                archive.close()

    def test_zip_resume_from_offset(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            expected = make_zip(directory / "sample.zip")
            with serve(directory) as base:
                archive = self._open(base, "sample.zip")
                stored = next(entry for entry in archive.files if entry.stored)
                payload = expected[stored.path]
                self.assertEqual(
                    b"".join(archive.iter_data(stored, start=1234)), payload[1234:]
                )
                archive.close()

    def test_zip_unsafe_entry_is_not_selectable(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            make_zip_with_unsafe_entry(directory / "unsafe.zip")
            with serve(directory) as base:
                archive = self._open(base, "unsafe.zip")
                entry = archive.files[0]
                self.assertFalse(entry.selectable)
                self.assertIn("Unsafe path", entry.unsupported or "")
                with self.assertRaises(ArchiveError):
                    b"".join(archive.iter_data(entry))
                archive.close()

    def test_rar4_stored(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            payload = make_rar4(directory / "stored.rar")
            with serve(directory) as base:
                archive = self._open(base, "stored.rar")
                self.assertEqual(len(archive.files), 1)
                entry = archive.files[0]
                self.assertEqual(entry.path, "hello.txt")
                self.assertTrue(entry.stored)
                self.assertEqual(b"".join(archive.iter_data(entry)), payload)
                archive.close()

    def test_rar5_stored(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            payload = make_rar5(directory / "stored5.rar")
            with serve(directory) as base:
                archive = self._open(base, "stored5.rar")
                self.assertEqual(len(archive.files), 1)
                entry = archive.files[0]
                self.assertEqual(entry.path, "hello5.txt")
                self.assertEqual(b"".join(archive.iter_data(entry)), payload)
                archive.close()

    def test_iso_9660(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            expected = make_iso(directory / "disc.iso")
            with serve(directory) as base:
                archive = self._open(base, "disc.iso")
                self.assertEqual(archive.kind, "ISO")
                paths = {entry.path for entry in archive.files}
                self.assertEqual(paths, set(expected))
                for entry in archive.files:
                    self.assertEqual(b"".join(archive.iter_data(entry)), expected[entry.path])
                archive.close()


if __name__ == "__main__":
    unittest.main()
