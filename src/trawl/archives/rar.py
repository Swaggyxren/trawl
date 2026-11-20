from __future__ import annotations

import struct
import zlib
from typing import Iterator

from ..net import HTTPRangeReader, RemoteInfo
from ..util import dos_datetime, strip_common_root, unix_datetime
from .base import (
    Archive,
    ArchiveError,
    BufferedRange,
    CorruptArchive,
    Entry,
    UnsupportedArchive,
    safe_entry_path,
)

RAR4_SIGNATURE = b"Rar!\x1a\x07\x00"
RAR5_SIGNATURE = b"Rar!\x1a\x07\x01\x00"

RAR4_MAIN = 0x73
RAR4_FILE = 0x74
RAR4_NEWSUB = 0x7A
RAR4_END = 0x7B

RAR5_MAIN = 1
RAR5_FILE = 2
RAR5_SERVICE = 3
RAR5_ENCRYPTION = 4
RAR5_END = 5

MAX_HEADER_SIZE = 2 * 1024 * 1024
MAX_HEADERS = 1_000_000


class RarArchive(Archive):
    kind = "RAR"

    def __init__(self, reader: HTTPRangeReader, info: RemoteInfo):
        super().__init__(reader, info)
        self._volume = False
        self._hash = BufferedRange(reader, limit=info.size)

    def read_index(self, progress=None) -> None:
        head = self.reader.read_range(0, min(16, self.info.size))
        base = 0
        version = 0
        if head.startswith(RAR5_SIGNATURE):
            version = 5
        elif head.startswith(RAR4_SIGNATURE):
            version = 4
        else:
            if progress:
                progress("Looking for the RAR signature")
            prefix = self.reader.read_range(0, min(self.info.size, 1024 * 1024))
            positions = []
            if RAR5_SIGNATURE in prefix:
                positions.append((prefix.find(RAR5_SIGNATURE), 5))
            if RAR4_SIGNATURE in prefix:
                positions.append((prefix.find(RAR4_SIGNATURE), 4))
            if not positions:
                raise CorruptArchive("Could not find a RAR signature.")
            base, version = min(positions, key=lambda item: item[0])
            self.warnings.append("The RAR archive has a self-extracting prefix.")

        if version == 5:
            self._read_rar5(base + 8, progress)
        elif version == 4:
            self._read_rar4(base + 7, progress)
        else:
            raise UnsupportedArchive("Unsupported RAR version.")
        strip_common_root(self.entries)
        if progress:
            progress(f"Indexed {len(self.files):,} files")

    # ------------------------------------------------------------------ RAR 4
    def _read_rar4(self, offset: int, progress=None) -> None:
        size = self.info.size
        count = 0
        while offset + 7 <= size and count < MAX_HEADERS:
            count += 1
            base = self._hash.read(offset, 7)
            header_crc, header_type, flags, header_size = struct.unpack("<HBHH", base)
            if header_size < 7 or header_size > MAX_HEADER_SIZE:
                raise CorruptArchive("Invalid RAR header size.")
            if offset + header_size > size:
                raise CorruptArchive("A RAR header points past the end of the file.")
            header = base + self._hash.read(offset + 7, header_size - 7)
            if zlib.crc32(header[2:]) & 0xFFFF != header_crc:
                raise CorruptArchive("A RAR header failed its checksum.")

            data_size = 0
            if header_type == RAR4_FILE:
                data_size = self._parse_rar4_file(header, offset, header_size)
            elif header_type == RAR4_MAIN:
                if flags & 0x0080:
                    raise UnsupportedArchive(
                        "This RAR has encrypted headers. Trawl cannot list it without the password."
                    )
                self._volume = bool(flags & 0x0001)
            elif header_type == RAR4_NEWSUB:
                if len(header) >= 11:
                    data_size = struct.unpack_from("<I", header, 7)[0]
            elif header_type == RAR4_END:
                if flags & 0x0001:
                    self.warnings.append("This is not the last volume of the RAR set.")
                break
            elif flags & 0x8000 and len(header) >= 11:
                # HD_ADD_SIZE_PRESENT on an unknown block.
                data_size = struct.unpack_from("<I", header, 7)[0]

            next_offset = offset + header_size + data_size
            if next_offset <= offset:
                raise CorruptArchive("The RAR header chain does not advance.")
            if next_offset > size:
                self.warnings.append("The RAR index ends past the end of the file.")
                break
            offset = next_offset
        if count >= MAX_HEADERS:
            self.warnings.append("Stopped after too many RAR headers.")

    def _parse_rar4_file(self, header: bytes, offset: int, header_size: int) -> int:
        if len(header) < 32:
            raise CorruptArchive("A RAR file header is too short.")
        (
            pack_size,
            unp_size,
            host_os,
            file_crc,
            file_time,
            _unp_version,
            method,
            name_size,
            attributes,
        ) = struct.unpack_from("<LLBLLBBHL", header, 7)
        position = 32
        return self._add_rar4_entry(
            header,
            offset,
            header_size,
            pack_size,
            unp_size,
            host_os,
            file_crc,
            file_time,
            method,
            name_size,
            attributes,
            position,
        )

    def _add_rar4_entry(
        self,
        header: bytes,
        offset: int,
        header_size: int,
        pack_size: int,
        unp_size: int,
        host_os: int,
        file_crc: int,
        file_time: int,
        method: int,
        name_size: int,
        attributes: int,
        position: int,
    ) -> int:
        # The 16-bit file flags live at bytes 3-4 of the block header.
        flags = struct.unpack_from("<H", header, 3)[0]
        if flags & 0x0100:  # LHD_LARGE: 64-bit sizes follow the fixed fields
            if position + 8 > len(header):
                raise CorruptArchive("A large RAR file header is truncated.")
            high_pack, high_unp = struct.unpack_from("<LL", header, position)
            position += 8
            pack_size |= high_pack << 32
            unp_size |= high_unp << 32
        if position + name_size > len(header):
            raise CorruptArchive("A RAR filename is truncated.")
        raw_name = header[position : position + name_size]
        name = self._decode_rar4_name(raw_name, flags)
        is_dir = (flags & 0x00E0) == 0x00E0 or (
            host_os == 0 and bool(attributes & 0x10)
        )
        if name.endswith(("/", "\\")):
            is_dir = True

        unsupported = None
        encrypted = bool(flags & 0x0004)
        split = bool(flags & 0x0003) or self._volume
        if encrypted:
            unsupported = "Encrypted"
        elif split:
            unsupported = "Multi-volume RAR"
        elif method != 0x30:
            unsupported = f"Compressed RAR entry (method 0x{method:02x})"
        elif pack_size != unp_size:
            unsupported = "Stored entry has inconsistent sizes"
        elif offset + header_size + pack_size > self.info.size:
            unsupported = "Truncated entry"

        cleaned, path_error = safe_entry_path(name, is_dir)
        if path_error:
            cleaned = f"unsafe-entry-{len(self.entries)}"
            unsupported = path_error
        if is_dir and not cleaned:
            return pack_size

        time = dos_datetime(file_time >> 16, file_time & 0xFFFF)
        if is_dir:
            pack_size = 0
        self.entries.append(
            Entry(
                path=cleaned,
                size=int(unp_size),
                compressed_size=int(pack_size),
                data_offset=offset + header_size,
                method=method,
                crc=int(file_crc) & 0xFFFFFFFF,
                mtime=time,
                is_dir=is_dir,
                stored=method == 0x30,
                encrypted=encrypted,
                unsupported=unsupported,
                raw_path=name,
                extra={"split": split, "host_os": host_os},
            )
        )
        return pack_size

    @staticmethod
    def _decode_rar4_name(raw: bytes, flags: int) -> str:
        if not (flags & 0x0200):
            raw = raw.split(b"\x00", 1)[0]
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("cp437", "replace")

        terminator = raw.find(b"\x00")
        if terminator < 0:
            raw = raw.split(b"\x00", 1)[0]
            try:
                return raw.decode("utf-8", "replace")
            except Exception:
                return raw.decode("latin-1", "replace")
        ascii_part = raw[:terminator]
        data = raw[terminator + 1 :]
        if not data:
            return ascii_part.decode("utf-8", "replace")
        high_byte = data[0]
        position = 1
        bits = 0
        flag_byte = 0
        units: list[int] = []
        limit = len(raw)  # matches libarchive's fn_end / 2 units
        while position < len(data) and len(units) < limit:
            if not bits:
                flag_byte = data[position]
                position += 1
                bits = 8
            bits -= 2
            code = (flag_byte >> bits) & 3
            if code == 0:
                if position >= len(data):
                    continue
                units.append(data[position])
                position += 1
            elif code == 1:
                if position >= len(data):
                    continue
                units.append((high_byte << 8) | data[position])
                position += 1
            elif code == 2:
                if position + 1 >= len(data):
                    position = len(data)
                    continue
                units.append((data[position + 1] << 8) | data[position])
                position += 2
            else:
                if position >= len(data):
                    continue
                length_byte = data[position]
                position += 1
                if length_byte & 0x80:
                    if position >= len(data):
                        continue
                    extra = data[position]
                    position += 1
                    high = high_byte
                else:
                    extra = 0
                    high = 0
                count = (length_byte & 0x7F) + 2
                while count and len(units) < limit:
                    index = len(units)
                    base_value = ascii_part[index] if index < len(ascii_part) else 0
                    units.append((high << 8) | ((base_value + extra) & 0xFF))
                    count -= 1
        encoded = b"".join(struct.pack(">H", unit & 0xFFFF) for unit in units)
        try:
            return encoded.decode("utf-16-be", "replace").split("\x00", 1)[0]
        except Exception:
            return ascii_part.decode("utf-8", "replace")

    # ------------------------------------------------------------------ RAR 5
    def _read_rar5(self, offset: int, progress=None) -> None:
        size = self.info.size
        count = 0
        while offset + 5 <= size and count < MAX_HEADERS:
            count += 1
            prefix = self._hash.read(offset, min(12, size - offset))
            header_crc = struct.unpack_from("<I", prefix, 0)[0]
            header_size, size_bytes = self._vint(prefix, 4)
            if size_bytes == 0 or size_bytes > 3:
                raise CorruptArchive("Invalid RAR5 header size field.")
            header_start = offset + 4 + size_bytes
            if header_size < 3 or header_size > MAX_HEADER_SIZE:
                raise CorruptArchive("Invalid RAR5 header size.")
            if header_start + header_size > size:
                raise CorruptArchive("A RAR5 header points past the end of the file.")
            header = self._hash.read(header_start, header_size)
            checksum_data = prefix[4 : 4 + size_bytes] + header
            if zlib.crc32(checksum_data) & 0xFFFFFFFF != header_crc:
                raise CorruptArchive("A RAR5 header failed its checksum.")

            position = 0
            header_type, position = self._vint_at(header, position)
            flags, position = self._vint_at(header, position)
            extra_size = 0
            data_size = 0
            if flags & 0x0001:
                extra_size, position = self._vint_at(header, position)
            if flags & 0x0002:
                data_size, position = self._vint_at(header, position)
            if extra_size > header_size:
                raise CorruptArchive("A RAR5 extra area is larger than its header.")

            if header_type == RAR5_MAIN:
                if position < header_size:
                    archive_flags, position = self._vint_at(header, position)
                    self._volume = bool(archive_flags & 0x0001)
            elif header_type == RAR5_FILE:
                self._parse_rar5_file(
                    header, header_start, header_size, data_size, extra_size, flags, position
                )
            elif header_type == RAR5_ENCRYPTION:
                raise UnsupportedArchive(
                    "This RAR has encrypted headers. Trawl cannot list it without the password."
                )
            elif header_type == RAR5_END:
                if position < header_size:
                    end_flags, _ = self._vint_at(header, position)
                    if end_flags & 0x0001:
                        self.warnings.append("This is not the last volume of the RAR set.")
                break

            next_offset = header_start + header_size + data_size
            if next_offset <= offset:
                raise CorruptArchive("The RAR5 header chain does not advance.")
            if next_offset > size:
                self.warnings.append("The RAR5 index ends past the end of the file.")
                break
            offset = next_offset
        if count >= MAX_HEADERS:
            self.warnings.append("Stopped after too many RAR headers.")

    def _parse_rar5_file(
        self,
        header: bytes,
        header_start: int,
        header_size: int,
        data_size: int,
        extra_size: int,
        flags: int,
        position: int,
    ) -> None:
        file_flags, position = self._vint_at(header, position)
        unpacked_size, position = self._vint_at(header, position)
        _attributes, position = self._vint_at(header, position)
        modified = None
        if file_flags & 0x0002:
            if position + 4 > header_size:
                raise CorruptArchive("A RAR5 timestamp is truncated.")
            modified = unix_datetime(struct.unpack_from("<I", header, position)[0])
            position += 4
        crc = None
        if file_flags & 0x0004:
            if position + 4 > header_size:
                raise CorruptArchive("A RAR5 checksum is truncated.")
            crc = struct.unpack_from("<I", header, position)[0]
            position += 4
        compression_info, position = self._vint_at(header, position)
        _host_os, position = self._vint_at(header, position)
        name_length, position = self._vint_at(header, position)
        extra_start = max(0, header_size - extra_size)
        if position + name_length > extra_start:
            raise CorruptArchive("A RAR5 filename is truncated.")
        raw_name = header[position : position + name_length]
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            name = raw_name.decode("utf-8", "replace")
        name = name.split("\x00", 1)[0]
        extra = header[extra_start:] if extra_size else b""
        encrypted = self._extra_has_type(extra, 1)
        method = (compression_info >> 7) & 0x7
        unknown_size = bool(file_flags & 0x0008)
        is_dir = bool(file_flags & 0x0001)
        split = bool(flags & 0x0008) or bool(flags & 0x0010) or self._volume

        unsupported = None
        if encrypted:
            unsupported = "Encrypted"
        elif split:
            unsupported = "Multi-volume RAR"
        elif unknown_size:
            unsupported = "Unknown uncompressed size"
        elif method != 0:
            unsupported = f"Compressed RAR entry (method {method})"
        elif data_size != unpacked_size:
            unsupported = "Stored entry has inconsistent sizes"
        elif header_start + header_size + data_size > self.info.size:
            unsupported = "Truncated entry"

        cleaned, path_error = safe_entry_path(name, is_dir)
        if path_error:
            cleaned = f"unsafe-entry-{len(self.entries)}"
            unsupported = path_error
        if is_dir and not cleaned:
            return

        self.entries.append(
            Entry(
                path=cleaned,
                size=int(unpacked_size),
                compressed_size=int(data_size if not is_dir else 0),
                data_offset=header_start + header_size,
                method=method,
                crc=crc,
                mtime=modified,
                is_dir=is_dir,
                stored=method == 0,
                encrypted=encrypted,
                unsupported=unsupported,
                raw_path=name,
                extra={"split": split},
            )
        )

    @staticmethod
    def _vint(data: bytes, position: int) -> tuple[int, int]:
        """Return ``(value, bytes_consumed)`` for a RAR vint at *position*."""
        value = 0
        shift = 0
        start = position
        while position < len(data) and shift <= 63:
            byte = data[position]
            position += 1
            value |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                return value, position - start
            shift += 7
        raise CorruptArchive("Invalid variable-length integer in a RAR header.")

    @staticmethod
    def _vint_at(data: bytes, position: int) -> tuple[int, int]:
        """Return ``(value, new_position)`` for a RAR vint at *position*."""
        value, consumed = RarArchive._vint(data, position)
        return value, position + consumed

    @staticmethod
    def _extra_has_type(extra: bytes, wanted: int) -> bool:
        position = 0
        while position < len(extra):
            size, consumed = RarArchive._vint(extra, position)
            if consumed == 0:
                return False
            position += consumed
            end = position + size
            if end > len(extra) or size == 0:
                return False
            record_type, type_consumed = RarArchive._vint(extra, position)
            if type_consumed == 0:
                return False
            if record_type == wanted:
                return True
            position = end
        return False

    # ------------------------------------------------------------------ data
    def iter_data(self, entry: Entry, start: int = 0) -> Iterator[bytes]:
        if entry.is_dir:
            return
        if entry.unsupported:
            raise ArchiveError(entry.unsupported)
        if start < 0 or start > entry.size:
            raise ArchiveError("Invalid resume offset.")
        if not entry.stored:
            raise ArchiveError("Only stored RAR entries can be extracted.")
        if start:
            yield from self.reader.iter_range(
                entry.data_offset + start, entry.size - start
            )
        else:
            yield from self.reader.iter_range(entry.data_offset, entry.size)
