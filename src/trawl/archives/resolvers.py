from __future__ import annotations

import html
import re
import urllib.parse
from typing import Iterable

from ..net import TrawlNetError, normalize_url

ARCHIVE_EXTENSIONS = (
    ".zip",
    ".zip64",
    ".rar",
    ".r00",
    ".iso",
    ".img",
    ".cbz",
    ".jar",
    ".apk",
    ".epub",
    ".whl",
)

_UNSUPPORTED_EXTENSIONS = {
    ".7z": "7z archives are not supported yet.",
    ".tar.gz": "tar.gz archives are not supported yet. Trawl needs random access to an index.",
    ".tgz": "tar.gz archives are not supported yet.",
    ".gz": "gzip streams are not supported yet.",
    ".bz2": "bzip2 streams are not supported yet.",
    ".xz": "xz streams are not supported yet.",
    ".udf": "UDF-only ISO images are not supported yet.",
}


class UnsupportedHost(TrawlNetError):
    pass


class NotAnArchive(TrawlNetError):
    pass


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _path(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).path
    except ValueError:
        return ""


def _with_query(url: str, **params: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def resolve_candidates(url: str) -> list[str]:
    """Return candidate direct-download URLs for a pasted link, in priority order."""
    url = normalize_url(url)
    host = _host(url)
    path = _path(url)
    candidates: list[str] = []

    if host in ("drive.google.com", "docs.google.com"):
        raise UnsupportedHost(
            "Google Drive links do not expose byte ranges in a stable way, so Trawl "
            "cannot stream an archive from Drive. Download it first, or use a direct host."
        )
    if host == "mega.nz" or host.endswith(".mega.nz"):
        raise UnsupportedHost(
            "Mega links are encrypted end-to-end in the browser, so a range read is not possible."
        )
    if host in ("www.mediafire.com", "mediafire.com"):
        # The page is scraped once we know it is HTML.
        candidates.append(url)
    elif host in ("pixeldrain.com", "www.pixeldrain.com"):
        match = re.match(r"^/u/([^/]+)/?$", path)
        if match:
            candidates.append(f"https://pixeldrain.com/api/file/{match.group(1)}?download")
        elif path.startswith("/api/file/") and "download" not in urllib.parse.urlsplit(url).query:
            candidates.append(_with_query(url, download=""))
        else:
            candidates.append(url)
    elif host in ("www.dropbox.com", "dropbox.com"):
        if "/s/" in path or "/scl/" in path:
            candidates.append(_with_query(url, dl="1"))
        else:
            candidates.append(url)
    elif host == "buzzheavier.com" or host.endswith(".buzzheavier.com"):
        candidates.append(url)
        if not path.rstrip("/").endswith("/download"):
            candidates.append(url.rstrip("/") + "/download")
    else:
        candidates.append(url)

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def unsupported_extension_message(url: str) -> str | None:
    path = _path(url).lower()
    for suffix, message in _UNSUPPORTED_EXTENSIONS.items():
        if path.endswith(suffix):
            return message
    return None


def _decode_page(data: str) -> str:
    text = html.unescape(data)
    text = text.replace("\\/", "/")
    return text


def _looks_like_archive(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(ARCHIVE_EXTENSIONS)


def _looks_like_download(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    lowered = (parts.path + "?" + parts.query).lower()
    return any(token in lowered for token in ("download", "dl=1", "/file/", "getfile"))


def scrape_archive_url(page: str, base_url: str) -> str | None:
    """Best-effort extraction of a direct archive link from a landing page."""
    text = _decode_page(page)
    candidates: list[str] = []

    direct_patterns = [
        r"https?://download[^\"'\\\s<>]+mediafire\.com/[^\"'\\\s<>]+",
        r"https?://[^\"'\\\s<>]+\.(?:zip|zip64|rar|iso|img|cbz|jar|apk|epub|whl)(?:\?[^\"'\\\s<>]*)?",
    ]
    for pattern in direct_patterns:
        candidates.extend(re.findall(pattern, text, re.IGNORECASE))

    # href/src/data-* attributes, including root-relative links.
    for match in re.finditer(
        r"(?:href|src|data-url|data-href)\s*=\s*[\"']([^\"']+)[\"']", text, re.IGNORECASE
    ):
        candidates.append(match.group(1))

    # Meta refresh targets.
    for match in re.finditer(
        r"http-equiv\s*=\s*[\"']?refresh[\"']?[^>]*content\s*=\s*[\"'][^\"']*url=([^\"'>]+)",
        text,
        re.IGNORECASE,
    ):
        candidates.append(match.group(1))

    resolved: list[tuple[int, str]] = []
    seen: set[str] = set()
    base_host = _host(base_url)
    for candidate in candidates:
        candidate = candidate.strip().strip("\"'")
        if not candidate or candidate.startswith(("#", "javascript:", "mailto:", "data:")):
            continue
        absolute = urllib.parse.urljoin(base_url, candidate)
        try:
            absolute = normalize_url(absolute)
        except ValueError:
            continue
        if absolute in seen or absolute == base_url:
            continue
        seen.add(absolute)
        if _looks_like_archive(absolute):
            score = 0
        elif _looks_like_download(absolute):
            score = 1
        else:
            continue
        # Same-host links are more likely to be the real file.
        if _host(absolute) == base_host:
            score -= 1
        resolved.append((score, absolute))

    if not resolved:
        return None
    resolved.sort(key=lambda item: item[0])
    return resolved[0][1]


def describe_host(url: str) -> str:
    host = _host(url)
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    return host or "direct link"


def name_from_url(url: str, fallback: str = "archive") -> str:
    try:
        path = urllib.parse.urlsplit(url).path
        name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    except Exception:
        name = ""
    return name or fallback
