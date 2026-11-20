from __future__ import annotations

import re
import stat
import struct
from datetime import datetime, timedelta, timezone
from typing import Iterator

from ..net import HTTPRangeReader, RemoteInfo
from ..util import strip_common_root
from .base import (
    Archive,
    ArchiveError,
    CorruptArchive,
    Entry,
    UnsupportedArchive,
    safe_entry_path,
)

SECTOR_SIZE = 2048
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 64


class IsoArchive(Archive):
    kind = "ISO"

    def __init__(self, reader: HTTPRangeReader, info: RemoteInfo):
        super().__init__(reader, info)
        self._primary_root: bytes | None = None
        self._joliet_root: bytes | None = None
        self._use_joliet = False
        self._use_rr = False
        self._visited_directories: set[int] = set()

    def read_index(self, progress=None) -> None:
        if self.info.size < (16 * SECTOR_SIZE) + SECTOR_SIZE:
            raise CorruptArchive("This file is too small to contain an ISO 9660 volume.")
        if progress:
            progress("Reading ISO volume descriptors")

        pvd = None
        for index in range(64):
            sector = 16 + index
            offset = sector * SECTOR_SIZE
            if offset + SECTOR_SIZE > self.info.size:
                break
            block = self.reader.read_range(offset, SECTOR_SIZE)
            if len(block) < SECTOR_SIZE:
                break
            if block[1:6] != b"CD001":
                if block[0] == 0:
                    continue
                break
            descriptor_type = block[0]
            if descriptor_type == 1 and pvd is None:
                pvd = block
            elif descriptor_type == 2 and block[88:91] in (b"%/@", b"%/C", b"%/E"):
                if self._joliet_root is None:
                    self._joliet_root = self._extract_root_record(block)
                    self._use_joliet = True
            elif descriptor_type == 255:
                break

        if pvd is None:
            raise UnsupportedArchive(
                "No ISO 9660 primary volume descriptor was found. UDF-only images are not supported."
            )
        self._primary_root = self._extract_root_record(pvd)
        if not self._primary_root:
            raise CorruptArchive("The ISO root directory record is missing.")
        self._use_rr = b"SP\x07\x01\xbe\xef" in self._record_system_use(self._primary_root)

        root = self._primary_root if (self._use_rr or not self._use_joliet) else self._joliet_root
        if not root:
            root = self._primary_root
        if progress:
            progress("Walking the ISO directory tree")
        self._walk_directory(root, "", 0, self._use_joliet and not self._use_rr)
        strip_common_root(self.entries)
        if progress:
            progress(f"Indexed {len(self.files):,} files")

    @staticmethod
    def _extract_root_record(block: bytes) -> bytes | None:
        if len(block) < 190:
            return None
        length = block[156]
        if length < 34 or 156 + length > len(block):
            return None
        return block[156 : 156 + length]

    @staticmethod
    def _parse_record(data: bytes, position: int) -> dict | None:
        if position + 33 > len(data):
            return None
        length = data[position]
        if length == 0:
            return None
        if length < 34 or position + length > len(data):
            return None
        ext_attr_length = data[position + 1]
        extent = struct.unpack_from("<I", data, position + 2)[0]
        data_length = struct.unpack_from("<I", data, position + 10)[0]
        date = data[position + 18 : position + 25]
        flags = data[position + 25]
        name_length = data[position + 32]
        if 33 + name_length > length:
            return None
        name = data[position + 33 : position + 33 + name_length]
        system_use_start = 33 + name_length + (1 if name_length % 2 == 0 else 0)
        system_use = data[position + system_use_start : position + length]
        return {
            "length": length,
            "ext_attr_length": ext_attr_length,
            "extent": extent,
            "size": data_length,
            "date": date,
            "flags": flags,
            "name": name,
            "system_use": system_use,
        }

    @staticmethod
    def _record_system_use(record: bytes) -> bytes:
        parsed = IsoArchive._parse_record(record, 0)
        return parsed["system_use"] if parsed else b""

    def _walk_directory(self, record: bytes, base_path: str, depth: int, joliet: bool) -> None:
        if depth > MAX_DEPTH:
            raise CorruptArchive("The ISO directory tree is nested too deeply.")
        parsed = self._parse_record(record, 0)
        if parsed is None:
            return
        extent = parsed["extent"]
        size = parsed["size"]
        if size <= 0:
            return
        if size > MAX_DIRECTORY_BYTES:
            raise ArchiveError("An ISO directory is too large to display safely.")
        directory_key = extent
        if directory_key in self._visited_directories:
            return
        self._visited_directories.add(directory_key)
        offset = extent * SECTOR_SIZE + parsed["ext_attr_length"]
        if offset + size > self.info.size:
            raise CorruptArchive("An ISO directory points past the end of the image.")
        data = self.reader.read_range(offset, size)
        if len(data) != size:
            raise CorruptArchive("An ISO directory is truncated.")

        position = 0
        while position < len(data):
            if data[position] == 0:
                position = ((position // SECTOR_SIZE) + 1) * SECTOR_SIZE
                continue
            item = self._parse_record(data, position)
            if item is None:
                break
            position += item["length"]
            name_bytes = item["name"]
            if not name_bytes or name_bytes in (b"\x00", b"\x01"):
                continue
            is_dir = bool(item["flags"] & 0x02)
            multi_extent = bool(item["flags"] & 0x80)
            mode = None
            rr_name = None
            if self._use_rr:
                rr_name, mode = self._parse_rock_ridge(item["system_use"])
            if joliet:
                name = self._decode_joliet(name_bytes)
            elif rr_name:
                name = rr_name
            else:
                name = self._decode_primary(name_bytes)
            name = name.strip()
            if name in ("", ".", ".."):
                continue
            if is_dir:
                path = f"{base_path}/{name}" if base_path else name
            else:
                path = f"{base_path}/{name}" if base_path else name
            cleaned, path_error = safe_entry_path(path, is_dir)
            if path_error:
                self.warnings.append(f"Skipped an ISO entry: {path_error}")
                continue

            unsupported = None
            if mode is not None and stat.S_ISLNK(mode):
                unsupported = "Symbolic link"
            elif multi_extent:
                unsupported = "Multi-extent file"

            modified = self._decode_time(item["date"])
            if is_dir:
                self.entries.append(
                    Entry(
                        path=cleaned,
                        size=0,
                        compressed_size=0,
                        mtime=modified,
                        is_dir=True,
                        stored=True,
                        raw_path=name,
                    )
                )
                if not (mode is not None and stat.S_ISLNK(mode)):
                    self._walk_directory(
                        data[position - item["length"] : position],
                        cleaned,
                        depth + 1,
                        joliet,
                    )
            else:
                self.entries.append(
                    Entry(
                        path=cleaned,
                        size=int(item["size"]),
                        compressed_size=int(item["size"]),
                        data_offset=item["extent"] * SECTOR_SIZE + item["ext_attr_length"],
                        method=0,
                        mtime=modified,
                        is_dir=False,
                        stored=True,
                        unsupported=unsupported,
                        raw_path=name,
                        extra={"multi_extent": multi_extent},
                    )
                )

    def _parse_rock_ridge(self, system_use: bytes) -> tuple[str | None, int | None]:
        position = 0
        name_parts: list[str] = []
        mode = None
        while position + 4 <= len(system_use):
            signature = system_use[position : position + 2]
            length = system_use[position + 2]
            if length < 4 or position + length > len(system_use):
                break
            entry = system_use[position : position + length]
            position += length
            if signature == b"NM" and length >= 5:
                flags = entry[4]
                if flags & 0x02 or flags & 0x04 or flags & 0x08:
                    continue
                try:
                    part = entry[5:].decode("utf-8", "replace")
                except Exception:
                    part = entry[5:].decode("latin-1", "replace")
                if flags & 0x01 and name_parts:
                    name_parts[-1] += part
                else:
                    name_parts.append(part)
            elif signature == b"PX" and length >= 12:
                mode = struct.unpack_from("<I", entry, 4)[0]
        return ("".join(name_parts) if name_parts else None), mode

    @staticmethod
    def _decode_primary(raw: bytes) -> str:
        try:
            name = raw.decode("ascii")
        except UnicodeDecodeError:
            name = raw.decode("latin-1", "replace")
        name = re.sub(r";\d+$", "", name)
        return name.rstrip(".") if name.endswith(".") else name

    @staticmethod
    def _decode_joliet(raw: bytes) -> str:
        if len(raw) % 2:
            raw = raw[:-1]
        try:
            name = raw.decode("utf-16-be")
        except UnicodeDecodeError:
            name = raw.decode("utf-16-be", "replace")
        name = name.split("\x00", 1)[0]
        name = re.sub(r";\d+$", "", name)
        return name

    @staticmethod
    def _decode_time(raw: bytes) -> datetime | None:
        if len(raw) < 7:
            return None
        year = 1900 + raw[0]
        month, day, hour, minute, second = raw[1], raw[2], raw[3], raw[4], raw[5]
        offset = raw[6]
        if offset > 127:
            offset -= 256
        try:
            zone = timezone(timedelta(minutes=offset * 15))
            value = datetime(
                year, month, day, hour, minute, min(second, 59), tzinfo=zone
            )
            return value.astimezone().replace(tzinfo=None)
        except ValueError:
            return None

    def iter_data(self, entry: Entry, start: int = 0) -> Iterator[bytes]:
        if entry.is_dir:
            return
        if entry.unsupported:
            raise ArchiveError(entry.unsupported)
        if start < 0 or start > entry.size:
            raise ArchiveError("Invalid resume offset.")
        if start:
            yield from self.reader.iter_range(
                entry.data_offset + start, entry.size - start
            )
        else:
            yield from self.reader.iter_range(entry.data_offset, entry.size)
