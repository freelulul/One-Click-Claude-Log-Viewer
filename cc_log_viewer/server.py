"""
ThreadingHTTPServer + URL router for the log viewer.

The handler is intentionally thin: parse path, dispatch to api.py, write the
returned bytes. Static files are served from cc_log_viewer/static/.
"""

from __future__ import annotations

import mimetypes
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import api as api_mod


_STATIC_DIR = Path(__file__).resolve().parent / "static"

_RE_PROJECT_DATES = re.compile(r"^/api/projects/(?P<id>[^/]+)/dates$")
_RE_SESSION_BLOB = re.compile(
    r"^/api/sessions/(?P<rest>.+)/blob/(?P<kind>[^/]+)/(?P<name>[^/]+)$"
)
_RE_SESSION_ENTRIES = re.compile(r"^/api/sessions/(?P<rest>.+)/entries$")
_RE_SESSION_STUBS = re.compile(r"^/api/sessions/(?P<rest>.+)$")


def _split_first_slash(rest: str) -> tuple[str, str]:
    parts = rest.split("/", 1)
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], parts[1])


def make_handler(state: api_mod.AppState) -> type:
    """Build a request-handler class bound to a single AppState."""

    class Handler(BaseHTTPRequestHandler):
        # http.server logs to stderr; we route via log_message below.
        protocol_version = "HTTP/1.1"

        # --- helpers ----------------------------------------------------

        def _write(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, rel: str) -> None:
            if not rel:
                rel = "index.html"
            target = (_STATIC_DIR / rel).resolve()
            try:
                target.relative_to(_STATIC_DIR.resolve())
            except ValueError:
                self._write(404, {"Content-Type": "text/plain"}, b"not found\n")
                return
            if not target.exists() or not target.is_file():
                self._write(404, {"Content-Type": "text/plain"}, b"not found\n")
                return
            mime, _ = mimetypes.guess_type(str(target))
            mime = mime or "application/octet-stream"
            body = target.read_bytes()
            # Default: no-store so dev edits to app.js / style.css / index.html
            # propagate on plain F5 without stale-cache footguns. Vendored
            # libraries (marked, highlight) are long-cached because they don't
            # change between sessions.
            cache_ctl = "no-store"
            if rel.startswith("vendor/"):
                cache_ctl = "public, max-age=86400, immutable"
            self._write(200, {
                "Content-Type": mime + ("; charset=utf-8" if mime.startswith("text/") or mime.endswith("javascript") or mime.endswith("json") else ""),
                "Content-Length": str(len(body)),
                "Cache-Control": cache_ctl,
            }, body)

        def _err(self, status: int, msg: str) -> None:
            body = msg.encode("utf-8") + b"\n"
            self._write(status, {
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(body)),
            }, body)

        # --- dispatch ---------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            try:
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)
            except Exception:
                self._err(400, "bad request")
                return

            # Static / index
            if path == "/" or path == "/index.html":
                self._serve_static("index.html")
                return
            if path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
                return

            try:
                if path == "/api/config":
                    self._send(api_mod.handle_config(state))
                    return
                if path == "/api/projects":
                    self._send(api_mod.handle_projects(state))
                    return
                m = _RE_PROJECT_DATES.match(path)
                if m:
                    self._send(api_mod.handle_project_dates(state, m["id"]))
                    return
                m = _RE_SESSION_BLOB.match(path)
                if m:
                    proj, sess = _split_first_slash(m["rest"])
                    self._send(api_mod.handle_blob(
                        state, proj, sess, m["kind"], m["name"]
                    ))
                    return
                m = _RE_SESSION_ENTRIES.match(path)
                if m:
                    proj, sess = _split_first_slash(m["rest"])
                    indices = api_mod.parse_indices((query.get("indices") or [""])[0])
                    self._send(api_mod.handle_session_entries(state, proj, sess, indices))
                    return
                m = _RE_SESSION_STUBS.match(path)
                if m:
                    proj, sess = _split_first_slash(m["rest"])
                    if not sess:
                        self._err(400, "missing session path")
                        return
                    offset = self._int(query.get("offset"), default=0, lo=0, hi=10**8)
                    limit = self._int(query.get("limit"), default=200, lo=1, hi=500)
                    with_meta = (offset == 0)
                    self._send(api_mod.handle_session_stubs(
                        state, proj, sess, offset, limit, with_meta
                    ))
                    return
            except Exception as e:
                self._err(500, f"internal error: {e!r}")
                return

            self._err(404, f"not found: {path}")

        def _send(self, resp: tuple[int, dict[str, str], bytes]) -> None:
            self._write(*resp)

        @staticmethod
        def _int(v, default: int, lo: int, hi: int) -> int:
            try:
                if not v:
                    return default
                if isinstance(v, list):
                    v = v[0]
                n = int(v)
                return max(lo, min(hi, n))
            except (TypeError, ValueError):
                return default

        # Suppress noisy default access log; emit a compact line.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            print(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}")

    return Handler


def serve(state: api_mod.AppState, host: str, port: int) -> None:
    """Blocking. Press Ctrl+C to stop."""
    handler_cls = make_handler(state)
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    httpd.allow_reuse_address = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        httpd.server_close()
