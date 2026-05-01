"""
REST API handlers.

Each handler returns (status_code, headers_dict, body_bytes). The HTTP server
just dispatches by URL path/method. State (loaded indexes, in-flight indexing
tasks) lives in the AppState singleton.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from . import cache as cache_mod
from . import dates as dates_mod
from . import indexer as indexer_mod


# Maximum number of full session indexes kept in memory. Each is the full
# entries[] list (for a 150k-line session ~ 50MB Python objects). 8 -> ~400MB
# soft cap.
MAX_INMEM_INDEXES = 8


class AppState:
    """
    Server-wide state. Threadsafe. Created once in server.py and passed to
    every handler.
    """
    def __init__(self, tz, projects_root: Path | None = None, port: int = 8088) -> None:
        self.tz = tz
        self.tz_name = dates_mod.tz_name(tz)
        self.projects_root = projects_root or cache_mod.projects_root()
        self.port = port
        self._lock = threading.Lock()
        # (project_id, session_path) -> index dict
        self._indexes: "OrderedDict[tuple[str,str], dict[str, Any]]" = OrderedDict()
        # (project_id, session_path) -> Progress (in-flight only)
        self._progress: dict[tuple[str, str], indexer_mod.Progress] = {}
        # (project_id, session_path) -> threading.Thread
        self._threads: dict[tuple[str, str], threading.Thread] = {}
        # cached project metadata: project_id -> {label, sessions: [...], discovered_at}
        self._project_meta: dict[str, dict[str, Any]] = {}

    # ---- index access ----------------------------------------------------

    def get_index(self, project_id: str, session_path: str) -> dict[str, Any] | None:
        """Return the in-memory index if loaded, else None."""
        with self._lock:
            key = (project_id, session_path)
            idx = self._indexes.get(key)
            if idx is not None:
                # Move-to-end for LRU.
                self._indexes.move_to_end(key)
            return idx

    def get_progress(self, project_id: str, session_path: str) -> indexer_mod.Progress | None:
        with self._lock:
            return self._progress.get((project_id, session_path))

    def ensure_index(
        self, project_id: str, session_path: str, jsonl_path: Path,
    ) -> tuple[dict[str, Any] | None, indexer_mod.Progress | None]:
        """
        Ensure an index exists or is being built for this session.

        Returns (index_or_None, progress_or_None):
          - (index, None)  -> ready, use it
          - (None, prog)   -> still indexing
          - (None, None)   -> file missing
        """
        if not jsonl_path.exists():
            return (None, None)

        key = (project_id, session_path)
        with self._lock:
            idx = self._indexes.get(key)
            stale = False
            if idx is not None:
                # Detect file growth (live CC session appending). On change we
                # KEEP serving the (slightly out-of-date) index and trigger a
                # background update so subsequent F5s see fresh data. This is
                # better than dropping the cache and forcing a 202-loading
                # state for every request during incremental re-index.
                try:
                    st = jsonl_path.stat()
                    cached_size = idx.get("file_size", -1)
                    cached_mtime = idx.get("file_mtime", -1.0)
                    if (st.st_size != cached_size or
                            abs(st.st_mtime - cached_mtime) > 0.001):
                        stale = True
                except OSError:
                    self._indexes.pop(key, None)
                    idx = None

            # Schedule background update for stale or first-time index.
            if (idx is None or stale) and key not in self._progress:
                prog_v = indexer_mod.Progress(jsonl_path.stat().st_size)
                self._progress[key] = prog_v
                t = threading.Thread(
                    target=self._index_worker,
                    args=(project_id, session_path, jsonl_path, key, prog_v),
                    daemon=True, name=f"indexer:{session_path}",
                )
                self._threads[key] = t
                t.start()

            # If we have an index (even stale), return it immediately.
            if idx is not None:
                self._indexes.move_to_end(key)
                return (idx, None)

            # No cached idx at all — return the in-flight progress.
            prog = self._progress.get(key)
            return (None, prog)

    def _index_worker(self, project_id, session_path, jsonl_path, key, prog) -> None:
        try:
            new_idx = indexer_mod.index_session(
                project_id, session_path, jsonl_path, self.tz, prog
            )
            with self._lock:
                self._indexes[key] = new_idx
                self._evict_locked()
                prog.finish()
                self._progress.pop(key, None)
                self._threads.pop(key, None)
        except Exception as e:
            prog.fail(repr(e))
            with self._lock:
                self._threads.pop(key, None)

    def _evict_locked(self) -> None:
        """Drop oldest entries when over the cap. Call with self._lock held."""
        while len(self._indexes) > MAX_INMEM_INDEXES:
            self._indexes.popitem(last=False)

    # ---- project metadata ------------------------------------------------

    def list_projects(self) -> list[dict[str, Any]]:
        """
        Cheap directory listing (no indexing). The frontend uses this for the
        project picker. Filters out projects with zero sessions and sorts by
        most-recently-modified session first so the user lands on the project
        they're actively working in.
        """
        out: list[dict[str, Any]] = []
        for pdir in cache_mod.discover_projects(self.projects_root):
            sessions = cache_mod.discover_sessions(pdir)
            if not sessions:
                # Skip projects with no jsonl sessions — they only clutter
                # the picker (e.g. cache leftovers, deleted logs).
                continue
            # Project mtime = max session jsonl mtime. Cheap stat() loop;
            # number of sessions per project is small (tens at most).
            mtime = 0.0
            for sjsonl in sessions:
                try:
                    st = sjsonl.stat()
                    if st.st_mtime > mtime:
                        mtime = st.st_mtime
                except OSError:
                    continue
            # Project dir names follow CC's convention: leading '-' then path
            # with '/' -> '-'. We can't disambiguate real dashes from path
            # separators, so just strip the leading dash and show the rest as
            # a path-like label without converting.
            display_name = pdir.name.lstrip("-") or pdir.name
            out.append({
                "id": pdir.name,
                "display_name": display_name,
                "session_count": len(sessions),
                "session_paths": [s.stem for s in sessions],
                "mtime": mtime,
            })
        # Newest-modified first.
        out.sort(key=lambda p: p["mtime"], reverse=True)
        return out

    def project_dates(
        self, project_id: str,
    ) -> tuple[dict[str, Any] | None, list[tuple[str, str, str]] | None]:
        """
        Returns (response_or_None, in_flight_sessions).

        in_flight_sessions: list of (session_path, label, jsonl_path_str) currently
        indexing. If not None, callers may show "indexing" placeholders or poll.

        response shape:
          { tz, project_id, indexed: int, in_progress: int, dates: [...] }
        """
        project_dir = self.projects_root / project_id
        if not project_dir.exists():
            return (None, None)

        sessions = cache_mod.discover_sessions(project_dir)
        index_records: list[dict[str, Any]] = []
        in_flight: list[tuple[str, str, str]] = []
        ready_count = 0

        for sjsonl in sessions:
            spath = sjsonl.stem
            idx, prog = self.ensure_index(project_id, spath, sjsonl)
            label = self._session_label(project_id, spath, sjsonl)
            session_dir = sjsonl.parent / spath
            has_subs = cache_mod.has_subagents(session_dir)  # O(1), not enumerate
            if idx is None:
                in_flight.append((spath, label, str(sjsonl)))
                continue
            ready_count += 1
            index_records.append({
                "__session_path": spath,
                "__index": idx,
                "__label": label,
                "__has_subagents": has_subs,
            })
        rolled = indexer_mod.aggregate_project_dates(
            project_id, project_dir, self.tz, index_records,
        )
        return (
            {
                "tz": self.tz_name,
                "project_id": project_id,
                "indexed": ready_count,
                "in_progress": len(in_flight),
                "in_flight_sessions": [
                    {"session_path": s, "label": lbl} for s, lbl, _ in in_flight
                ],
                "dates": rolled["dates"],
                "session_meta": rolled["session_meta"],
            },
            in_flight,
        )

    def _session_label(self, project_id: str, session_path: str, jsonl_path: Path) -> str:
        """Cache labels in self._project_meta[project_id]['labels']."""
        meta = self._project_meta.setdefault(project_id, {"labels": {}})
        labels = meta["labels"]
        if session_path in labels:
            return labels[session_path]
        lbl = indexer_mod.derive_session_label(jsonl_path)
        labels[session_path] = lbl
        return lbl


# ---- handler helpers -----------------------------------------------------

def _json_response(payload: Any, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "Cache-Control": "no-store",
    }
    return (status, headers, body)


def _err(status: int, msg: str) -> tuple[int, dict[str, str], bytes]:
    return _json_response({"error": msg}, status=status)


def _split_session_path(rest: str) -> tuple[str, str]:
    """
    Parse '<projectId>/<sessionPath>...' returning (project_id, session_path).
    session_path may include '/subagents/agent-X' for subagent sessions.

    The trailing component may contain segments like '/entries' or
    '/blob/...' which the caller strips before calling us.
    """
    parts = rest.split("/", 1)
    if len(parts) < 2:
        return (rest, "")
    return (parts[0], parts[1])


def _resolve_session_jsonl(
    state: AppState, project_id: str, session_path: str,
) -> Path | None:
    """
    Given session_path like 'uuid' or 'uuid/subagents/agent-X', return the
    jsonl Path. Returns None if the path escapes the project dir.
    """
    project_dir = state.projects_root / project_id
    if not project_dir.exists() or not project_dir.is_dir():
        return None
    if "/" not in session_path:
        path = project_dir / f"{session_path}.jsonl"
    else:
        # Subagent: <uuid>/subagents/agent-<id>
        path = project_dir / f"{session_path}.jsonl"
    # Path traversal guard
    try:
        path.resolve().relative_to(project_dir.resolve())
    except ValueError:
        return None
    return path if path.exists() else None


# ---- handlers ------------------------------------------------------------

def handle_config(state: AppState) -> tuple[int, dict[str, str], bytes]:
    import socket
    hostname = socket.getfqdn() or socket.gethostname()
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"
    p = state.port
    return _json_response({
        "tz": state.tz_name,
        "hostname": hostname,
        "user": user,
        "port": p,
        "ssh_hint": f"ssh -L {p}:127.0.0.1:{p} {user}@{hostname}",
        "projects_root": str(state.projects_root),
    })


def handle_projects(state: AppState) -> tuple[int, dict[str, str], bytes]:
    return _json_response({"projects": state.list_projects()})


def handle_project_dates(
    state: AppState, project_id: str,
) -> tuple[int, dict[str, str], bytes]:
    project_id = urllib.parse.unquote(project_id)
    response, in_flight = state.project_dates(project_id)
    if response is None:
        return _err(404, f"project {project_id!r} not found")
    status = 200 if not in_flight else 202
    return _json_response(response, status=status)


def handle_session_stubs(
    state: AppState, project_id: str, session_path: str,
    offset: int, limit: int, with_meta: bool,
) -> tuple[int, dict[str, str], bytes]:
    project_id = urllib.parse.unquote(project_id)
    session_path = urllib.parse.unquote(session_path)
    jsonl = _resolve_session_jsonl(state, project_id, session_path)
    if jsonl is None:
        return _err(404, f"session {session_path!r} not found in {project_id!r}")
    idx, prog = state.ensure_index(project_id, session_path, jsonl)
    if idx is None:
        snap = prog.snapshot() if prog else None
        return _json_response({
            "indexing": True,
            "progress": snap,
        }, status=202)

    entries = idx.get("entries", [])
    total = len(entries)
    end = min(offset + limit, total)
    stubs = []
    for i in range(offset, end):
        e = entries[i]
        stub = {
            "idx": i,
            "type": e.get("type"),
            "ts": e.get("ts"),
            "role": e.get("role"),
            "kind": e.get("kind"),
            "tool_name": e.get("tool_name"),
            "preview": e.get("preview"),
            "size": e.get("size"),
            "isCompactSummary": e.get("isCompactSummary"),
            "isSidechain": e.get("isSidechain"),
            "ext_blobs": e.get("ext_blobs"),
            "filter_class": indexer_mod._classify_for_filter(e),
        }
        stubs.append(stub)

    response: dict[str, Any] = {"stubs": stubs, "offset": offset, "end": end}
    if with_meta:
        # First-page payload: include session-level metadata + a compact
        # per-entry filter_classes string so the client can recompute
        # visible_indices instantly when filter chips toggle.
        response["total"] = total
        response["type_breakdown"] = idx.get("type_breakdown", {})
        response["compact_indices"] = idx.get("compact_indices", [])
        response["fork"] = idx.get("fork")
        response["by_date"] = idx.get("by_date", {})
        response["day_map"] = idx.get("day_map", {})
        # 1-char codes per entry; matches FILTER_CODES in app.js.
        codes = {
            "user": "u", "assistant_text": "a", "assistant_tool": "t",
            "tool_result": "r", "assistant_thinking": "T",
            "compact_summary": "c", "command_inject": "C",
            "meta": "m", "other": "o",
        }
        response["filter_classes"] = "".join(
            codes[indexer_mod._classify_for_filter(e)] for e in entries
        )
        # Subagent files (under <session_dir>/subagents/) and tool-results blobs.
        if "/" not in session_path:
            session_dir = jsonl.parent / session_path
            subs = cache_mod.discover_subagents(session_dir)
            response["subagents"] = [
                {"path": f"{session_path}/subagents/{p.stem}",
                 "name": p.stem,
                 "size": p.stat().st_size}
                for p in subs
            ]
            response["tool_result_blobs"] = cache_mod.discover_tool_result_blobs(session_dir)
        else:
            response["subagents"] = []
            response["tool_result_blobs"] = []
    return _json_response(response)


def handle_session_entries(
    state: AppState, project_id: str, session_path: str, indices: list[int],
) -> tuple[int, dict[str, str], bytes]:
    project_id = urllib.parse.unquote(project_id)
    session_path = urllib.parse.unquote(session_path)
    jsonl = _resolve_session_jsonl(state, project_id, session_path)
    if jsonl is None:
        return _err(404, f"session not found")
    idx, prog = state.ensure_index(project_id, session_path, jsonl)
    if idx is None:
        return _json_response({"indexing": True}, status=202)

    entries = idx.get("entries", [])
    total = len(entries)
    out: list[dict[str, Any]] = []
    with jsonl.open("rb") as f:
        for i in indices:
            if i < 0 or i >= total:
                continue
            e = entries[i]
            offset = e["offset"]
            size = e["size"]
            f.seek(offset)
            raw = f.read(size)
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as parse_err:
                parsed = {"_unparseable": True, "_error": str(parse_err)}
            out.append({"idx": i, "entry": parsed})
    return _json_response({"entries": out})


def handle_blob(
    state: AppState, project_id: str, session_path: str, kind: str, name: str,
) -> tuple[int, dict[str, str], bytes]:
    project_id = urllib.parse.unquote(project_id)
    session_path = urllib.parse.unquote(session_path)
    if kind != "tool-results":
        return _err(404, f"unknown blob kind {kind!r}")
    project_dir = state.projects_root / project_id
    if not project_dir.exists():
        return _err(404, "project not found")
    if "/" in session_path:
        # Subagents share parent's tool-results dir.
        parent_session = session_path.split("/", 1)[0]
    else:
        parent_session = session_path
    blob_path = project_dir / parent_session / "tool-results" / name
    try:
        blob_path.resolve().relative_to(project_dir.resolve())
    except ValueError:
        return _err(404, "invalid blob path")
    if not blob_path.exists() or not blob_path.is_file():
        return _err(404, "blob not found")
    body = blob_path.read_bytes()
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Length": str(len(body)),
        "Cache-Control": "no-store",
    }
    return (200, headers, body)


# Convenience: parse query string indices=
def parse_indices(q: str) -> list[int]:
    out: list[int] = []
    for tok in q.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            n = int(tok)
        except ValueError:
            continue
        if 0 <= n < 100_000_000:
            out.append(n)
    return out[:50]
