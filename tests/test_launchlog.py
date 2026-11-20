from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trawl.archives.base import CorruptArchive
from trawl.errors import error_code, friendly_message
from trawl.launchlog import LaunchLog, redact_url
from trawl.net import validate_link


class ValidationTests(unittest.TestCase):
    def test_valid_links(self):
        cases = (
            "https://example.com/a.zip",
            "http://localhost:8000/file.rar",
            "http://127.0.0.1:8080/disc.iso",
            "example.com/file.zip",
        )
        for text in cases:
            with self.subTest(text=text):
                url, error = validate_link(text)
                self.assertTrue(url)
                self.assertEqual(error, "")

    def test_invalid_free_text(self):
        for text in (
            "Paste a link to a ZIP, RAR or ISO file",
            "https://example.com/a b.zip",
            "https://example.com/a\nhttps://evil.com/b",
            "https://foo/bar",
            "ftp://example.com/a.zip",
            "a" * 2049,
        ):
            with self.subTest(text=text[:40]):
                url, error = validate_link(text)
                self.assertIsNone(url)
                self.assertIn("doesn't look like a link", error)


class ErrorCodeTests(unittest.TestCase):
    def test_error_codes_are_short(self):
        self.assertEqual(error_code(ValueError("paste a link")), "invalid_url")
        self.assertEqual(error_code(CorruptArchive("Checksum mismatch")), "crc_mismatch")
        self.assertNotIn("paste a link", friendly_message(ValueError("paste a link")))


class LaunchLogTests(unittest.TestCase):
    def test_redaction(self):
        info = redact_url("https://user:REDACTED@example.com/a/b.zip?token=EXAMPLE#frag")
        self.assertIsNotNone(info)
        self.assertEqual(info["host"], "example.com")
        self.assertEqual(info["path"], "/a/b.zip")
        self.assertTrue(info["has_query"])
        self.assertIsNone(redact_url("Paste a link to a ZIP file"))

    def test_host_and_path_cap(self):
        long_host = "a" * 200 + ".com"
        info = redact_url(f"https://{long_host}/{'b' * 250}.zip")
        self.assertEqual(len(info["host"]), 100)
        self.assertEqual(len(info["path"]), 100)

    def test_invalid_input_never_logs_url(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "launches.jsonl"
            log = LaunchLog(path)
            log.start()
            log.record_url("Paste a link to a ZIP file")
            log.finish()
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([e["event"] for e in events], ["start", "end"])
            self.assertEqual(events[-1]["counts"]["urls"], 0)

    def test_download_and_progress_fields(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "launches.jsonl"
            log = LaunchLog(path)
            log.start()
            log.record_progress(1024, 2, 0)
            log.record_download(
                5, 0, 0, 5000,
                mode="all", selected_files=5, total_files=5,
                duration_seconds=1.25, result="complete",
            )
            events = [json.loads(line) for line in path.read_text().splitlines()]
            progress = events[1]
            self.assertEqual(progress["event"], "progress")
            self.assertEqual(progress["bytes"], 1024)
            download = events[2]
            self.assertEqual(download["mode"], "all")
            self.assertEqual(download["selected_files"], 5)
            self.assertEqual(download["total_files"], 5)
            self.assertEqual(download["duration_seconds"], 1.25)
            self.assertEqual(download["result"], "complete")

    def test_error_event_has_code_and_class_only(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "launches.jsonl"
            log = LaunchLog(path)
            log.start()
            log.record_error("invalid_url", "ValueError")
            log.finish()
            event = json.loads(path.read_text().splitlines()[1])
            self.assertEqual(event["code"], "invalid_url")
            self.assertEqual(event["exception_class"], "ValueError")
            self.assertNotIn("message", event)

    def test_legacy_cleanup_drops_whole_session(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "launches.jsonl"
            bad = "bad-session"
            good = "good-session"
            lines = [
                {"session": bad, "event": "start"},
                {"session": bad, "event": "url", "host": "fix these trawl ui issues (src"},
                {"session": bad, "event": "end"},
                {"session": good, "event": "url", "host": "example.com", "path": "/a.zip"},
                {"session": good, "event": "end"},
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
            log = LaunchLog(path)
            log.start()
            text = path.read_text()
            self.assertNotIn(bad, text)
            self.assertNotIn("paste", text)
            self.assertIn(good, text)
            self.assertIn("start", text)

    def test_record_before_start_is_ignored(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "launches.jsonl"
            log = LaunchLog(path)
            log.record_url("https://example.com/a.zip")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
