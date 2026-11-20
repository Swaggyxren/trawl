"""Tiny, deterministic archive fixtures built with the standard library only."""

from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path

SECTOR_SIZE = 2048


def make_zip(path: Path) -> dict[str, bytes]:
    contents = {
        "Project/docs/readme.txt": b"Hello from a remote ZIP!\n" * 12,
        "Project/data/big.bin": bytes((index * 37) % 256 for index in range(90000)),
        "assets/logo.png": b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 20,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Project/docs/readme.txt", contents["Project/docs/readme.txt"])
        info = zipfile.ZipInfo("Project/data/big.bin")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, contents["Project/data/big.bin"])
        archive.writestr("assets/logo.png", contents["assets/logo.png"])
    return contents


def make_zip_with_unsafe_entry(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("../escape.txt", b"do not write me")


def _rar4_header(header_type: int, flags: int, size: int, body: bytes = b"") -> bytes:
    header = struct.pack("<HBHH", 0, header_type, flags, size) + body
    return struct.pack("<H", zlib.crc32(header[2:]) & 0xFFFF) + header[2:]


def make_rar4(path: Path) -> bytes:
    data = b"STORED RAR4 DATA\n"
    name = b"hello.txt"
    main = _rar4_header(0x73, 0, 13, b"\x00\x00" + struct.pack("<I", 0))
    fields = struct.pack(
        "<LLBLLBBHL",
        len(data),
        len(data),
        3,
        zlib.crc32(data) & 0xFFFFFFFF,
        0,
        20,
        0x30,
        len(name),
        0,
    )
    file_header = _rar4_header(0x74, 0, 32 + len(name), fields + name)
    end = _rar4_header(0x7B, 0, 7)
    path.write_bytes(b"Rar!\x1a\x07\x00" + main + file_header + data + end)
    return data


def _vint(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            result.append(byte | 0x80)
        else:
            result.append(byte)
            return bytes(result)


def _rar5_block(header_type: int, flags: int, body: bytes, data: bytes = b"") -> bytes:
    header = _vint(header_type) + _vint(flags) + body
    header_size = _vint(len(header))
    checksum = zlib.crc32(header_size + header) & 0xFFFFFFFF
    return struct.pack("<I", checksum) + header_size + header + data


def make_rar5(path: Path) -> bytes:
    data = b"STORED RAR5 DATA\n"
    name = b"hello5.txt"
    main = _rar5_block(1, 0, _vint(0))
    file_flags = 0x0004  # CRC32 is present
    fields = (
        _vint(file_flags)
        + _vint(len(data))
        + _vint(0)
        + struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
        + _vint(0)  # compression information: stored
        + _vint(0)  # host OS: Windows
        + _vint(len(name))
        + name
    )
    file_header = _rar5_block(2, 0x0002, _vint(len(data)) + fields, data)
    end = _rar5_block(5, 0, _vint(0))
    path.write_bytes(b"Rar!\x1a\x07\x01\x00" + main + file_header + end)
    return data


def _iso_record(extent: int, size: int, flags: int, name: bytes) -> bytes:
    name = name.encode("ascii") if isinstance(name, str) else name
    length = 33 + len(name)
    if length % 2:
        length += 1
    record = bytearray(length)
    record[0] = length
    struct.pack_into("<I", record, 2, extent)
    struct.pack_into("<I", record, 10, size)
    record[18:25] = bytes([120, 1, 1, 0, 0, 0, 0])
    record[25] = flags
    record[32] = len(name)
    record[33 : 33 + len(name)] = name
    return bytes(record)


def _iso_sector(data: bytes) -> bytes:
    data = data[:SECTOR_SIZE]
    return data + b"\x00" * (SECTOR_SIZE - len(data))


def make_iso(path: Path) -> dict[str, bytes]:
    root_file = b"Hello ISO!\n"
    nested_file = b"Nested!\n"
    image = bytearray(b"\x00" * (16 * SECTOR_SIZE))

    pvd = bytearray(SECTOR_SIZE)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[40:72] = b"TRAWL_TEST".ljust(32, b" ")
    pvd[156 : 156 + 34] = _iso_record(17, SECTOR_SIZE, 0x02, b"\x00")
    struct.pack_into("<I", pvd, 80, 21)
    image += _iso_sector(pvd)

    root = _iso_record(17, SECTOR_SIZE, 0x02, b"\x00")
    root += _iso_record(17, SECTOR_SIZE, 0x02, b"\x01")
    root += _iso_record(18, len(root_file), 0, b"README.TXT;1")
    root += _iso_record(19, SECTOR_SIZE, 0x02, b"SUBDIR")
    image += _iso_sector(root)
    image += _iso_sector(root_file)

    subdir = _iso_record(19, SECTOR_SIZE, 0x02, b"\x00")
    subdir += _iso_record(19, SECTOR_SIZE, 0x02, b"\x01")
    subdir += _iso_record(20, len(nested_file), 0, b"NESTED.TXT;1")
    image += _iso_sector(subdir)
    image += _iso_sector(nested_file)

    terminator = bytearray(SECTOR_SIZE)
    terminator[0] = 255
    terminator[1:6] = b"CD001"
    terminator[6] = 1
    image += _iso_sector(terminator)

    path.write_bytes(image)
    return {"README.TXT": root_file, "SUBDIR/NESTED.TXT": nested_file}
