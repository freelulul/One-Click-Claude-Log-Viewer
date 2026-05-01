# cc-log-viewer

A lightweight, dependency-free web UI for browsing Claude Code logs in
`~/.claude/projects/`.

Built for the case where the existing tools choke: 100k+ entries per session,
multi-day single-session projects, slow remote/HPC environments accessed over
SSH. Default-collapsed everything; nothing loads until you ask for it.

## Why

If you've used existing viewers and hit one of these:

- A single session over 100MB freezes the browser tab.
- Date display is unreliable / shows the wrong day.
- Tool calls and `file-history-snapshot` noise drown the actual conversation.
- The viewer dumps generated `.html` files into `~/.claude/projects/`.
- You're SSH'd into a remote box and the viewer wants to bind `0.0.0.0`.

This is a from-scratch rewrite addressing every one of those.

## Features

- **Turn-based timeline.** Each "turn" in the conversation is one card:
  *user prompt* or *assistant text reply*. Tool calls, tool results,
  thinking blocks, slash-command scaffolding and other meta noise between
  turns get folded into a single collapsed bar (`▶ 5 tools · 2 thinking ·
  8 results · 3 /cmd`) that you click to expand inline. A 100k-entry
  session collapses to a few hundred turns.
- **Color-coded prompts.** Real human-typed prompts render as bold
  emerald-green cards; slash command invocations and system-injected
  user messages (e.g. `<command-name>`, `<local-command-stdout>`,
  skill bodies starting with `# /name`, `isMeta:true`) are auto-detected
  and bucketed into a separate `/cmd` class so the green cards are
  unambiguously *what you typed*.
- **Virtual scroll over groups, not entries.** Variable-height layout
  (76 px per primary card, 32 px per collapsed span, 28 px per inner row
  when a span is expanded, 30 px for compaction markers). Only ~80 rows
  live in the DOM at any time. Toggling a span preserves scroll anchor
  so the scrollbar thumb does not jump. The timeline scrollbar is
  16 px wide with a 48 px-minimum thumb so 100k-entry sessions still
  leave a thumb you can click and drag.
- **Default-collapsed detail.** Click a card → a wide drawer slides in
  from the right (`clamp(520px, 60vw, 1100px)`) with the full content.
  Markdown-rendered, dark-themed code highlighting (atom-one-dark),
  single scroll surface (no nested scrollbars). Long content shows
  `Show full (N chars)` on demand. External tool-result blobs
  (`tool-results/<hash>.txt`) load via a button. hljs only highlights
  blocks that markdown tagged with an explicit language; untagged
  prose-in-pre stays neutral (no random navy syntax noise).
- **Date-first navigation + per-day scope.** `Project → Date → Session`.
  Sessions that span multiple days appear under each date with a
  `Day N/M` chip. Clicking a session under date `2026-04-30` *scopes*
  the timeline to ONLY that date's entries by default — a multi-day
  session with 100k+ entries collapses to today's few-hundred-turn
  slice and the scrollbar represents that slice, not the whole history.
  Topbar shows a `📅 2026-04-30 · N` chip with × to clear scope and
  fall back to the full session view. The timeline lands on that
  date's most recent message; `j`/`k` walks back through earlier turns
  on the same date.
- **Project picker auto-curated.** Empty / stub project directories
  (no `.jsonl` sessions) are filtered out. Remaining projects are
  sorted by latest-session-modified-time, descending — what you're
  actively working in always lands on top.
- **Stable session colors.** Each session gets a hue from a 24-color
  palette (deterministic from sessionId, deduplicated within a project)
  so a long-running session is recognizable across every date it
  appears under.
- **Reliable timestamps.** Built around the `timestamp` field in each
  jsonl entry, parsed with `zoneinfo`, rendered with the matching
  `Intl.DateTimeFormat` in the browser. Default is your local timezone;
  pass `--tz <IANA-name>` to override.
- **Compaction & fork markers.** `isCompactSummary:true` becomes a
  horizontal divider in the timeline; `forkedFrom` shows up as a
  "↳ continued from <id>" banner.
- **Subagents nested.** `<session>/subagents/agent-*.jsonl` files
  appear as expandable links inside their parent session.
- **Live refresh that doesn't stall.** When the file grows (a live CC
  session appending), F5 returns the cached index *immediately* and
  triggers an *incremental* re-index in a background thread. The next
  F5 sees the new entries. No 30-second "indexing..." wall.
- **SSH-tunnel-friendly.** Binds to `127.0.0.1` only. The startup
  banner prints the exact `ssh -L` command to copy onto your laptop.
  `--public` is gated behind a second `--i-mean-it` flag.
- **Tool-result classifier fix.** Claude Code wraps tool results as
  `type:"user"` entries with `tool_result` content. Most viewers count
  these as user prompts and inflate the count 10–20×. We detect and
  reclassify them as results, so the `user` filter chip shows your
  real prompts only.

## Requirements

- Python ≥ 3.9 (`zoneinfo` is part of the stdlib from 3.9).
- A modern browser. Tested on Chrome and Firefox.

There is **no `pip install` step**. Vendored frontend deps (marked.js,
highlight.js) are committed under `cc_log_viewer/static/vendor/`.

## Usage

From the repo root:

```sh
./claude_log_viewer.sh
```

That picks the newest Python it can find on the system that has `zoneinfo`,
then runs `python3 -m cc_log_viewer`. Be explicit if you want:

```sh
python3 -m cc_log_viewer --port 8088 --tz <IANA-timezone>
```

The startup banner prints (with your actual host / user / timezone in
place of the angle-bracket placeholders):

```
Claude Code Log Viewer
─────────────────────────────────────────────────────
Server   : http://127.0.0.1:8088
Hostname : <host>
Timezone : <timezone>
Logs root: ~/.claude/projects

[SSH tunnel] On your laptop, run:
  ssh -L 8088:127.0.0.1:8088 <user>@<host>
Then open http://127.0.0.1:8088/ in your local browser.
```

Run that command from your laptop. Open the local URL. Done.

### Common flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--port N` | 8088 | TCP port. Auto-iterates +1 if busy. |
| `--tz NAME` | system local | IANA name (`America/Chicago`), `UTC`, or fixed offset (`+08:00`). |
| `--public` | (off) | Bind to `0.0.0.0`. **Requires `--i-mean-it`** to actually take effect. |
| `--projects-root DIR` | `~/.claude/projects` | Override the logs directory. |
| `--selftest` | — | Run a synthetic 1MB index pass and exit 0. |

## UI

```
┌──── topbar ──────────────────────────────────────────────────┐
│ ☰ cc-log-viewer ●session 12k total 📅 2026-04-30 · 234 [×]   │
│ [u] [a] [t] [r] [T] [/cmd] [c] [m] [o]   ↻ tz: <tz>  ?       │
├──────────────────────────────────────────────────────────────┤
│  ●●  USER · 09:11:37        ← emerald, your real prompt      │
│       I want you to rewrite the viewer because…              │
│       …                                                      │
│                                                              │
│   ▶ 5 tool · 2 thinking · 8 result · 1 /cmd  09:12:01-12:30  │
│                                                              │
│  ●●  ASSISTANT · 09:12:30                                    │
│       Let me first explore the codebase…                     │
│                                                              │
│   ▼ 8 tool · 1 thinking · 5 result          09:12:35-14:50   │
│   │  ▶  Bash    ldd --version                       09:12:35 │
│   │  ←  Result  GLIBC 2.31-...                      09:12:35 │
│   │  θ  thinking · The user's project structure shows…       │
│   │  ▶  Edit    indexer.py                          09:12:42 │
│   │  …                                                       │
│                                                              │
│  ─── compaction · #245 ─────────────────────────────         │
│                                                              │
│  ●●  USER · 09:15:30                                         │
│       Looks great but I'd prefer if…                         │
└──────────────────────────────────────────────────────────────┘
```

- **Click a primary card** → detail drawer slides in from the right with
  the full content (markdown rendered, code highlighted).
- **Click a span bar** (▶) → expands inline, showing each tool / result
  / thinking / cmd entry as a small row. Click any row → opens its
  detail too. Toggling a span preserves your scroll anchor.
- **Click ☰** → opens the project + date + session picker as a left
  drawer. Empty projects are filtered, remaining are sorted newest
  modified first.
- **`📅 YYYY-MM-DD · N` chip** in the topbar shows the active date
  scope and the entry count on that date. Click `×` to clear scope
  and see the full session.
- **Filter chips** in the topbar toggle visibility of each entry class:
  `u` user, `a` assistant text, `t` tools, `r` tool results, `T`
  thinking, `/cmd` slash command + system injects, `c` compactions,
  `m` meta (file-history-snapshot, attachment, etc. — off by default),
  `o` other.

### Keyboard shortcuts

| Key | Action |
| --- | --- |
| `Esc` | Close drawer / detail / help popover |
| `j` / `k` | Step to next / previous primary card (auto-scrolls + opens detail) |
| `Ctrl+P` | Open the picker drawer |
| `F5` | Reload — picks up newly appended log lines via lazy incremental index |

## How it works

```
~/.claude/projects/
  -<project-id>/
    <session-uuid>.jsonl                      # main session log
    <session-uuid>/
      subagents/agent-*.jsonl                 # nested subagent sessions
      tool-results/<hash>.txt                 # external tool blobs
  .cc-viewer-cache/                           # offset indexes (created by us)
    -<project-id>/<session-uuid>.idx.json     # mtime+size validated, append-only
```

On first open of a session the indexer streams through the jsonl once,
recording per-line byte offset, type, role, timestamp, tool name, and a
200-char preview. Subsequent accesses are nearly instant because we only
read the small index file. When the session jsonl grows (live Claude
Code session appends new lines), the indexer continues from
`last_byte_offset` instead of re-scanning the whole file. The in-memory
index keeps serving the slightly-stale data while the incremental
update runs in a daemon thread; the next F5 sees fresh data.

The frontend gets the per-entry filter classes as a single ASCII string
on the first request (`uatamCatm…`, ~150 KB for 150k entries). It uses
that to compute *groups* client-side without any per-entry round-trip:
runs of `u` and `a` become primary cards, runs of other classes become
collapsible spans, `c` becomes a divider. Stub data (timestamp, preview)
for individual entries is fetched lazily as you scroll into them, in
batches of 200.

## Architecture

```
cc_log_viewer/
  __main__.py        # CLI args, banner, port iteration
  server.py          # ThreadingHTTPServer + URL dispatch
  api.py             # endpoint handlers; AppState + threadsafe LRU
  indexer.py         # jsonl line-offset scan, header extraction, classifier
  cache.py           # cache file paths, mtime+size invalidation
  dates.py           # zoneinfo + Day-N-of-M math
  static/
    index.html       # topbar + main + drawers
    app.js           # vanilla JS, virtual scroll, group computation, markdown
    style.css
    vendor/          # marked.js, highlight.js, theme
```

Backend is stdlib-only Python: no `pip install`, no venv, no internet
needed at runtime. Frontend is a single-page app with no build step.

## REST API (for scripting)

```
GET  /api/config
GET  /api/projects
GET  /api/projects/<id>/dates
GET  /api/sessions/<projectId>/<sessionPath>?offset=0&limit=200
GET  /api/sessions/<projectId>/<sessionPath>/entries?indices=12,15,16
GET  /api/sessions/<projectId>/<sessionPath>/blob/tool-results/<hash>
```

`sessionPath` is a session UUID, or `<uuid>/subagents/agent-<id>` for a
subagent. The first stub request returns metadata + a compact
`filter_classes` string covering the full session; subsequent requests
return only stubs for the requested offset window.

## License

MIT.
