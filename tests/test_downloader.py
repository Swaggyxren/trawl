from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trawl.archives import open_archive
from trawl.downloader import DownloadJob, DownloadOptions, DestinationGuard
from trawl.net import HTTPRangeReader

from fixtures import make_zip
from support import serve


def wait_for(job: DownloadJob, timeout: float = 10.0) -> None:
    finished = threading.Event()
    original = job.on_finished

    def on_finished(progress):
        if original is not None:
            original(progress)
        finished.set()

    job.on_finished = on_finished
    job.start()
    if not finished.wait(timeout):
        raise AssertionError("Download did not finish in time")


class DownloadTests(unittest.TestCase):
    def _archive(self, base: str):
        reader = HTTPRangeReader(f"{base}/sample.zip", {})
        archive = open_archive(reader, reader.probe())
        archive.read_index()
        return archive

    def test_downloads_all_files(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            expected = make_zip(directory / "sample.zip")
            with serve(directory) as base:
                archive = self._archive(base)
                destination = directory / "out"
                job = DownloadJob(archive, archive.files, destination)
                wait_for(job)
                for relative, payload in expected.items():
                    self.assertEqual((destination / relative).read_bytes(), payload)
                archive.close()

    def test_skip_existing(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            make_zip(directory / "sample.zip")
            with serve(directory) as base:
                archive = self._archive(base)
                entry = archive.files[0]
                destination = directory / "out"
                target = destination / entry.path
                target.parent.mkdir(parents=True)
                target.write_bytes(b"keep me")
                job = DownloadJob(archive, [entry], destination)
                wait_for(job)
                self.assertEqual(target.read_bytes(), b"keep me")
                self.assertEqual(job.files[0].status, "skipped")
                archive.close()

    def test_resume_stored_entry(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            expected = make_zip(directory / "sample.zip")
            with serve(directory) as base:
                archive = self._archive(base)
                entry = next(item for item in archive.files if item.stored)
                destination = directory / "out"
                part = destination / (entry.path + ".trawl-part")
                part.parent.mkdir(parents=True)
                part.write_bytes(expected[entry.path][:5000])
                job = DownloadJob(archive, [entry], destination)
                wait_for(job)
                self.assertEqual(
                    (destination / entry.path).read_bytes(), expected[entry.path]
                )
                archive.close()

    def test_destination_guard_refuses_symlink(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "outside").mkdir()
            (root / "link").symlink_to(root / "outside", target_is_directory=True)
            guard = DestinationGuard(root)
            guard.prepare_root()
            with self.assertRaises(Exception):
                guard.resolve("link/file.txt")

    def test_destination_guard_refuses_traversal(self):
        with TemporaryDirectory() as temp:
            guard = DestinationGuard(Path(temp))
            guard.prepare_root()
            with self.assertRaises(Exception):
                guard.resolve("../escape.txt")


if __name__ == "__main__":
    unittest.main()
