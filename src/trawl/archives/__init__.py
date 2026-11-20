from __future__ import annotations

from ..net import HTTPRangeReader, RemoteInfo
from .base import Archive, ArchiveError, CorruptArchive, Entry, UnsupportedArchive
from .iso import IsoArchive
from .rar import RarArchive
from .zip import ZipArchive

__all__ = [
    "Archive",
    "ArchiveError",
    "CorruptArchive",
    "Entry",
    "UnsupportedArchive",
    "detect_format",
    "open_archive",
]

ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def detect_format(reader: HTTPRangeReader, info: RemoteInfo) -> str:
    """Sniff the archive format using as few bytes as possible."""
    head = reader.read_range(0, min(info.size, 4096))
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if head.startswith(ZIP_MAGICS):
        return "zip"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd"
    if info.size >= 32774:
        descriptor = reader.read_range(16 * 2048, 6)
        if len(descriptor) >= 6 and descriptor[1:6] == b"CD001":
            return "iso"

    # Self-extracting archives: look for a signature in the first 1 MiB.
    prefix = reader.read_range(0, min(info.size, 1024 * 1024))
    positions = [
        (prefix.find(b"Rar!\x1a\x07"), "rar"),
        (prefix.find(b"PK\x03\x04"), "zip"),
    ]
    found = [(position, kind) for position, kind in positions if position > 0]
    if found:
        return min(found)[1]
    return "unknown"


def open_archive(reader: HTTPRangeReader, info: RemoteInfo, kind: str | None = None) -> Archive:
    kind = kind or detect_format(reader, info)
    if kind == "zip":
        return ZipArchive(reader, info)
    if kind == "rar":
        return RarArchive(reader, info)
    if kind == "iso":
        return IsoArchive(reader, info)
    if kind == "7z":
        raise UnsupportedArchive(
            "7z archives are not supported yet. Trawl can read ZIP, RAR and ISO images."
        )
    raise UnsupportedArchive(
        "Trawl could not recognise this file. It supports ZIP, RAR and ISO images."
    )
