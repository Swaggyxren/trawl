from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from . import __version__
from .net import validate_link
from .settings import data_dir

MAX_FIELD = 100


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def redact_url(url: str) -> dict[str, object] | None:
    """Return diagnostic-safe URL fields, or ``None`` if the URL is invalid.

    Only validated http(s) URLs are logged. Credentials, query strings and
    fragments are dropped, and host/path are capped at 100 characters.
    """
    valid, _error = validate_link(url)
    if not valid:
        return None
    try:
        parts = urllib.parse.urlsplit(valid)
    except ValueError:
        return None
    path = (parts.path or "/")[:MAX_FIELD]
    host = (parts.hostname or "")[:MAX_FIELD].lower()
    if not host:
        return None
    return {
        "scheme": parts.scheme.lower(),
        "host": host,
        "path": path,
        "extension": Path(path).suffix.lower()[:16],
        "has_query": bool(parts.query),
    }


class LaunchLog:
    """Append-only JSONL diagnostic log, one correlated event per line.

    A ``start`` event is written immediately so a crash still leaves a trace;
    ``end`` is written when the application shuts down or the window closes.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "launches.jsonl")
        self.session_id = str(uuid.uuid4())
        self._lock = threading.Lock()
        self._started = False
        self._finished = False
        self._start_time = 0.0
        self._counts = {"urls": 0, "archives": 0, "downloads": 0, "errors": 0}

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._start_time = time.time()
        self._cleanup_legacy()
        self._write("start", version=__version__, pid=os.getpid())

    def _cleanup_legacy(self) -> None:
        """Drop sessions whose URL events contain pasted free text."""
        try:
            raw = self.path.read_text("utf-8")
        except (OSError, UnicodeError):
            return
        events: list[dict] = []
        bad_sessions: set[str] = set()
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            if event.get("event") != "url":
                continue
            host = event.get("host")
            valid_host = isinstance(host, str) and bool(host)
            if valid_host:
                if any(char.isspace() for char in host):
                    valid_host = False
                elif not (host == "localhost" or "." in host or _is_ip(host)):
                    valid_host = False
            if not valid_host or "raw" in event:
                bad_sessions.add(str(event.get("session", "")))

        kept: list[str] = []
        for event in events:
            if str(event.get("session", "")) in bad_sessions:
                continue
            if event.get("event") == "error" and (
                "code" not in event or "message" in event
            ):
                continue
            if event.get("event") == "url":
                if isinstance(event.get("host"), str):
                    event["host"] = event["host"][:MAX_FIELD]
                if isinstance(event.get("path"), str):
                    event["path"] = event["path"][:MAX_FIELD]
            kept.append(json.dumps(event, sort_keys=True, ensure_ascii=False))
        if len(kept) == len(events):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".cleanup")
            temp.write_text("\n".join(kept) + ("\n" if kept else ""), "utf-8")
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except OSError:
            pass

    def record_url(self, url: str) -> None:
        fields = redact_url(url)
        if fields is None:
            return
        with self._lock:
            if not self._started:
                return
            self._counts["urls"] += 1
        self._write("url", **fields)

    def record_archive(self, kind: str, files: int, size: int) -> None:
        with self._lock:
            if not self._started:
                return
            self._counts["archives"] += 1
        self._write("archive", kind=str(kind)[:16], files=files, size=size)

    def record_download(
        self,
        files: int,
        failed: int,
        skipped: int,
        bytes_written: int,
        *,
        mode: str = "selected",
        selected_files: int = 0,
        total_files: int = 0,
        duration_seconds: float = 0.0,
        result: str = "complete",
    ) -> None:
        with self._lock:
            if not self._started:
                return
            self._counts["downloads"] += 1
            if failed:
                self._counts["errors"] += 1
        self._write(
            "download",
            files=files,
            failed=failed,
            skipped=skipped,
            bytes=bytes_written,
            mode=mode,
            selected_files=selected_files,
            total_files=total_files,
            duration_seconds=round(duration_seconds, 3),
            result=result,
        )

    def record_progress(
        self, bytes_done: int, files_done: int, files_failed: int
    ) -> None:
        with self._lock:
            if not self._started:
                return
        self._write(
            "progress",
            bytes=bytes_done,
            files_done=files_done,
            files_failed=files_failed,
        )

    def record_error(self, code: str, exception_class: str = "") -> None:
        """Write a short error code and exception class, never free text."""
        with self._lock:
            if not self._started:
                return
            self._counts["errors"] += 1
        self._write(
            "error",
            code=str(code)[:40],
            exception_class=str(exception_class)[:80],
        )

    def finish(self) -> None:
        with self._lock:
            if not self._started or self._finished:
                return
            self._finished = True
            counts = dict(self._counts)
            duration = max(0.0, time.time() - self._start_time)
        self._write(
            "end",
            duration_seconds=round(duration, 3),
            counts=counts,
        )

    def _write(self, event: str, **fields: object) -> None:
        record = {
            "session": self.session_id,
            "event": event,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **fields,
        }
        line = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
            except OSError:
                # Diagnostics must never break the app.
                pass
