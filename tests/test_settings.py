from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from trawl.settings import HistoryItem, SettingsStore


class SettingsTests(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            store = SettingsStore(path)
            store.settings.threads = 8
            store.settings.headers = "Referer: https://example.com"
            store.remember(
                HistoryItem(
                    url="https://example.com/a.zip",
                    filename="a.zip",
                    size=1234,
                    kind="ZIP",
                    opened_at=1000.0,
                )
            )
            reloaded = SettingsStore(path)
            self.assertEqual(reloaded.settings.threads, 8)
            self.assertEqual(reloaded.settings.headers, "Referer: https://example.com")
            self.assertEqual(len(reloaded.settings.history), 1)
            self.assertEqual(reloaded.settings.history[0].filename, "a.zip")

    def test_history_dedupe_and_clear(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            store = SettingsStore(path)
            item = HistoryItem(url="https://example.com/a.zip", filename="a.zip")
            store.remember(item)
            store.remember(item)
            self.assertEqual(len(store.settings.history), 1)
            store.clear_history()
            self.assertEqual(store.settings.history, [])


if __name__ == "__main__":
    unittest.main()
