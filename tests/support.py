from __future__ import annotations

import contextlib
import email.utils
import http.server
import mimetypes
import os
import re
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit


class RangeRequestHandler(http.server.BaseHTTPRequestHandler):
    """A deterministic HTTP/1.1 file server with byte-range support."""

    directory: Path = Path(".")
    ignore_ranges = False
    html_pages: dict[str, str] = {}
    redirects: dict[str, str] = {}
    overrides: dict[str, bytes] = {}
    requests: list = []
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence test output
        pass

    def do_HEAD(self):
        self._serve(head_only=True)

    def do_GET(self):
        self._serve(head_only=False)

    def _serve(self, head_only: bool):
        self.requests.append((self.command, self.path, dict(self.headers)))
        path = unquote(urlsplit(self.path).path)
        if path in self.redirects:
            self.send_response(302)
            self.send_header("Location", self.redirects[path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in self.html_pages:
            body = self.html_pages[path].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return

        relative = path.lstrip("/")
        target = (self.directory / relative).resolve()
        if not str(target).startswith(str(self.directory.resolve())) or not target.is_file():
            self.send_error(404)
            return

        if relative in self.overrides:
            body = self.overrides[relative]
        else:
            body = target.read_bytes()
        size = len(body)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        etag = f'"{size:x}-{int(os.path.getmtime(target)):x}"'
        last_modified = email.utils.formatdate(os.path.getmtime(target), usegmt=True)

        start = 0
        end = size - 1
        partial = False
        range_header = self.headers.get("Range")
        if range_header and not self.ignore_ranges:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            else:
                start = max(0, size - int(match.group(2)))
                end = size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            partial = True

        payload = body[start : end + 1]
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Accept-Ranges", "none" if self.ignore_ranges else "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)


@contextlib.contextmanager
def serve(directory: Path, **options):
    handler = type(
        "TestRangeHandler",
        (RangeRequestHandler,),
        {
            "directory": Path(directory),
            **options,
        },
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
