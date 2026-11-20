from __future__ import annotations

import logging
import os
import stat
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .archives.base import Archive, ArchiveError, Entry
from .util import human_size

log = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
SKIPPED = "skipped"
FAILED = "failed"
CANCELLED = "cancelled"

_FINISHED = (DONE, SKIPPED, FAILED, CANCELLED)


class UnsafeDestination(ArchiveError):
    pass


@dataclass(slots=True)
class DownloadOptions:
    threads: int = 4
    skip_existing: bool = True
    overwrite: bool = False
    resume: bool = True
    verify: bool = True
    preserve_mtime: bool = True
    flatten: bool = False


@dataclass
class FileProgress:
    entry: Entry
    status: str = QUEUED
    downloaded: int = 0
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    speed: float = 0.0

    @property
    def size(self) -> int:
        return self.entry.size

    @property
    def fraction(self) -> float:
        if self.entry.size <= 0:
            return 1.0 if self.status == DONE else 0.0
        return min(1.0, self.downloaded / self.entry.size)

    @property
    def finished(self) -> bool:
        return self.status in _FINISHED


@dataclass
class JobProgress:
    files: list[FileProgress]
    total_bytes: int = 0
    done_bytes: int = 0
    total_files: int = 0
    done_files: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    speed: float = 0.0
    status: str = QUEUED
    message: str = ""


class DestinationGuard:
    """Creates destination directories without following any symlinks."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self._root_real: str | None = None
        self._lock = threading.Lock()

    def prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise UnsafeDestination(f"{self.root} is not a directory.")
        real = os.path.realpath(self.root)
        if not os.access(real, os.W_OK):
            raise UnsafeDestination(f"Trawl cannot write to {self.root}.")
        self._root_real = real

    def resolve(self, relative: str) -> Path:
        if self._root_real is None:
            raise UnsafeDestination("The destination folder is not ready.")
        parts = [part for part in relative.split("/") if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts):
            raise UnsafeDestination(f"Refusing to write an unsafe path: {relative}")

        current = self._root_real
        for part in parts[:-1]:
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                with self._lock:
                    try:
                        os.mkdir(current, 0o755)
                    except FileExistsError:
                        pass
                info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                raise UnsafeDestination(
                    f"Refusing to write through the symbolic link {current}."
                )
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafeDestination(f"{current} exists and is not a folder.")
        target = os.path.join(current, parts[-1])
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return Path(target)
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeDestination(f"Refusing to overwrite the symbolic link {target}.")
        return Path(target)


class DownloadJob:
    """Downloads a set of archive entries using a small worker pool."""

    def __init__(
        self,
        archive: Archive,
        entries: Iterable[Entry],
        destination: Path,
        options: DownloadOptions | None = None,
        on_update: Callable[[JobProgress], None] | None = None,
        on_finished: Callable[[JobProgress], None] | None = None,
    ):
        self.archive = archive
        self.destination = Path(destination).expanduser()
        self.options = options or DownloadOptions()
        self.on_update = on_update
        self.on_finished = on_finished
        self.files = [FileProgress(entry=entry) for entry in entries if not entry.is_dir]
        self.total_bytes = sum(item.size for item in self.files)
        self.status = QUEUED
        self.message = ""
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._started_at = 0.0
        self._last_emit = 0.0
        self._sampled_at = 0.0
        self._sampled_bytes = 0
        self._speed = 0.0
        self._guard = DestinationGuard(self.destination)

    # ------------------------------------------------------------------ state
    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()
        self._emit(force=True)

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("This download has already been started.")
        self.status = RUNNING
        self._started_at = time.monotonic()
        self._sampled_at = self._started_at
        try:
            self._guard.prepare_root()
        except Exception as error:
            self.status = FAILED
            self.message = str(error)
            for item in self.files:
                item.status = FAILED
                item.error = str(error)
            self._emit(force=True)
            self._finish()
            return

        workers = max(1, min(int(self.options.threads or 1), 16, max(1, len(self.files))))
        for index in range(workers):
            thread = threading.Thread(
                target=self._worker,
                name=f"trawl-download-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        # The coordinator reaps the workers and emits the final state, so the
        # GUI never misses the completion callback.
        threading.Thread(target=self._coordinate, name="trawl-coordinator", daemon=True).start()

    def _coordinate(self) -> None:
        for thread in self._threads:
            thread.join()
        with self._lock:
            if self.cancelled and self.status == RUNNING:
                self.status = CANCELLED
            elif any(item.status == FAILED for item in self.files):
                self.status = "partial"
                self.message = f"{sum(1 for item in self.files if item.status == FAILED)} file(s) failed."
            else:
                self.status = DONE
                self.message = "All files downloaded."
        self._emit(force=True)
        self._finish()

    def _finish(self) -> None:
        if self.on_finished is not None:
            try:
                self.on_finished(self.snapshot())
            except Exception:
                log.exception("Download completion callback failed")

    # -------------------------------------------------------------- progress
    def snapshot(self) -> JobProgress:
        with self._lock:
            done_bytes = sum(
                item.downloaded if item.status != SKIPPED else item.size for item in self.files
            )
            done_files = sum(1 for item in self.files if item.finished)
            failed = sum(1 for item in self.files if item.status == FAILED)
            skipped = sum(1 for item in self.files if item.status == SKIPPED)
            elapsed = max(0.001, time.monotonic() - self._started_at) if self._started_at else 0.0
            speed = done_bytes / elapsed if elapsed and self.status == RUNNING else 0.0
            return JobProgress(
                files=list(self.files),
                total_bytes=self.total_bytes,
                done_bytes=min(done_bytes, self.total_bytes),
                total_files=len(self.files),
                done_files=done_files,
                failed_files=failed,
                skipped_files=skipped,
                speed=max(self._speed, speed),
                status=self.status,
                message=self.message,
            )

    def _emit(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._sampled_at
            if elapsed >= 0.5:
                done_bytes = sum(
                    item.downloaded if item.status != SKIPPED else item.size
                    for item in self.files
                )
                self._speed = max(0.0, (done_bytes - self._sampled_bytes) / elapsed)
                self._sampled_bytes = done_bytes
                self._sampled_at = now
        if not force and now - self._last_emit < 0.12:
            return
        self._last_emit = now
        if self.on_update is not None:
            try:
                self.on_update(self.snapshot())
            except Exception:
                log.exception("Download progress callback failed")

    # ---------------------------------------------------------------- workers
    def _worker(self) -> None:
        while not self.cancelled:
            with self._lock:
                pending = next((item for item in self.files if item.status == QUEUED), None)
                if pending is None:
                    return
                pending.status = RUNNING
                pending.started_at = time.monotonic()
            try:
                self._download_one(pending)
            except Exception as error:
                if self.cancelled:
                    with self._lock:
                        pending.status = CANCELLED
                else:
                    log.debug("Download failed for %s", pending.entry.path, exc_info=True)
                    with self._lock:
                        pending.status = FAILED
                        pending.error = str(error) or error.__class__.__name__
                        pending.finished_at = time.monotonic()
            self._emit()

    def _download_one(self, item: FileProgress) -> None:
        entry = item.entry
        relative = entry.path
        if self.options.flatten:
            relative = entry.path.rsplit("/", 1)[-1]
        target = self._guard.resolve(relative)

        if target.exists() and not self.options.overwrite:
            if self.options.skip_existing:
                with self._lock:
                    item.status = SKIPPED
                    item.downloaded = entry.size
                    item.finished_at = time.monotonic()
                    item.speed = 0.0
                return
            raise ArchiveError(f"{target.name} already exists.")

        part = target.with_name(target.name + ".trawl-part")
        if not entry.stored or not self.options.resume:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            resume_at = 0
        else:
            try:
                resume_at = part.stat().st_size
            except OSError:
                resume_at = 0
            if resume_at > entry.size:
                part.unlink(missing_ok=True)
                resume_at = 0

        crc: int | None = None
        if self.options.verify and entry.crc is not None:
            crc = 0 if not resume_at else None

        mode = "ab" if resume_at else "wb"
        with open(part, mode) as handle:
            with self._lock:
                item.downloaded = resume_at
            last = time.monotonic()
            last_bytes = resume_at
            for chunk in self.archive.iter_data(entry, start=resume_at):
                if self.cancelled:
                    raise ArchiveError("Cancelled")
                if not chunk:
                    continue
                handle.write(chunk)
                size_now = len(chunk)
                with self._lock:
                    item.downloaded += size_now
                if crc is not None:
                    crc = zlib.crc32(chunk, crc)
                now = time.monotonic()
                if now - last >= 0.35:
                    with self._lock:
                        item.speed = (item.downloaded - last_bytes) / max(0.001, now - last)
                    last = now
                    last_bytes = item.downloaded
                    self._emit()
            handle.flush()
            os.fsync(handle.fileno())

        with self._lock:
            item.downloaded = entry.size
        if self.options.verify and entry.crc is not None:
            if crc is None:
                crc = self._file_crc(part)
            if (crc & 0xFFFFFFFF) != (entry.crc & 0xFFFFFFFF):
                part.unlink(missing_ok=True)
                raise ArchiveError(
                    f"Checksum mismatch (expected {entry.crc:08x}). The download may be corrupt."
                )
        os.replace(part, target)
        if self.options.preserve_mtime and entry.mtime is not None:
            try:
                timestamp = entry.mtime.timestamp()
                os.utime(target, (timestamp, timestamp))
            except (OSError, OverflowError, ValueError):
                pass
        with self._lock:
            item.status = DONE
            item.finished_at = time.monotonic()

    @staticmethod
    def _file_crc(path: Path) -> int:
        crc = 0
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                crc = zlib.crc32(chunk, crc)
        return crc


def estimate_destination(archive_folder: str, base: Path) -> Path:
    return Path(base).expanduser() / archive_folder


def free_space(path: Path) -> int | None:
    try:
        stats = os.statvfs(path)
        return stats.f_bavail * stats.f_frsize
    except OSError:
        return None


def describe_missing_space(required: int, available: int | None) -> str:
    if available is None or available >= required:
        return ""
    return (
        f"Not enough free space: {human_size(required)} needed, "
        f"{human_size(available)} available."
    )
