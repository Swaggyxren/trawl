from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(value: int | float) -> str:
    """Format a byte count using binary units, e.g. ``1.4 GB``."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        return "—"
    if value < 1024:
        return f"{int(value)} B"
    for index, unit in enumerate(_UNITS[1:], start=1):
        value /= 1024.0
        if value < 1024 or unit == _UNITS[-1]:
            if value >= 100:
                return f"{value:.0f} {unit}"
            if value >= 10:
                return f"{value:.1f} {unit}"
            return f"{value:.2f} {unit}"
    return f"{value:.1f} PB"


def human_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "—"
    return f"{human_size(bytes_per_second)}/s"


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds != seconds or seconds > 86400 * 7:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s left"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s left"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m left"


def human_count(value: int, singular: str, plural: str | None = None) -> str:
    word = singular if value == 1 else (plural or singular + "s")
    return f"{value:,} {word}"


def human_relative_time(timestamp: float) -> str:
    delta = max(0.0, datetime.now().timestamp() - timestamp)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 86400 * 7:
        return f"{int(delta // 86400)}d ago"
    return datetime.fromtimestamp(timestamp).strftime("%d %b %Y")


def tilde_path(path: str | Path) -> str:
    try:
        home = str(Path.home())
        text = str(path)
        if text == home:
            return "~"
        if text.startswith(home + os.sep):
            return "~" + text[len(home) :]
        return text
    except Exception:
        return str(path)


def sanitize_filename(name: str, fallback: str = "archive") -> str:
    name = (name or "").strip().replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = name.replace("/", "_").strip().strip(".")
    if not name:
        return fallback
    # Avoid pathological component lengths.
    if len(name.encode("utf-8", "replace")) > 200:
        root, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 12:
            name = root[: 180] + "." + ext
        else:
            name = name[: 180]
    return name or fallback


def archive_folder_name(filename: str) -> str:
    base = sanitize_filename(filename, "archive")
    lowered = base.lower()
    for suffix in (
        ".part1.rar",
        ".part01.rar",
        ".zip64",
        ".zip",
        ".rar",
        ".iso",
        ".img",
        ".cbz",
        ".jar",
        ".apk",
        ".epub",
    ):
        if lowered.endswith(suffix) and len(base) > len(suffix):
            return sanitize_filename(base[: -len(suffix)], "archive")
    root, dot, ext = base.rpartition(".")
    if dot and 0 < len(ext) <= 8:
        return sanitize_filename(root, "archive")
    return base


class UnsafePath(ValueError):
    """Raised when an archive entry tries to escape the destination."""


def normalize_entry_path(raw: str) -> str:
    """Return a safe relative POSIX path, or raise :class:`UnsafePath`.

    Archive paths are untrusted. We normalise separators, reject absolute
    paths, drive letters, NUL bytes and ``..`` components.
    """
    if raw is None:
        raise UnsafePath("empty path")
    text = str(raw)
    if "\x00" in text:
        raise UnsafePath("path contains a NUL byte")
    text = text.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    if not text:
        raise UnsafePath("empty path")
    if re.match(r"^[A-Za-z]:($|/)", text):
        raise UnsafePath("path contains a drive letter")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePath("path contains '..'")
        if len(part.encode("utf-8", "surrogateescape")) > 255:
            raise UnsafePath("path component is too long")
        parts.append(part)
    if not parts:
        raise UnsafePath("empty path")
    result = "/".join(parts)
    if len(result.encode("utf-8", "surrogateescape")) > 4096:
        raise UnsafePath("path is too long")
    return result


def strip_common_root(entries: Iterable) -> None:
    """Strip a single shared top-level folder from ``entries`` in place.

    Archives made with "extract to folder" often contain ``Project/...``.
    We drop that redundant folder so the destination does not become
    ``Project/Project/...``. Files left at the root prevent stripping.
    """
    entries = list(entries)
    files = [entry for entry in entries if not getattr(entry, "is_dir", False)]
    if not files:
        return
    roots = {entry.path.split("/", 1)[0] for entry in files}
    if len(roots) != 1:
        return
    root = next(iter(roots))
    if not root:
        return
    if any("/" not in entry.path for entry in files):
        return
    prefix = root + "/"
    for entry in entries:
        if entry.path == root:
            entry.path = ""
        elif entry.path.startswith(prefix):
            entry.path = entry.path[len(prefix) :]


def dos_datetime(date: int, time: int) -> datetime | None:
    """Convert an MS-DOS date/time pair to a naive datetime."""
    try:
        year = 1980 + ((date >> 9) & 0x7F)
        month = (date >> 5) & 0x0F
        day = date & 0x1F
        hour = (time >> 11) & 0x1F
        minute = (time >> 5) & 0x3F
        second = (time & 0x1F) * 2
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        return datetime(year, month, day, hour, minute, min(second, 59))
    except ValueError:
        return None


def unix_datetime(value: int) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone().replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write_bytes(path, json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n")


_FORBIDDEN_HEADERS = {
    "host",
    "range",
    "content-length",
    "transfer-encoding",
    "connection",
    "accept-encoding",
    "if-range",
}


def parse_header_lines(text: str) -> dict[str, str]:
    """Parse ``Name: value`` lines into a header dict, rejecting unsafe ones."""
    text = text or ""
    # A bare CR can be used to smuggle an extra header into an HTTP request.
    if "\r" in text:
        raise ValueError("Header values cannot contain line breaks.")
    headers: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"Header line is missing a colon: {raw_line!r}")
        name = name.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name):
            raise ValueError(f"Invalid header name: {name!r}")
        if name.lower() in _FORBIDDEN_HEADERS:
            raise ValueError(f"The {name} header is managed by Trawl and cannot be overridden.")
        if "\r" in value or "\n" in value:
            raise ValueError("Header values cannot contain line breaks.")
        headers[name] = value
    return headers
