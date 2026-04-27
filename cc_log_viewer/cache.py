"""
On-disk cache for jsonl line-offset indices.

Each .jsonl file gets one cache file. Cache validity = (file_size, file_mtime).
For append-only growth (the common case during a live Claude Code session),
the indexer does an incremental update starting at last_byte_offset.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

CACHE_SCHEMA_VERSION = 1
CACHE_DIR_NAME = ".cc-viewer-cache"


def projects_root() -> Path:
    """~/.claude/projects/. Override via env CC_LOG_PROJECTS_DIR."""
    override = os.environ.get("CC_LOG_PROJECTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "projects"


def cache_root() -> Path:
    """~/.claude/projects/.cc-viewer-cache/."""
    return projects_root() / CACHE_DIR_NAME


def cache_path(project_id: str, session_path: str) -> Path:
    """
    Cache file for a session jsonl.

    project_id   = directory name under ~/.claude/projects (e.g. "-net-...-simdevice")
    session_path = <session_uuid>  OR  <session_uuid>/subagents/agent-<id>
                    (the path *relative to the project dir*, without .jsonl)

    Subagent caches use '__' separators in the file name to avoid nested dirs.
    """
    safe = session_path.replace("/", "__")
    return cache_root() / project_id / f"{safe}.idx.json"


def date_index_path(project_id: str) -> Path:
    """Per-project rollup index aggregating all sessions' active dates."""
    return cache_root() / project_id / "_dates.idx.json"


def load_cache(path: Path) -> dict[str, Any] | None:
    """Read cache JSON, return None if missing or invalid."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_cache(path: Path, data: dict[str, Any]) -> None:
    """Atomic write: tmp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data["schema_version"] = CACHE_SCHEMA_VERSION
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_cache_valid(cache: dict[str, Any] | None, jsonl_path: Path) -> tuple[bool, bool]:
    """
    Decide whether to reuse, incrementally extend, or rebuild the cache.

    Returns (reusable, can_extend):
      reusable     = True  -> cache is fresh, return as-is
      can_extend   = True  -> cache is stale-but-appendable; resume from last_byte_offset
      both False   = drop cache, rebuild from scratch
    """
    if cache is None or not jsonl_path.exists():
        return False, False

    try:
        st = jsonl_path.stat()
    except OSError:
        return False, False

    cur_size = st.st_size
    cur_mtime = st.st_mtime
    cached_size = cache.get("file_size", -1)
    cached_mtime = cache.get("file_mtime", -1.0)
    last_offset = cache.get("last_byte_offset", -1)

    # Truncated or rotated -> rebuild.
    if cur_size < cached_size:
        return False, False

    # Identical size and mtime -> reuse as-is.
    if cur_size == cached_size and abs(cur_mtime - cached_mtime) < 0.001:
        return True, False

    # Grew, last_offset is sane -> incremental.
    if cur_size > cached_size and 0 <= last_offset <= cached_size:
        return False, True

    # mtime changed but size identical (rare, defensive): rebuild.
    return False, False


def discover_projects(root: Path | None = None) -> list[Path]:
    """
    Return project directories in ~/.claude/projects (excluding the cache dir
    and any files). Skip the on-disk cache directory and other dotted dirs.
    """
    root = root or projects_root()
    if not root.exists():
        return []
    out: list[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name == CACHE_DIR_NAME:
            continue
        out.append(entry)
    return out


def discover_sessions(project_dir: Path) -> list[Path]:
    """
    Return all top-level <session>.jsonl files in a project.

    Subagent jsonls live under <session>/subagents/agent-*.jsonl and are
    discovered separately by the indexer (per-session).
    """
    if not project_dir.exists():
        return []
    out: list[Path] = []
    for entry in sorted(project_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".jsonl":
            stem = entry.stem
            # Validate UUID-ish name (skip oddities), but be permissive.
            if len(stem) >= 8:
                out.append(entry)
    return out


def discover_subagents(session_dir: Path) -> list[Path]:
    """
    Subagent jsonls under <session>/subagents/agent-*.jsonl.
    The session_dir is the directory next to the main jsonl with the same stem.

    Note: enumerating thousands of files on a network mount is slow (we've seen
    AFS take 17s for 500+ files). This function pays that cost; callers that
    just need a yes/no answer should use has_subagents() instead.
    """
    sub = session_dir / "subagents"
    if not sub.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(sub.iterdir()):
        if entry.is_file() and entry.suffix == ".jsonl" and entry.name.startswith("agent-"):
            out.append(entry)
    return out


def has_subagents(session_dir: Path) -> bool:
    """
    Cheap O(1) check (no iterdir): does this session have ANY subagent files?
    Use this on the request fast path; reserve discover_subagents for when the
    caller actually needs the file list.
    """
    sub = session_dir / "subagents"
    if not sub.is_dir():
        return False
    # Try a single iterdir() call but break on the first hit. iterdir is a
    # generator, so this is O(1) on most filesystems.
    try:
        with os.scandir(str(sub)) as it:
            for entry in it:
                if entry.name.startswith("agent-") and entry.name.endswith(".jsonl"):
                    return True
    except OSError:
        return False
    return False


def discover_tool_result_blobs(session_dir: Path) -> list[str]:
    """Filenames inside <session>/tool-results/ (referenced by inline content)."""
    tr = session_dir / "tool-results"
    if not tr.is_dir():
        return []
    return sorted(p.name for p in tr.iterdir() if p.is_file())
