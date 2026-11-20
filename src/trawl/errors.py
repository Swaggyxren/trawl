from __future__ import annotations

from .archives.base import ArchiveError, CorruptArchive, UnsupportedArchive
from .net import (
    HTTPStatusError,
    IncompleteResponse,
    RangeUnsupported,
    TooManyRedirects,
    TrawlNetError,
)


def error_code(error: Exception) -> str:
    """Short, stable diagnostic code for *error* (never raw exception text)."""
    if isinstance(error, ValueError):
        return "invalid_url"
    if isinstance(error, RangeUnsupported):
        return "range_unsupported"
    if isinstance(error, HTTPStatusError):
        if error.status in (401, 403):
            return "unauthorized"
        if error.status == 404:
            return "not_found"
        if error.status == 429:
            return "rate_limited"
        return "http_error"
    if isinstance(error, TooManyRedirects):
        return "too_many_redirects"
    if isinstance(error, IncompleteResponse):
        return "incomplete_response"
    if isinstance(error, CorruptArchive):
        if "checksum" in str(error).lower():
            return "crc_mismatch"
        return "corrupt_archive"
    if isinstance(error, UnsupportedArchive):
        return "unsupported_archive"
    if isinstance(error, TrawlNetError):
        return "unreachable"
    if isinstance(error, ArchiveError):
        return "archive_error"
    if isinstance(error, OSError):
        return "io_error"
    return "unknown"


def friendly_message(error: Exception) -> str:
    """Plain-language message for *error*, safe to show in the UI."""
    code = error_code(error)
    messages = {
        "invalid_url": "That isn't a valid link.",
        "range_unsupported": "This server doesn't allow partial downloads.",
        "unauthorized": "This link needs a login or token.",
        "not_found": "That archive was not found.",
        "rate_limited": "The server is rate-limiting requests. Try slow mode.",
        "http_error": "The server refused the request.",
        "too_many_redirects": "This link redirects too many times.",
        "incomplete_response": "The connection dropped before the file finished.",
        "crc_mismatch": "A downloaded file failed its checksum.",
        "corrupt_archive": "This archive looks damaged or incomplete.",
        "unsupported_archive": "This archive format is not supported.",
        "archive_error": "Trawl could not read this archive.",
        "unreachable": "Could not reach the server.",
        "io_error": "Could not read or write a local file.",
    }
    return messages.get(code, "Something went wrong while opening this archive.")
