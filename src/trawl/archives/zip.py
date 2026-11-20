from __future__ import annotations

import bz2
import stat
import struct
import zlib
from datetime import datetime
from typing import Iterator

from ..net import HTTPRangeReader, RemoteInfo
from ..util import dos_datetime, strip_common_root, unix_datetime
from .base import Archive, ArchiveError, ChunkedReader, CorruptArchive, Entry, safe_entry_path

try:  # Python 3.14+
    from compression import zstd as _zstd  # type: ignore

    _ZstdError: type[BaseException] | tuple = _zstd.ZstdError
except Exception:  # pragma: no cover - depends on the interpreter
    _zstd = None
    _ZstdError = ()

HAS_ZSTD = _zstd is not None

EOCD_SIGNATURE = b"PK\x05\x06"
ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
CENTRAL_SIGNATURE = b"PK\x01\x02"
LOCAL_SIGNATURE = b"PK\x03\x04"

METHOD_STORED = 0
METHOD_DEFLATE = 8
METHOD_BZIP2 = 12
METHOD_LZMA = 14
METHOD_ZSTD = 93
METHOD_AES = 99

MAX_CENTRAL_DIRECTORY = 128 * 1024 * 1024


class ZipArchive(Archive):
    kind = "ZIP"

    def __init__(self, reader: HTTPRangeReader, info: RemoteInfo):
        super().__init__(reader, info)
        self._cd_entries = 0
        self._cd_parsed = 0

    def read_index(self, progress=None) -> None:
        if progress:
            progress("Finding the ZIP directory")
        size = self.info.size
        if size < 22:
            raise CorruptArchive("This file is too small to be a ZIP archive.")
        tail_length = min(size, 65557)
        tail = self.reader.read_range(size - tail_length, tail_length)
        eocd_position = self._find_eocd(tail)
        if eocd_position is None:
            raise CorruptArchive("Could not find the ZIP end-of-directory record.")
        eocd_absolute = size - tail_length + eocd_position
        eocd = self.reader.read_range(eocd_absolute, 22)
        if len(eocd) != 22:
            raise CorruptArchive("The ZIP end-of-directory record is truncated.")
        (
            _signature,
            disk,
            cd_disk,
            entries_disk,
            entries_total,
            cd_size,
            cd_offset,
            comment_length,
        ) = struct.unpack("<4s4H2IH", eocd)

        if (
            entries_total == 0xFFFF
            or entries_disk == 0xFFFF
            or cd_size == 0xFFFFFFFF
            or cd_offset == 0xFFFFFFFF
            or disk == 0xFFFF
            or cd_disk == 0xFFFF
        ):
            entries_total, cd_size, cd_offset = self._read_zip64_directory(eocd_absolute, size)
            entries_disk = entries_total
            disk = cd_disk = 0

        if disk not in (0, 0xFFFF) or cd_disk not in (0, 0xFFFF):
            raise ArchiveError("Multi-disk ZIP archives are not supported.")
        if entries_total > 2_000_000:
            raise ArchiveError("This ZIP contains too many entries to display safely.")
        if cd_size <= 0:
            if entries_total == 0:
                self.entries = []
                if progress:
                    progress("Empty archive")
                return
            raise CorruptArchive("The ZIP central directory is empty.")
        if cd_size > MAX_CENTRAL_DIRECTORY:
            raise ArchiveError(
                f"The ZIP index is {cd_size / 1048576:.0f} MB, which is too large to load."
            )
        if cd_offset + cd_size > size:
            raise CorruptArchive("The ZIP central directory points past the end of the file.")

        if progress:
            progress(f"Reading {entries_total:,} ZIP entries")
        reader = ChunkedReader(self.reader, cd_offset, cd_size, chunk_size=512 * 1024)
        self._cd_entries = entries_total
        for index in range(entries_total):
            try:
                self._read_central_entry(reader, index)
            except struct.error as error:
                raise CorruptArchive("The ZIP central directory is truncated.") from error
            if progress and index and index % 20000 == 0:
                progress(f"Indexed {index:,} of {entries_total:,} files")
        self._cd_parsed = len(self.entries)
        if self._cd_parsed != entries_total:
            self.warnings.append(
                f"The ZIP directory claimed {entries_total:,} entries but {self._cd_parsed:,} were readable."
            )
        strip_common_root(self.entries)
        if progress:
            progress(f"Indexed {len(self.files):,} files")

    @staticmethod
    def _find_eocd(tail: bytes) -> int | None:
        position = tail.rfind(EOCD_SIGNATURE)
        fallback = None
        while position != -1:
            if position + 22 <= len(tail):
                if fallback is None:
                    fallback = position
                comment_length = struct.unpack_from("<H", tail, position + 20)[0]
                if position + 22 + comment_length == len(tail):
                    return position
            position = tail.rfind(EOCD_SIGNATURE, 0, position)
        return fallback

    def _read_zip64_directory(self, eocd_absolute: int, size: int) -> tuple[int, int, int]:
        locator_position = eocd_absolute - 20
        if locator_position < 0:
            raise CorruptArchive("The ZIP64 locator is missing.")
        locator = self.reader.read_range(locator_position, 20)
        if not locator.startswith(ZIP64_LOCATOR_SIGNATURE):
            raise CorruptArchive("The ZIP64 locator is missing or damaged.")
        _, _, zip64_offset, _ = struct.unpack("<4sIQI", locator)
        if zip64_offset + 56 > size:
            raise CorruptArchive("The ZIP64 end-of-directory record is out of bounds.")
        record = self.reader.read_range(zip64_offset, 56)
        if not record.startswith(ZIP64_EOCD_SIGNATURE):
            raise CorruptArchive("The ZIP64 end-of-directory record is damaged.")
        (
            _signature,
            _record_size,
            _version_made,
            _version_needed,
            _disk,
            _cd_disk,
            entries_disk,
            entries_total,
            cd_size,
            cd_offset,
        ) = struct.unpack("<4sQ2H2I4Q", record)
        if entries_disk not in (entries_total, 0xFFFFFFFFFFFFFFFF):
            raise CorruptArchive("The ZIP64 entry counts disagree.")
        return int(entries_total), int(cd_size), int(cd_offset)

    def _read_central_entry(self, reader: ChunkedReader, index: int) -> None:
        header = reader.read(46)
        if not header.startswith(CENTRAL_SIGNATURE):
            raise CorruptArchive("The ZIP central directory is malformed.")
        (
            _signature,
            version_made,
            _version_needed,
            flags,
            method,
            mod_time,
            mod_date,
            crc,
            compressed_size,
            uncompressed_size,
            name_length,
            extra_length,
            comment_length,
            disk_start,
            _internal_attributes,
            external_attributes,
            local_header_offset,
        ) = struct.unpack("<4s6H3I5H2I", header)

        name_bytes = reader.read(name_length)
        extra = reader.read(extra_length)
        if comment_length:
            reader.skip(comment_length)

        unix_mode = 0
        made_by = version_made >> 8
        if made_by in (3, 19):
            unix_mode = external_attributes >> 16

        # ZIP64 extended information, plus extra timestamps and a UTF-8 name.
        extra_fields = self._parse_extra(extra)
        if 0x0001 in extra_fields:
            data = extra_fields[0x0001]
            position = 0
            if uncompressed_size == 0xFFFFFFFF and position + 8 <= len(data):
                uncompressed_size = struct.unpack_from("<Q", data, position)[0]
                position += 8
            if compressed_size == 0xFFFFFFFF and position + 8 <= len(data):
                compressed_size = struct.unpack_from("<Q", data, position)[0]
                position += 8
            if local_header_offset == 0xFFFFFFFF and position + 8 <= len(data):
                local_header_offset = struct.unpack_from("<Q", data, position)[0]
                position += 8
            if disk_start == 0xFFFF and position + 4 <= len(data):
                disk_start = struct.unpack_from("<I", data, position)[0]

        name = self._decode_name(name_bytes, flags, extra_fields)
        modified = None
        if 0x5455 in extra_fields:
            data = extra_fields[0x5455]
            if len(data) >= 5 and data[0] & 1:
                modified = unix_datetime(struct.unpack_from("<I", data, 1)[0])
        if modified is None and 0x000A in extra_fields:
            modified = self._ntfs_time(extra_fields[0x000A])
        if modified is None:
            modified = dos_datetime(mod_date, mod_time)

        is_dir = name.endswith("/")
        if unix_mode:
            if stat.S_ISDIR(unix_mode):
                is_dir = True

        cleaned, path_error = safe_entry_path(name, is_dir)
        if path_error:
            cleaned = f"unsafe-entry-{index}"
        unsupported = path_error
        encrypted = bool(flags & 0x0001) or method == METHOD_AES
        if encrypted:
            unsupported = "Encrypted"
        elif disk_start not in (0, 0xFFFF):
            unsupported = "Multi-disk archive"
        elif unix_mode and stat.S_ISLNK(unix_mode):
            unsupported = "Symbolic link"
        elif method == METHOD_LZMA:
            unsupported = "LZMA-compressed ZIP entry"
        elif method == METHOD_ZSTD and not HAS_ZSTD:
            unsupported = "Zstandard-compressed ZIP entry (requires Python 3.14+)"
        elif method not in (METHOD_STORED, METHOD_DEFLATE, METHOD_BZIP2, METHOD_ZSTD):
            unsupported = f"Unsupported compression method {method}"
        elif method == METHOD_STORED and compressed_size != uncompressed_size:
            unsupported = "Stored entry has inconsistent sizes"

        if is_dir and not cleaned:
            return
        entry = Entry(
            path=cleaned,
            size=int(uncompressed_size),
            compressed_size=int(compressed_size),
            method=int(method),
            crc=int(crc) & 0xFFFFFFFF,
            mtime=modified,
            is_dir=is_dir,
            stored=method == METHOD_STORED,
            encrypted=encrypted,
            unsupported=unsupported,
            raw_path=name,
            extra={"local_header": int(local_header_offset), "flags": flags},
        )
        self.entries.append(entry)

    @staticmethod
    def _parse_extra(extra: bytes) -> dict[int, bytes]:
        fields: dict[int, bytes] = {}
        position = 0
        while position + 4 <= len(extra):
            header_id, size = struct.unpack_from("<HH", extra, position)
            position += 4
            data = extra[position : position + size]
            position += size
            fields[header_id] = data
        return fields

    @staticmethod
    def _decode_name(name_bytes: bytes, flags: int, extra_fields: dict[int, bytes]) -> str:
        if 0x7075 in extra_fields:
            data = extra_fields[0x7075]
            if len(data) >= 5 and data[0] == 1:
                expected_crc = struct.unpack_from("<I", data, 1)[0]
                if zlib.crc32(name_bytes) & 0xFFFFFFFF == expected_crc:
                    try:
                        return data[5:].decode("utf-8")
                    except UnicodeDecodeError:
                        pass
        if flags & 0x0800:
            return name_bytes.decode("utf-8", "replace")
        return name_bytes.decode("cp437", "replace")

    @staticmethod
    def _ntfs_time(data: bytes) -> datetime | None:
        if len(data) < 8 + 4 + 24:
            return None
        # Reserved(4), attribute tag(2), size(2), mtime(8), atime(8), ctime(8)
        position = 4
        while position + 4 <= len(data):
            tag, size = struct.unpack_from("<HH", data, position)
            position += 4
            if tag == 1 and size >= 24 and position + 24 <= len(data):
                filetime = struct.unpack_from("<Q", data, position)[0]
                if filetime == 0:
                    return None
                try:
                    seconds = (filetime - 116444736000000000) / 10_000_000
                    return datetime.fromtimestamp(seconds)
                except (OverflowError, OSError, ValueError):
                    return None
            position += size
        return None

    def _data_offset(self, entry: Entry) -> int:
        cached = entry.extra.get("data_offset")
        if cached is not None:
            return int(cached)
        local_offset = int(entry.extra.get("local_header", 0))
        header = self.reader.read_range(local_offset, 30)
        if len(header) != 30 or not header.startswith(LOCAL_SIGNATURE):
            raise CorruptArchive(f"Missing ZIP local header for {entry.path}.")
        name_length, extra_length = struct.unpack_from("<HH", header, 26)
        data_offset = local_offset + 30 + name_length + extra_length
        entry.extra["data_offset"] = data_offset
        return data_offset

    def iter_data(self, entry: Entry, start: int = 0) -> Iterator[bytes]:
        if entry.is_dir:
            return
        if entry.unsupported:
            raise ArchiveError(entry.unsupported)
        if start < 0 or start > entry.size:
            raise ArchiveError("Invalid resume offset.")
        data_offset = self._data_offset(entry)
        if entry.method == METHOD_STORED:
            if start:
                yield from self.reader.iter_range(data_offset + start, entry.size - start)
            else:
                yield from self.reader.iter_range(data_offset, entry.size)
            return

        if start:
            raise ArchiveError("Compressed entries cannot be resumed mid-stream.")
        if entry.compressed_size <= 0 and entry.size > 0:
            raise CorruptArchive(f"{entry.path} has no compressed data.")

        output_size = 0
        try:
            if entry.method == METHOD_DEFLATE:
                decompressor = zlib.decompressobj(-15)
                for chunk in self.reader.iter_range(data_offset, entry.compressed_size):
                    data = decompressor.decompress(chunk)
                    if data:
                        output_size += len(data)
                        if output_size > entry.size:
                            raise CorruptArchive(f"{entry.path} inflates past its stated size.")
                        yield data
                tail = decompressor.flush()
                if tail:
                    output_size += len(tail)
                    if output_size > entry.size:
                        raise CorruptArchive(f"{entry.path} inflates past its stated size.")
                    yield tail
            elif entry.method == METHOD_BZIP2:
                decompressor = bz2.BZ2Decompressor()
                for chunk in self.reader.iter_range(data_offset, entry.compressed_size):
                    data = decompressor.decompress(chunk)
                    if data:
                        output_size += len(data)
                        yield data
            elif entry.method == METHOD_ZSTD:
                if _zstd is None:
                    raise ArchiveError("Zstandard support requires Python 3.14 or newer.")
                decompressor = _zstd.ZstdDecompressor()
                for chunk in self.reader.iter_range(data_offset, entry.compressed_size):
                    data = decompressor.decompress(chunk)
                    if data:
                        output_size += len(data)
                        yield data
            else:
                raise ArchiveError(f"Unsupported compression method {entry.method}.")
        except (zlib.error, OSError, EOFError, _ZstdError) as error:
            raise CorruptArchive(f"Could not decompress {entry.path}: {error}") from error
        if output_size != entry.size:
            raise CorruptArchive(
                f"{entry.path} extracted to {output_size:,} bytes, expected {entry.size:,}."
            )
