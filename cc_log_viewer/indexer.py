"""
JSONL line-offset indexer.

For each .jsonl file, walks the file once and produces a header-only index
containing byte offsets, timestamps, types, roles, tool names, and a short
preview per entry. The full entry content is never loaded into memory at
indexing time; serving full content is on-demand via byte offset.

Design:
- Single-pass over the file, iterating by lines (handles >100MB safely with
  Python file iteration; no mmap needed because we always need byte offsets
  *between* lines and tell()/readline cooperate cleanly).
- json.loads each line; recoverable ValueError -> skip the line and continue.
- For lines >1MB, skip preview extraction but still record the offset.
- Append-only incremental update: when extending, seek to last_byte_offset and
  continue. The append must start at a line boundary (which last_byte_offset
  always is, since we only set it after a successful readline).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable

from . import cache as cache_mod
from . import dates as dates_mod


# Per-line size cap above which we still record offset but skip JSON parse for
# preview/header. 1 MB is generous; the largest line we've seen is ~362KB.
HUGE_LINE_BYTES = 1 << 20

# Preview character cap.
PREVIEW_CHARS = 200

# Types that have visible roles by default in the UI.
CONVERSATION_TYPES = frozenset({"user", "assistant"})

# Types we count under "meta" for filter chip stats.
META_TYPES = frozenset({
    "attachment", "system", "permission-mode", "last-prompt",
    "queue-operation", "task_reminder", "custom-title", "auto_mode",
    "auto_mode_exit", "plan_mode", "plan_mode_exit", "deferred_tools_delta",
    "file-history-snapshot", "progress",
})


class Progress:
    """
    Thread-safe progress reporter for the indexer.

    The HTTP handler instantiates one of these per (project_id, session_path)
    pair on first access and the indexer thread updates it line-by-line.
    """
    def __init__(self, total_bytes: int):
        self._lock = threading.Lock()
        self.total_bytes = max(total_bytes, 1)
        self.bytes_done = 0
        self.lines_done = 0
        self.complete = False
        self.error: str | None = None

    def update(self, bytes_done: int, lines_done: int) -> None:
        with self._lock:
            self.bytes_done = bytes_done
            self.lines_done = lines_done

    def finish(self) -> None:
        with self._lock:
            self.bytes_done = self.total_bytes
            self.complete = True

    def fail(self, msg: str) -> None:
        with self._lock:
            self.error = msg
            self.complete = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_bytes": self.total_bytes,
                "bytes_done": self.bytes_done,
                "lines_done": self.lines_done,
                "fraction": self.bytes_done / self.total_bytes,
                "complete": self.complete,
                "error": self.error,
            }


def _coerce_str(v: Any) -> str:
    """Best-effort string coercion for preview text."""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


def _extract_header(parsed: dict[str, Any]) -> dict[str, Any]:
    """
    Extract the header fields we want to cache, given a parsed jsonl line.

    Returns a small dict (no large content). The 'preview' field is at most
    PREVIEW_CHARS chars; the 'tool_name' field is set when this entry contains
    a tool_use block; 'ext_blobs' lists tool-results/<hash>.txt references
    found inline.
    """
    msg_type = parsed.get("type") or ""
    ts = parsed.get("timestamp") or ""
    uuid = parsed.get("uuid") or ""
    parent = parsed.get("parentUuid")
    is_sidechain = bool(parsed.get("isSidechain"))
    is_compact = bool(parsed.get("isCompactSummary"))
    forked_from = parsed.get("forkedFrom")

    role: str | None = None
    kind: str = ""
    tool_name: str | None = None
    preview: str = ""
    ext_blobs: list[str] = []

    msg = parsed.get("message")
    if isinstance(msg, dict):
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            kind = "string"
            preview = content[:PREVIEW_CHARS]
        elif isinstance(content, list):
            kinds_seen: list[str] = []
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                bt = blk.get("type")
                if bt:
                    kinds_seen.append(bt)
                # Capture the first text-ish preview we can find.
                if not preview:
                    if bt == "text":
                        preview = _coerce_str(blk.get("text"))[:PREVIEW_CHARS]
                    elif bt == "thinking":
                        preview = "[thinking] " + _coerce_str(blk.get("thinking"))[:PREVIEW_CHARS - 11]
                    elif bt == "tool_use":
                        tn = blk.get("name") or ""
                        preview = f"[tool_use:{tn}] " + _coerce_str(blk.get("input"))[:PREVIEW_CHARS - 12]
                    elif bt == "tool_result":
                        preview = "[tool_result] " + _coerce_str(blk.get("content"))[:PREVIEW_CHARS - 14]
                # Take the first tool name we see.
                if tool_name is None and bt == "tool_use":
                    tool_name = blk.get("name") or None
                # Collect tool-result blob references from inline content.
                if bt == "tool_result":
                    raw = _coerce_str(blk.get("content"))
                    # Look for "tool-results/<hash>" references.
                    for token in raw.split():
                        if token.startswith("tool-results/"):
                            cleaned = token.rstrip(":,;)")
                            if cleaned not in ext_blobs:
                                ext_blobs.append(cleaned)
            kind = "+".join(sorted(set(kinds_seen))) if kinds_seen else "empty"

    # Normalize whitespace in preview for one-line display.
    preview = " ".join(preview.split())[:PREVIEW_CHARS]

    # Determine if this user-type entry is actually a command/system inject
    # rather than real typing. Examined here so the per-line index records
    # the flag once; the classifier later just reads it (with a preview-based
    # fallback for caches that pre-date this field).
    is_command_like = False
    if msg_type == "user" and "tool_result" not in (kind or ""):
        if parsed.get("isMeta"):
            is_command_like = True
        elif _is_command_like_text(preview):
            is_command_like = True

    return {
        "type": msg_type,
        "ts": ts,
        "uuid": uuid,
        "parent": parent,
        "isSidechain": is_sidechain,
        "isCompactSummary": is_compact,
        "forkedFrom": forked_from,
        "role": role,
        "kind": kind,
        "tool_name": tool_name,
        "preview": preview,
        "ext_blobs": ext_blobs or None,
        "is_command_like": is_command_like,
    }


def _classify_for_filter(header: dict[str, Any]) -> str:
    """One of: 'user', 'assistant_text', 'assistant_thinking', 'assistant_tool',
    'tool_result', 'command_inject', 'meta', 'compact_summary', 'other'.

    type=user entries get triaged into three buckets:
    - tool_result: content is a tool_result block (not a real user msg)
    - command_inject: slash command markers, system reminders, slash-skill
                       bodies, [Request interrupted ...] stubs — i.e. anything
                       NOT typed by the human
    - user: actually-typed human prompt
    """
    t = header.get("type")
    kind = header.get("kind") or ""
    if header.get("isCompactSummary"):
        return "compact_summary"
    if t == "user":
        if "tool_result" in kind:
            return "tool_result"
        # Cached flag (set in _extract_header at index time).
        if header.get("is_command_like"):
            return "command_inject"
        # Fallback for older caches that don't carry the flag: detect from
        # preview directly. Preview is whitespace-normalized but the
        # leading-edge marker is preserved.
        preview = (header.get("preview") or "").lstrip()
        if preview and _is_command_like_text(preview):
            return "command_inject"
        return "user"
    if t == "assistant":
        if "tool_use" in kind:
            return "assistant_tool"
        if "tool_result" in kind:
            return "tool_result"
        if "thinking" in kind and "text" not in kind:
            return "assistant_thinking"
        return "assistant_text"
    if t in META_TYPES:
        return "meta"
    return "other"


def _index_jsonl(
    jsonl_path: Path,
    tz,
    progress: Progress | None = None,
    start_offset: int = 0,
    seed_index: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Scan jsonl_path from start_offset to EOF, return a fresh index dict.

    If seed_index is given (incremental update), the new entries are appended
    onto a copy of seed_index. Caller is responsible for ensuring start_offset
    matches a line boundary (always true if it came from a prior cache).
    """
    entries: list[dict[str, Any]] = list(seed_index) if seed_index else []
    bin_filter: dict[str, int] = {}
    if seed_index:
        # Replay class counts from seeded entries so type_breakdown is right.
        for h in seed_index:
            cls = _classify_for_filter(h)
            bin_filter[cls] = bin_filter.get(cls, 0) + 1

    file_size = jsonl_path.stat().st_size
    bytes_done = start_offset
    lines_done = len(entries)

    PROGRESS_STEP = max(1024 * 64, file_size // 200) if file_size else 1
    next_progress_at = bytes_done + PROGRESS_STEP

    with jsonl_path.open("rb") as f:
        if start_offset:
            f.seek(start_offset)
        while True:
            line_offset = f.tell()
            raw = f.readline()
            if not raw:
                break
            line_size = len(raw)
            entry: dict[str, Any] = {
                "offset": line_offset,
                "size": line_size,
                "type": "", "ts": "", "uuid": "", "parent": None,
                "isSidechain": False, "isCompactSummary": False, "forkedFrom": None,
                "role": None, "kind": "", "tool_name": None, "preview": "",
                "ext_blobs": None,
            }
            if line_size <= HUGE_LINE_BYTES:
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    parsed = None
                if isinstance(parsed, dict):
                    header = _extract_header(parsed)
                    entry.update(header)
            else:
                # Huge line: stamp size only, no preview.
                entry["preview"] = f"[huge line: {line_size:,} bytes]"
            entries.append(entry)
            cls = _classify_for_filter(entry)
            bin_filter[cls] = bin_filter.get(cls, 0) + 1
            lines_done += 1
            bytes_done = line_offset + line_size
            if progress is not None and bytes_done >= next_progress_at:
                progress.update(bytes_done, lines_done)
                next_progress_at = bytes_done + PROGRESS_STEP

    # Build by-date index in the chosen TZ.
    by_date: dict[str, list[int]] = {}
    compact_indices: list[int] = []
    fork: str | None = None
    for i, e in enumerate(entries):
        if e.get("isCompactSummary"):
            compact_indices.append(i)
        d = dates_mod.local_date(e["ts"], tz) if e.get("ts") else None
        if d:
            by_date.setdefault(d, []).append(i)
    if entries and entries[0].get("forkedFrom"):
        fork = entries[0]["forkedFrom"]

    day_map = dates_mod.day_n_of_m(by_date.keys())

    st = jsonl_path.stat()
    if progress is not None:
        progress.update(st.st_size, lines_done)

    return {
        "schema_version": cache_mod.CACHE_SCHEMA_VERSION,
        "file_size": st.st_size,
        "file_mtime": st.st_mtime,
        "last_byte_offset": st.st_size,
        "num_lines": lines_done,
        "entries": entries,
        "by_date": by_date,
        "day_map": {d: list(t) for d, t in day_map.items()},
        "type_breakdown": bin_filter,
        "compact_indices": compact_indices,
        "fork": fork,
    }


def index_session(
    project_id: str,
    session_path: str,
    jsonl_path: Path,
    tz,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """
    Public entry point. Looks up cache; reuses, extends, or rebuilds.

    project_id   = project directory name
    session_path = session uuid OR <uuid>/subagents/agent-<id>
    """
    cpath = cache_mod.cache_path(project_id, session_path)
    cache = cache_mod.load_cache(cpath)
    reusable, can_extend = cache_mod.is_cache_valid(cache, jsonl_path)

    if reusable and cache is not None:
        if progress is not None:
            progress.finish()
        return cache

    if can_extend and cache is not None:
        try:
            seed_idx = cache.get("entries") or []
            start = cache.get("last_byte_offset") or 0
            new_idx = _index_jsonl(
                jsonl_path, tz, progress=progress,
                start_offset=start, seed_index=seed_idx,
            )
            cache_mod.save_cache(cpath, new_idx)
            if progress is not None:
                progress.finish()
            return new_idx
        except Exception as e:  # fall through to full rebuild on any error
            if progress is not None:
                progress.fail(f"incremental update failed: {e}; rebuilding")

    new_idx = _index_jsonl(jsonl_path, tz, progress=progress)
    cache_mod.save_cache(cpath, new_idx)
    if progress is not None:
        progress.finish()
    return new_idx


def aggregate_project_dates(
    project_id: str, project_dir: Path, tz, indexes: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """
    Combine the per-session by_date maps into a project-level rollup.

    Color assignment: each session's hue is computed via _color_index(session_path)
    initially, but we run a greedy pass to ensure no two sessions in the same
    project collide on the same palette slot (until > 24 sessions, at which
    point overflow uses simple modulo).
    """
    rolled: dict[str, list[dict[str, Any]]] = {}
    session_meta: dict[str, dict[str, Any]] = {}
    indexes = list(indexes)

    # Greedy color slot assignment within this project.
    color_assignment: dict[str, int] = {}
    used: set[int] = set()
    palette_size = len(PALETTE_HUES)
    sorted_entries = sorted(indexes, key=lambda e: e["__session_path"])
    for entry in sorted_entries:
        spath = entry["__session_path"]
        slot = _color_index(spath)
        if len(used) >= palette_size:
            color_assignment[spath] = slot
            continue
        # Probe forward with a stride that ensures eventual hit on a free slot.
        for step in range(palette_size):
            cand = (slot + step) % palette_size
            if cand not in used:
                color_assignment[spath] = cand
                used.add(cand)
                break

    for entry in indexes:
        spath = entry["__session_path"]
        idx = entry["__index"]
        label = entry.get("__label") or ""
        has_subs = entry.get("__has_subagents", False)
        color_idx = color_assignment.get(spath, _color_index(spath))
        day_map = idx.get("day_map") or {}
        for d, lines in idx.get("by_date", {}).items():
            n, total = day_map.get(d, (1, 1))
            row = {
                "session_path": spath,
                "color_idx": color_idx,
                "label": label,
                "count": len(lines),
                "first_idx_on_date": lines[0] if lines else 0,
                "last_idx_on_date": lines[-1] if lines else 0,
                "day_n": n,
                "day_total": total,
                "has_subagents": has_subs,
            }
            rolled.setdefault(d, []).append(row)
        session_meta[spath] = {
            "label": label, "color_idx": color_idx,
            "day_total": idx.get("day_map") and len(idx["day_map"]) or 0,
            "active_dates": sorted(idx.get("by_date", {}).keys()),
            "fork": idx.get("fork"),
            "type_breakdown": idx.get("type_breakdown", {}),
            "num_lines": idx.get("num_lines", 0),
            "has_subagents": has_subs,
        }
    sorted_dates = sorted(rolled.keys(), reverse=True)
    return {
        "dates": [
            {
                "date": d,
                "session_count": len(rolled[d]),
                "total_entries": sum(s["count"] for s in rolled[d]),
                "sessions": rolled[d],
            }
            for d in sorted_dates
        ],
        "session_meta": session_meta,
    }


# 24-hue palette indices. A stable, well-separated palette so two distinct
# sessions don't pick visually-confusable hues. Index by hash(sessionId)%24.
PALETTE_HUES = [
    8, 23, 38, 53, 68, 83, 98, 113, 128, 143, 158, 173,
    188, 203, 218, 233, 248, 263, 278, 293, 308, 323, 338, 353,
]


def _color_index(session_path: str) -> int:
    """
    Stable color slot for a session. Uses Python's hash() but bounded to a
    deterministic value across runs by xor'ing bytes. Python sets PYTHONHASHSEED
    randomly by default, so we don't use hash() — use a custom additive hash.
    """
    h = 0
    for ch in session_path.encode("utf-8"):
        h = (h * 131 + ch) & 0xFFFFFFFF
    return h % len(PALETTE_HUES)


# Prefixes that mark a type=user entry as a command/system injection rather
# than a real human-typed prompt. Used both for session-label derivation and
# for the 'C' (command_inject) classifier below.
_NOISE_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>",
    "<local-command-caveat>", "<local-command-stdout>", "<local-command-stderr>",
    "<system-reminder>", "<task-notification>", "<task-output>",
    "Caveat:", "[Request interrupted",
)
_LABEL_NOISE_PREFIXES = _NOISE_PREFIXES  # back-compat alias


def _is_command_like_text(text: str) -> bool:
    """Detect that a user-type entry is a command/system injection, not real
    human typing.

    Catches:
    - <command-name>/foo</command-name> wrappers (slash command invocation)
    - <local-command-stdout>...</local-command-stdout> (slash command output)
    - <system-reminder>, <task-notification>, etc.
    - "# /<name>" markdown heading at line 1 — typical slash-skill body inject
    - "[Request interrupted by user for tool use]" style system stubs
    """
    if not text:
        return False
    s = text.lstrip()
    if not s:
        return False
    if any(s.startswith(p) for p in _NOISE_PREFIXES):
        return True
    # Slash skill body templates: "# /<alnum>" markdown heading.
    if s.startswith("# /") and len(s) > 3 and (s[3].isalnum() or s[3] == "_"):
        return True
    return False


def derive_session_label(jsonl_path: Path, max_chars: int = 60) -> str:
    """
    Read up to the first ~80 lines and return the first real user prompt as a
    human-friendly session label. Skips slash-command markup, system tags, and
    task-notification injected messages.
    """
    if not jsonl_path.exists():
        return ""
    try:
        with jsonl_path.open("rb") as f:
            for _ in range(80):
                raw = f.readline()
                if not raw:
                    break
                if len(raw) > HUGE_LINE_BYTES:
                    continue
                try:
                    d = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if d.get("type") != "user" or d.get("isMeta"):
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                txt = None
                if isinstance(content, str):
                    txt = content
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            txt = blk.get("text")
                            break
                if not txt:
                    continue
                stripped = txt.strip()
                if not stripped:
                    continue
                if any(stripped.startswith(p) for p in _LABEL_NOISE_PREFIXES):
                    continue
                first_line = " ".join(stripped.split())
                return first_line[:max_chars]
    except OSError:
        pass
    return ""
