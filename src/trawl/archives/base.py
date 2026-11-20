from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from ..net import HTTPRangeReader, RemoteInfo
from ..util import normalize_entry_path


class ArchiveError(Exception):
    """A user-facing archive parsing or extraction error."""


class UnsupportedArchive(ArchiveError):
    pass


class CorruptArchive(ArchiveError):
    pass


@dataclass(slots=True)
class Entry:
    path: str
    size: int
    compressed_size: int
    data_offset: int = 0
    method: int = 0
    crc: int | None = None
    mtime: datetime | None = None
    is_dir: bool = False
    stored: bool = True
    encrypted: bool = False
    unsupported: str | None = None
    raw_path: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.raw_path:
            self.raw_path = self.path
        if self.size < 0:
            self.size = 0
        if self.compressed_size < 0:
            self.compressed_size = 0

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1] if self.path else ""

    @property
    def folder(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    @property
    def resumable(self) -> bool:
        return self.stored and not self.is_dir and self.unsupported is None

    @property
    def selectable(self) -> bool:
        return not self.is_dir and self.unsupported is None


class Archive:
    """Base class for a remotely-readable archive."""

    kind = "Archive"

    def __init__(self, reader: HTTPRangeReader, info: RemoteInfo):
        self.reader = reader
        self.info = info
        self.entries: list[Entry] = []
        self.warnings: list[str] = []

    @property
    def files(self) -> list[Entry]:
        return [entry for entry in self.entries if not entry.is_dir]

    @property
    def supported_files(self) -> list[Entry]:
        return [entry for entry in self.entries if entry.selectable]

    @property
    def total_size(self) -> int:
        return sum(entry.size for entry in self.files)

    @property
    def supported_size(self) -> int:
        return sum(entry.size for entry in self.supported_files)

    def read_index(self, progress=None) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def iter_data(self, entry: Entry, start: int = 0) -> Iterator[bytes]:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        self.reader.close()


def safe_entry_path(raw: str, is_dir: bool = False) -> tuple[str, str | None]:
    """Normalise a path; return ``(path, error)`` instead of raising."""
    try:
        return normalize_entry_path(raw), None
    except Exception as error:
        fallback = "unsafe-entry"
        if is_dir:
            fallback += "-dir"
        return fallback, f"Unsafe path: {error}"


class BufferedRange:
    """Random-access read cache over a remote range.

    Archive headers usually live in nearby clusters; a small read-ahead
    window avoids one HTTP request per header.
    """

    def __init__(self, reader: HTTPRangeReader, limit: int | None = None, chunk_size: int = 131072):
        self.reader = reader
        self.limit = limit
        self.chunk_size = max(4096, chunk_size)
        self._start = 0
        self._data = b""

    def read(self, offset: int, count: int) -> bytes:
        if count <= 0:
            return b""
        local = offset - self._start
        if 0 <= local and local + count <= len(self._data):
            return self._data[local : local + count]
        fetch = max(count, self.chunk_size)
        if self.limit is not None:
            fetch = min(fetch, max(0, self.limit - offset))
        data = self.reader.read_range(offset, fetch)
        if len(data) < count:
            raise CorruptArchive("The archive header is truncated.")
        self._start = offset
        self._data = data
        return data[:count]

    def invalidate(self) -> None:
        self._start = 0
        self._data = b""


class ChunkedReader:
    """Forward-only buffered reader over a remote range."""

    def __init__(self, reader: HTTPRangeReader, offset: int, length: int, chunk_size: int = 262144):
        self.reader = reader
        self.offset = offset
        self.length = max(0, length)
        self.chunk_size = max(4096, chunk_size)
        self.position = 0
        self._buffer = b""
        self._buffer_start = 0

    @property
    def remaining(self) -> int:
        return max(0, self.length - self.position)

    def _ensure(self, count: int) -> None:
        if count <= 0:
            return
        buffer_position = self.position - self._buffer_start
        if 0 <= buffer_position and buffer_position + count <= len(self._buffer):
            return
        fetch = min(max(count, self.chunk_size), self.length - self.position)
        if fetch <= 0:
            raise CorruptArchive("Unexpected end of archive data.")
        data = self.reader.read_range(self.offset + self.position, fetch)
        if len(data) != fetch:
            raise CorruptArchive("The server returned less data than requested.")
        self._buffer = data
        self._buffer_start = self.position

    def read(self, count: int) -> bytes:
        if count < 0:
            raise ValueError("negative read")
        if count == 0:
            return b""
        if self.position + count > self.length:
            raise CorruptArchive("Archive index is truncated.")
        self._ensure(count)
        start = self.position - self._buffer_start
        data = self._buffer[start : start + count]
        self.position += len(data)
        return data

    def skip(self, count: int) -> None:
        if count <= 0:
            return
        if self.position + count > self.length:
            raise CorruptArchive("Archive index is truncated.")
        self.position += count

    def read_remaining(self) -> bytes:
        return self.read(self.remaining)

    def tell(self) -> int:
        return self.position
