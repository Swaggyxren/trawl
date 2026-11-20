from __future__ import annotations

import base64
import http.client
import ipaddress
import logging
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import __version__
from .util import sanitize_filename

log = logging.getLogger(__name__)

USER_AGENT = f"Trawl/{__version__} (Linux; GTK; +https://github.com/Swaggyxren/trawl)"


class TrawlNetError(Exception):
    """Base class for network errors shown to the user."""


class RangeUnsupported(TrawlNetError):
    def __init__(self, info: "RemoteInfo"):
        self.info = info
        super().__init__(
            "This server ignores HTTP byte-range requests, so Trawl cannot "
            "read the archive index without downloading the whole file."
        )


class HTTPStatusError(TrawlNetError):
    def __init__(self, status: int, reason: str, url: str, detail: str = ""):
        self.status = status
        self.reason = reason
        self.url = url
        self.detail = detail
        super().__init__(self._friendly())

    def _friendly(self) -> str:
        common = {
            400: "The server rejected the request (400).",
            401: "This link needs a login or token.",
            403: "The server refused access to this link (403).",
            404: "The archive was not found (404).",
            407: "The server requires proxy authentication.",
            410: "This link has expired (410).",
            416: "The server could not satisfy the requested byte range.",
            429: "The server is rate-limiting requests (429). Try again later, or enable slow mode.",
        }
        base = common.get(self.status, f"The server returned HTTP {self.status} {self.reason}.")
        if self.detail:
            return f"{base}\n\n{self.detail}"
        return base


class TooManyRedirects(TrawlNetError):
    pass


class IncompleteResponse(TrawlNetError):
    pass


@dataclass(slots=True)
class RemoteInfo:
    url: str
    final_url: str
    size: int
    filename: str
    content_type: str = ""
    etag: str = ""
    last_modified: str = ""
    accepts_ranges: bool = True
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_html(self) -> bool:
        return "text/html" in self.content_type.lower() or "application/xhtml" in self.content_type.lower()

    @property
    def host(self) -> str:
        try:
            return urllib.parse.urlsplit(self.final_url).hostname or ""
        except ValueError:
            return ""


INVALID_LINK_MESSAGE = (
    "That doesn't look like a link. Paste the full http(s) link to a "
    "ZIP, RAR or ISO file."
)

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_HOSTISH_RE = re.compile(r"^[A-Za-z0-9._-]+(?::\d+)?(?:/|$)")


def validate_link(text: str) -> tuple[str | None, str]:
    """Validate pasted input and return ``(url, error)``.

    Only http(s) links with a real host are accepted. A bare ``host/path`` is
    allowed to become https://; free text, whitespace and over-long input are
    rejected before any session or log entry is created.
    """
    value = (text or "").strip().strip("<>").strip()
    if not value or len(value) > 2048 or any(char.isspace() for char in value):
        return None, INVALID_LINK_MESSAGE
    if not _SCHEME_RE.match(value) and _HOSTISH_RE.match(value):
        value = "https://" + value
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None, INVALID_LINK_MESSAGE
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None, INVALID_LINK_MESSAGE
    host = (parts.hostname or "").lower()
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if not host or not (is_ip or host == "localhost" or "." in host):
        return None, INVALID_LINK_MESSAGE
    try:
        return normalize_url(value), ""
    except ValueError:
        return None, INVALID_LINK_MESSAGE


def normalize_url(text: str) -> str:
    text = (text or "").strip().strip("<>").strip()
    if not text:
        raise ValueError("Paste a link first.")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("Only http and https links are supported.")
    if not parts.netloc:
        raise ValueError("That does not look like a valid link.")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _quote_path(parts: urllib.parse.SplitResult) -> str:
    path = parts.path or "/"
    path = urllib.parse.quote(path, safe="/%:@!$&'()*+,;=-._~")
    query = urllib.parse.quote(parts.query, safe="=&?/:;+,%@[]!$'()*~-._")
    return path + (("?" + query) if query else "")


def _content_disposition_filename(value: str) -> str:
    if not value:
        return ""
    match = re.search(r"filename\*\s*=\s*([^']*)''([^;]+)", value, re.IGNORECASE)
    if match:
        charset = match.group(1).strip() or "utf-8"
        raw = match.group(2).strip().strip('"')
        try:
            return urllib.parse.unquote(raw, encoding=charset, errors="replace")
        except LookupError:
            return urllib.parse.unquote(raw, errors="replace")
    match = re.search(r'filename\s*=\s*"([^"]+)"', value, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"filename\s*=\s*([^;]+)", value, re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"')
    return ""


def filename_from_info(final_url: str, content_disposition: str = "") -> str:
    name = _content_disposition_filename(content_disposition)
    if not name:
        try:
            path = urllib.parse.urlsplit(final_url).path
            name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
        except Exception:
            name = ""
    name = sanitize_filename(name, "archive")
    return name


def _parse_content_range(value: str) -> tuple[int | None, int | None, int | None]:
    """Return (start, end, total) from a Content-Range header."""
    if not value:
        return None, None, None
    match = re.match(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", value, re.IGNORECASE)
    if match:
        total = None if match.group(3) == "*" else int(match.group(3))
        return int(match.group(1)), int(match.group(2)), total
    match = re.match(r"\s*bytes\s+\*/(\d+)\s*$", value, re.IGNORECASE)
    if match:
        return None, None, int(match.group(1))
    return None, None, None


class HTTPRangeReader:
    """A small, thread-safe HTTP range client built on :mod:`http.client`.

    Each worker thread gets its own keep-alive connections. Range requests
    are retried from the last byte received, so a dropped connection does
    not restart a large file.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        insecure: bool = False,
        timeout: float = 30.0,
        rate_limit: bool = False,
        max_retries: int = 4,
        page_fetcher=None,
    ):
        self.original_url = url
        self._timeout = timeout
        self._insecure = insecure
        self._rate_limit = rate_limit
        self._max_retries = max(0, int(max_retries))
        self._page_fetcher = page_fetcher
        self._lock = threading.RLock()
        self._local = threading.local()
        self._all_connections: set[http.client.HTTPConnection] = set()
        self._next_request_at = 0.0

        # Pull credentials out of the URL so they are not sent in the request line.
        parts = urllib.parse.urlsplit(url)
        self._auth_header = ""
        if parts.username is not None:
            user = urllib.parse.unquote(parts.username)
            password = urllib.parse.unquote(parts.password or "")
            token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
            self._auth_header = f"Basic {token}"
            netloc = parts.hostname or ""
            if ":" in netloc and not netloc.startswith("["):
                netloc = f"[{netloc}]"
            if parts.port:
                netloc += f":{parts.port}"
            url = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))

        self.url = url
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.info: RemoteInfo | None = None
        self._closed = False

    # ------------------------------------------------------------------ setup
    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self._insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _host_key(self, url: str) -> tuple[str, str, int]:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname or ""
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            pass
        port = parts.port or (443 if scheme == "https" else 80)
        return scheme, host, port

    def _connection(self, key: tuple[str, str, int]) -> http.client.HTTPConnection:
        connections = getattr(self._local, "connections", None)
        if connections is None:
            connections = {}
            self._local.connections = connections
        connection = connections.get(key)
        if connection is None:
            scheme, host, port = key
            if scheme == "https":
                connection = http.client.HTTPSConnection(
                    host, port, timeout=self._timeout, context=self._ssl_context()
                )
            else:
                connection = http.client.HTTPConnection(host, port, timeout=self._timeout)
            connections[key] = connection
            with self._lock:
                self._all_connections.add(connection)
        return connection

    def _drop_connection(self, key: tuple[str, str, int], connection: http.client.HTTPConnection) -> None:
        connections = getattr(self._local, "connections", None)
        if connections is not None and connections.get(key) is connection:
            connections.pop(key, None)
        with self._lock:
            self._all_connections.discard(connection)
        try:
            connection.close()
        except Exception:
            pass

    def close(self) -> None:
        self._closed = True
        with self._lock:
            connections = list(self._all_connections)
            self._all_connections.clear()
        for connection in connections:
            try:
                connection.close()
            except Exception:
                pass
        local = getattr(self._local, "connections", None)
        if isinstance(local, dict):
            local.clear()

    # --------------------------------------------------------------- requests
    def _throttle(self) -> None:
        if not self._rate_limit:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_request_at - now
            if wait <= 0:
                self._next_request_at = now + 0.35
                return
            self._next_request_at = max(self._next_request_at, now) + 0.35
        if wait > 0:
            time.sleep(min(wait, 2.0))

    def _apply_headers(
        self,
        request: http.client.HTTPConnection,
        path: str,
        headers: dict[str, str],
        method: str,
    ) -> None:
        request.putrequest(method, path, skip_accept_encoding=True)
        request.putheader("User-Agent", USER_AGENT)
        request.putheader("Accept", "*/*")
        request.putheader("Accept-Encoding", "identity")
        request.putheader("Connection", "keep-alive")
        if self._auth_header and not any(k.lower() == "authorization" for k in self.headers):
            request.putheader("Authorization", self._auth_header)
        merged = dict(self.headers)
        merged.update(headers)
        for name, value in merged.items():
            request.putheader(name, value)

    def _request_once(
        self, url: str, headers: dict[str, str], method: str = "GET"
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, str, tuple[str, str, int]]:
        current = url
        for _ in range(12):
            parts = urllib.parse.urlsplit(current)
            if parts.scheme.lower() not in ("http", "https"):
                raise TrawlNetError(f"Redirected to an unsupported URL scheme: {parts.scheme}")
            key = self._host_key(current)
            path = _quote_path(parts)
            connection = self._connection(key)
            try:
                self._apply_headers(connection, path, headers, method)
                connection.endheaders()
                response = connection.getresponse()
            except (OSError, http.client.HTTPException, ssl.SSLError):
                self._drop_connection(key, connection)
                raise

            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                try:
                    response.read(64 * 1024)
                except Exception:
                    pass
                if response.will_close:
                    self._drop_connection(key, connection)
                if not location:
                    raise TrawlNetError("The server redirected the request without a location.")
                current = urllib.parse.urljoin(current, location)
                continue
            return connection, response, current, key
        raise TooManyRedirects("Too many redirects while fetching this link.")

    def _sleep_backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = 0.0
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 0.0
        if delay <= 0:
            delay = min(2.0 ** attempt, 12.0)
        if self._rate_limit:
            delay = max(delay, 1.0)
        delay += random.uniform(0.0, 0.4)
        time.sleep(min(delay, 60.0))

    def _request_with_retries(
        self, url: str, headers: dict[str, str], method: str = "GET"
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, str, tuple[str, str, int]]:
        attempt = 0
        last_error: Exception | None = None
        while True:
            if self._closed:
                raise TrawlNetError("The connection was closed.")
            self._throttle()
            try:
                connection, response, final_url, key = self._request_once(url, headers, method)
            except (OSError, http.client.HTTPException, ssl.SSLError) as error:
                last_error = error
                if attempt >= self._max_retries:
                    raise TrawlNetError(f"Could not reach the server: {error}") from error
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            if response.status in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                retry_after = response.getheader("Retry-After")
                try:
                    response.read(32 * 1024)
                except Exception:
                    pass
                if response.will_close:
                    self._drop_connection(key, connection)
                self._sleep_backoff(attempt, retry_after)
                attempt += 1
                continue
            return connection, response, final_url, key

    def _finish(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
        key: tuple[str, str, int],
    ) -> None:
        if response.will_close:
            self._drop_connection(key, connection)

    def _error_detail(self, response: http.client.HTTPResponse) -> str:
        try:
            raw = response.read(4096)
        except Exception:
            raw = b""
        text = raw.decode("utf-8", "replace").strip()
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:300]

    # ------------------------------------------------------------------- probe
    def probe(self) -> RemoteInfo:
        if self.info is not None:
            return self.info
        connection, response, final_url, key = self._request_with_retries(
            self.url, {"Range": "bytes=0-0"}
        )
        try:
            content_type = response.getheader("Content-Type", "") or ""
            content_disposition = response.getheader("Content-Disposition", "") or ""
            encoding = (response.getheader("Content-Encoding", "") or "").lower()
            etag = response.getheader("ETag", "") or ""
            last_modified = response.getheader("Last-Modified", "") or ""
            if encoding and encoding not in ("identity",):
                raise TrawlNetError(
                    "The server is compressing the response; byte ranges would not map to the file."
                )
            filename = filename_from_info(final_url, content_disposition)
            if response.status == 206:
                start, end, total = _parse_content_range(response.getheader("Content-Range", ""))
                if start != 0:
                    raise TrawlNetError("The server returned an unexpected byte range.")
                if total is None:
                    try:
                        response.read(1)
                    except Exception:
                        pass
                    total = self._head_size(final_url)
                try:
                    response.read(1)
                except Exception:
                    pass
                self._finish(connection, response, key)
                if total is None:
                    raise TrawlNetError("The server did not report the archive's size.")
                info = RemoteInfo(
                    url=self.url,
                    final_url=final_url,
                    size=int(total),
                    filename=filename,
                    content_type=content_type,
                    etag=etag,
                    last_modified=last_modified,
                    accepts_ranges=True,
                    headers={k.lower(): v for k, v in response.getheaders()},
                )
                self.info = info
                return info

            if response.status == 200:
                try:
                    size = int(response.getheader("Content-Length", "0") or 0)
                except ValueError:
                    size = 0
                # Do not read the body: close this connection immediately.
                self._drop_connection(key, connection)
                info = RemoteInfo(
                    url=self.url,
                    final_url=final_url,
                    size=size,
                    filename=filename,
                    content_type=content_type,
                    etag=etag,
                    last_modified=last_modified,
                    accepts_ranges=False,
                    headers={k.lower(): v for k, v in response.getheaders()},
                )
                raise RangeUnsupported(info)

            if response.status == 416:
                _, _, total = _parse_content_range(response.getheader("Content-Range", ""))
                self._finish(connection, response, key)
                if total == 0:
                    info = RemoteInfo(
                        url=self.url,
                        final_url=final_url,
                        size=0,
                        filename=filename,
                        content_type=content_type,
                        etag=etag,
                        last_modified=last_modified,
                        accepts_ranges=True,
                        headers={k.lower(): v for k, v in response.getheaders()},
                    )
                    self.info = info
                    return info

            detail = self._error_detail(response)
            self._finish(connection, response, key)
            raise HTTPStatusError(response.status, response.reason or "", final_url, detail)
        finally:
            if response.isclosed() is False:
                try:
                    response.close()
                except Exception:
                    pass
                self._drop_connection(key, connection)
            self._finish(connection, response, key)

    def _head_size(self, url: str) -> int | None:
        try:
            connection, response, _, key = self._request_with_retries(url, {}, method="HEAD")
        except Exception:
            return None
        try:
            if response.status == 200:
                value = response.getheader("Content-Length")
                return int(value) if value else None
            return None
        except (TypeError, ValueError):
            return None
        finally:
            self._finish(connection, response, key)

    # ------------------------------------------------------------------- reads
    def _open_range(
        self, start: int, end: int
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, tuple[str, str, int]]:
        connection, response, _, key = self._request_with_retries(
            self.url, {"Range": f"bytes={start}-{end}"}
        )
        if response.status == 206:
            r_start, _, _ = _parse_content_range(response.getheader("Content-Range", ""))
            if r_start != start:
                self._drop_connection(key, connection)
                raise TrawlNetError("The server returned a different byte range than requested.")
            return connection, response, key
        if response.status == 200:
            self._drop_connection(key, connection)
            info = self.info
            if info is not None:
                raise RangeUnsupported(info)
            raise TrawlNetError("The server ignored the byte-range request.")
        detail = self._error_detail(response)
        self._finish(connection, response, key)
        raise HTTPStatusError(response.status, response.reason or "", self.url, detail)

    def read_range(self, start: int, length: int) -> bytes:
        if length <= 0:
            return b""
        return b"".join(self.iter_range(start, length))

    def iter_range(self, start: int, length: int, chunk_size: int = 262144) -> Iterator[bytes]:
        if length <= 0:
            return
        if start < 0:
            raise ValueError("Negative range start")
        end = start + length - 1
        position = start
        failures = 0
        while position <= end:
            try:
                connection, response, key = self._open_range(position, end)
            except TrawlNetError:
                raise
            except (OSError, http.client.HTTPException) as error:
                failures += 1
                if failures > self._max_retries:
                    raise TrawlNetError(f"Download interrupted: {error}") from error
                self._sleep_backoff(failures)
                continue

            try:
                while position <= end:
                    to_read = min(chunk_size, end - position + 1)
                    try:
                        data = response.read(to_read)
                    except (OSError, http.client.HTTPException) as error:
                        raise IncompleteResponse(str(error)) from error
                    if not data:
                        raise IncompleteResponse(
                            f"The connection closed with {end - position + 1} bytes left."
                        )
                    position += len(data)
                    failures = 0
                    yield data
            except GeneratorExit:
                try:
                    response.close()
                except Exception:
                    pass
                self._drop_connection(key, connection)
                raise
            except IncompleteResponse as error:
                failures += 1
                self._drop_connection(key, connection)
                if failures > self._max_retries:
                    raise TrawlNetError(f"Download interrupted: {error}") from error
                self._sleep_backoff(failures)
                continue
            else:
                self._finish(connection, response, key)
                break

    def __enter__(self) -> "HTTPRangeReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def fetch_page(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    insecure: bool = False,
    timeout: float = 30.0,
    max_bytes: int = 2 * 1024 * 1024,
) -> tuple[str, str]:
    """Fetch an HTML page (for link resolution). Returns (text, final_url)."""
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url)
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "text/html,application/xhtml+xml,*/*;q=0.8")
    request.add_header("Accept-Encoding", "identity")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPRedirectHandler(),
    )
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read(4096).decode("utf-8", "replace")
        except Exception:
            pass
        raise HTTPStatusError(error.code, error.reason or "", url, detail[:300]) from error
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        raise TrawlNetError(f"Could not reach the server: {reason}") from error
    try:
        raw = response.read(max_bytes + 1)
        final_url = response.geturl()
        content_type = response.headers.get_content_charset() or "utf-8"
        try:
            text = raw[:max_bytes].decode(content_type, "replace")
        except LookupError:
            text = raw[:max_bytes].decode("utf-8", "replace")
        return text, final_url
    finally:
        response.close()
