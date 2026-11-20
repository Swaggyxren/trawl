from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "trawl"
    return Path.home() / ".config" / "trawl"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "trawl"
    return Path.home() / ".local" / "share" / "trawl"


@dataclass
class HistoryItem:
    url: str
    filename: str
    size: int = 0
    kind: str = ""
    opened_at: float = 0.0
    destination: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "HistoryItem":
        return cls(
            url=str(value.get("url", "")),
            filename=str(value.get("filename", "")),
            size=int(value.get("size", 0) or 0),
            kind=str(value.get("kind", "")),
            opened_at=float(value.get("opened_at", 0) or 0),
            destination=str(value.get("destination", "")),
        )


@dataclass
class Settings:
    download_dir: str = ""
    threads: int = 4
    skip_existing: bool = True
    overwrite: bool = False
    resume: bool = True
    verify: bool = True
    preserve_mtime: bool = True
    insecure: bool = False
    slow_mode: bool = False
    headers: str = ""
    strip_single_root: bool = True
    choice_skip: bool = False
    last_choice: str = "select"
    notify_finish: bool = True
    play_sound: bool = True
    history: list[HistoryItem] = field(default_factory=list)
    window_width: int = 1180
    window_height: int = 780
    maximized: bool = False

    def effective_download_dir(self) -> Path:
        if self.download_dir:
            return Path(self.download_dir).expanduser()
        downloads = Path.home() / "Downloads"
        if downloads.is_dir():
            return downloads
        return Path.home()


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "settings.json")
        self.settings = Settings()
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            self.settings.download_dir = str(self.settings.effective_download_dir())
            return
        except (OSError, ValueError) as error:
            log.warning("Could not read settings: %s", error)
            return
        if not isinstance(raw, dict):
            return
        settings = Settings()
        for name in (
            "download_dir",
            "threads",
            "skip_existing",
            "overwrite",
            "resume",
            "verify",
            "preserve_mtime",
            "insecure",
            "slow_mode",
            "headers",
            "strip_single_root",
            "choice_skip",
            "last_choice",
            "notify_finish",
            "play_sound",
            "window_width",
            "window_height",
            "maximized",
        ):
            if name in raw:
                try:
                    setattr(settings, name, type(getattr(settings, name))(raw[name]))
                except (TypeError, ValueError):
                    pass
        settings.threads = max(1, min(16, settings.threads))
        if settings.last_choice not in ("all", "select"):
            settings.last_choice = "select"
        settings.window_width = max(760, min(4000, settings.window_width))
        settings.window_height = max(520, min(3000, settings.window_height))
        history = raw.get("history")
        if isinstance(history, list):
            for item in history[:50]:
                if isinstance(item, dict) and item.get("url"):
                    try:
                        settings.history.append(HistoryItem.from_dict(item))
                    except (TypeError, ValueError):
                        continue
        if not settings.download_dir:
            settings.download_dir = str(settings.effective_download_dir())
        self.settings = settings

    def save(self) -> None:
        payload = asdict(self.settings)
        payload["history"] = [
            {
                "url": item.url,
                "filename": item.filename,
                "size": item.size,
                "kind": item.kind,
                "opened_at": item.opened_at,
                "destination": item.destination,
            }
            for item in self.settings.history[:50]
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".settings.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def remember(self, item: HistoryItem) -> None:
        self.settings.history = [
            existing
            for existing in self.settings.history
            if not (existing.url == item.url and existing.filename == item.filename)
        ]
        self.settings.history.insert(0, item)
        del self.settings.history[50:]
        self.save()

    def forget(self, url: str) -> None:
        self.settings.history = [
            item for item in self.settings.history if item.url != url
        ]
        self.save()

    def clear_history(self) -> None:
        self.settings.history.clear()
        self.save()
