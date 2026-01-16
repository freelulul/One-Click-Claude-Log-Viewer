#!/usr/bin/env python3
"""
Enhanced Claude Log Viewer Server

Features:
1. Auto-regenerates HTML when source log files change
2. Provides session selector UI - view individual sessions
3. Live reload support
"""

import os
import sys
import json
import time
import subprocess
import threading
import http.server
import socketserver
from pathlib import Path
from datetime import datetime
import re
import html

# Configuration
PROJECT_DIR = Path.home() / ".claude" / "projects"
PORT = 8088  # Changed from 8080 which is often in use
WATCH_INTERVAL = 5  # seconds

# Global state for live reload
regeneration_in_progress = False
regeneration_lock = threading.Lock()


class SessionInfo:
    """Store session metadata"""
    def __init__(self, session_id, title, timestamp_start, timestamp_end, messages, tokens, preview, file_path, jsonl_size=0, html_size=0, project_folder=None):
        self.session_id = session_id
        self.title = title
        self.timestamp_start = timestamp_start
        self.timestamp_end = timestamp_end
        self.messages = messages
        self.tokens = tokens
        self.preview = preview
        self.file_path = file_path
        self.jsonl_size = jsonl_size  # JSONL file size in bytes
        self.html_size = html_size    # HTML file size in bytes
        self.project_folder = project_folder  # Project folder name for deletion


def format_size(size_bytes):
    """Format file size in human-readable format"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def get_source_files_mtime(project_dir: Path) -> float:
    """Get the latest modification time of source log files (jsonl)"""
    latest_mtime = 0
    for jsonl_file in project_dir.rglob("*.jsonl"):
        mtime = jsonl_file.stat().st_mtime
        if mtime > latest_mtime:
            latest_mtime = mtime
    return latest_mtime


def get_html_files_mtime(project_dir: Path) -> float:
    """Get the latest modification time of generated HTML files"""
    latest_mtime = 0
    for html_file in project_dir.rglob("*.html"):
        mtime = html_file.stat().st_mtime
        if mtime > latest_mtime:
            latest_mtime = mtime
    return latest_mtime


def find_outdated_sessions():
    """Find jsonl files that are newer than their corresponding HTML files"""
    outdated = []
    for project_dir in PROJECT_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            # Skip agent files
            if jsonl_file.name.startswith("agent-"):
                continue
            # Find corresponding HTML file
            session_id = jsonl_file.stem
            html_file = project_dir / f"session-{session_id}.html"
            combined_html = project_dir / "combined_transcripts.html"

            jsonl_mtime = jsonl_file.stat().st_mtime

            # Check if HTML doesn't exist or is older than jsonl
            if not html_file.exists():
                outdated.append(jsonl_file)
            elif html_file.stat().st_mtime < jsonl_mtime:
                outdated.append(jsonl_file)
            # Also check combined_transcripts.html
            elif combined_html.exists() and combined_html.stat().st_mtime < jsonl_mtime:
                if jsonl_file not in outdated:
                    outdated.append(jsonl_file)
    return outdated


def regenerate_logs(force_clear=False):
    """Run claude-code-log to regenerate HTML files (incremental)"""
    global regeneration_in_progress

    # Prevent multiple simultaneous regenerations
    with regeneration_lock:
        if regeneration_in_progress:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Regeneration already in progress, skipping...")
            return
        regeneration_in_progress = True

    try:
        # Find outdated sessions
        outdated = find_outdated_sessions()

        if not outdated and not force_clear:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] All sessions up-to-date, skipping regeneration")
            return

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Found {len(outdated)} outdated session(s), regenerating...")

        # Check if any combined_transcripts.html files are missing (claude-code-log cache bug workaround)
        missing_combined = False
        projects_to_update = set()
        for jsonl_file in outdated:
            projects_to_update.add(jsonl_file.parent)

        for project_dir in PROJECT_DIR.iterdir():
            if project_dir.is_dir():
                combined = project_dir / "combined_transcripts.html"
                has_jsonl = any(project_dir.glob("*.jsonl"))
                if has_jsonl and not combined.exists():
                    missing_combined = True
                    print(f"  Missing: {project_dir.name}/combined_transcripts.html")

        # Group by project directory
        for jsonl_file in outdated:
            # Delete the outdated HTML file to force regeneration
            session_id = jsonl_file.stem
            html_file = jsonl_file.parent / f"session-{session_id}.html"
            if html_file.exists():
                try:
                    html_file.unlink()
                    print(f"  Deleted: {html_file.name}")
                except Exception:
                    pass

        # Delete combined_transcripts.html for affected projects
        for project_dir in projects_to_update:
            combined = project_dir / "combined_transcripts.html"
            if combined.exists():
                try:
                    combined.unlink()
                    print(f"  Deleted: {project_dir.name}/combined_transcripts.html")
                except Exception:
                    pass

        # If combined_transcripts.html is missing, use --clear-html to workaround claude-code-log cache bug
        cmd = ["uvx", "claude-code-log@latest"]
        if missing_combined or force_clear:
            cmd.append("--clear-html")
            print(f"  Using --clear-html to fix missing combined_transcripts.html")

        # Regenerate (claude-code-log will only generate missing files)
        result = subprocess.run(
            cmd,
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Regeneration complete ({len(outdated)} session(s) updated)")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Warning: claude-code-log returned non-zero")
            if result.stderr:
                print(f"  stderr: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Warning: claude-code-log timed out")
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error regenerating: {e}")
    finally:
        with regeneration_lock:
            regeneration_in_progress = False


def parse_sessions_from_combined(html_path: Path) -> list[SessionInfo]:
    """Parse session info from combined_transcripts.html"""
    sessions = []
    if not html_path.exists():
        return sessions

    content = html_path.read_text(encoding='utf-8')

    # Find session links using regex
    pattern = r"<a href='#msg-session-([^']+)'[^>]*class='session-link'>(.*?)</a>"
    matches = re.findall(pattern, content, re.DOTALL)

    for session_id, link_content in matches:
        # Extract title
        title_match = re.search(r"<div class='session-link-title'>\s*(.*?)\s*</div>", link_content, re.DOTALL)
        title = title_match.group(1).strip() if title_match else session_id[:8]
        title = re.sub(r'<[^>]+>', '', title).strip()  # Remove HTML tags
        title = re.sub(r'\s+', ' ', title)  # Normalize whitespace

        # Extract timestamps
        ts_match = re.search(r"data-timestamp=\"([^\"]+)\".*?data-timestamp-end=\"([^\"]+)\"", link_content)
        ts_start = ts_match.group(1) if ts_match else ""
        ts_end = ts_match.group(2) if ts_match else ""

        # Extract message count
        msg_match = re.search(r"(\d+)\s*messages", link_content)
        messages = int(msg_match.group(1)) if msg_match else 0

        # Extract token usage
        token_match = re.search(r"Token usage[^<]+", link_content)
        tokens = token_match.group(0) if token_match else ""

        # Extract preview
        preview_match = re.search(r"<pre class='session-preview'>(.*?)</pre>", link_content, re.DOTALL)
        preview = html.unescape(preview_match.group(1)[:200]) if preview_match else ""

        # Find individual session file and get sizes
        parent_dir = html_path.parent
        session_file = parent_dir / f"session-{session_id}.html"
        jsonl_file = parent_dir / f"{session_id}.jsonl"

        # Get file sizes
        jsonl_size = jsonl_file.stat().st_size if jsonl_file.exists() else 0
        html_size = session_file.stat().st_size if session_file.exists() else 0

        sessions.append(SessionInfo(
            session_id=session_id,
            title=title,
            timestamp_start=ts_start,
            timestamp_end=ts_end,
            messages=messages,
            tokens=tokens,
            preview=preview,
            file_path=str(session_file.relative_to(PROJECT_DIR)) if session_file.exists() else None,
            jsonl_size=jsonl_size,
            html_size=html_size,
            project_folder=parent_dir.name
        ))

    return sessions


def get_all_projects() -> dict:
    """Get all projects and their sessions"""
    projects = {}

    for item in PROJECT_DIR.iterdir():
        if item.is_dir():
            combined = item / "combined_transcripts.html"
            if combined.exists():
                project_name = item.name.replace('-', '/').lstrip('/')
                sessions = parse_sessions_from_combined(combined)
                if sessions:
                    projects[item.name] = {
                        'display_name': project_name,
                        'sessions': sessions,
                        'combined_path': str(combined.relative_to(PROJECT_DIR))
                    }

    return projects


def generate_session_selector_html() -> str:
    """Generate a custom session selector page"""
    projects = get_all_projects()

    html_content = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Claude Log Viewer - Session Selector</title>
    <style>
        :root {
            --primary: #6366f1;
            --primary-light: #818cf8;
            --bg: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --card-bg: rgba(255, 255, 255, 0.95);
            --text: #1f2937;
            --text-muted: #6b7280;
            --border: #e5e7eb;
            --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            min-height: 100vh;
            padding: 20px;
            color: var(--text);
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        header {
            text-align: center;
            padding: 30px 0;
            color: white;
        }

        header h1 {
            font-size: 2.5em;
            margin-bottom: 10px;
            text-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }

        header p {
            opacity: 0.9;
            font-size: 1.1em;
        }

        .status-bar {
            background: rgba(255,255,255,0.2);
            border-radius: 8px;
            padding: 10px 20px;
            margin: 20px 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
            color: white;
        }

        .status-bar button {
            background: white;
            color: var(--primary);
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            transition: transform 0.2s;
        }

        .status-bar button:hover {
            transform: scale(1.05);
        }

        .project-card {
            background: var(--card-bg);
            border-radius: 16px;
            margin-bottom: 24px;
            box-shadow: var(--shadow);
            overflow: hidden;
        }

        .project-header {
            background: linear-gradient(90deg, #f3d6d2, #f1dcce, #f0e4ca);
            padding: 16px 24px;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: background 0.2s;
        }

        .project-header:hover {
            background: linear-gradient(90deg, #ecc8c4, #e9cfc0, #e8d8bc);
        }

        .project-header h2 {
            font-size: 1.2em;
            color: #2c3e50;
        }

        .project-header .badge {
            background: var(--primary);
            color: white;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85em;
        }

        .project-header .actions {
            display: flex;
            gap: 8px;
        }

        .project-header .actions a {
            background: #4caf50;
            color: white;
            padding: 6px 12px;
            border-radius: 6px;
            text-decoration: none;
            font-size: 0.85em;
            transition: background 0.2s;
        }

        .project-header .actions a:hover {
            background: #43a047;
        }

        .sessions-list {
            padding: 16px;
            display: none;
        }

        .sessions-list.expanded {
            display: block;
        }

        .session-item {
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 12px;
            transition: all 0.2s;
            cursor: pointer;
        }

        .session-item:hover {
            border-color: var(--primary-light);
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
        }

        .session-item.selected {
            border-color: var(--primary);
            background: rgba(99, 102, 241, 0.05);
        }

        .session-title {
            font-weight: 600;
            font-size: 1.1em;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
        }

        .session-title .id {
            color: var(--text-muted);
            font-weight: normal;
            font-size: 0.85em;
        }

        .session-meta {
            font-size: 0.9em;
            color: var(--text-muted);
            margin-bottom: 8px;
        }

        .newest-badge {
            background: #10b981;
            color: white;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.7em;
            font-weight: bold;
            margin-left: 8px;
            vertical-align: middle;
        }

        .session-tokens {
            font-size: 0.8em;
            color: var(--text-muted);
            background: #f3f4f6;
            padding: 4px 8px;
            border-radius: 4px;
            display: inline-block;
            margin-bottom: 8px;
        }

        .session-preview {
            font-size: 0.85em;
            color: var(--text-muted);
            background: #f9fafb;
            padding: 8px;
            border-radius: 6px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            font-family: monospace;
        }

        .session-actions {
            margin-top: 12px;
            display: flex;
            gap: 8px;
        }

        .session-actions a {
            padding: 6px 14px;
            border-radius: 6px;
            text-decoration: none;
            font-size: 0.9em;
            font-weight: 500;
            transition: all 0.2s;
        }

        .btn-view {
            background: var(--primary);
            color: white;
        }

        .btn-view:hover {
            background: var(--primary-light);
        }

        .btn-new-tab {
            background: #f3f4f6;
            color: var(--text);
        }

        .btn-new-tab:hover {
            background: #e5e7eb;
        }

        .btn-delete {
            background: #fee2e2;
            color: #dc2626;
            border: 1px solid #fecaca;
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 0.9em;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-delete:hover {
            background: #fecaca;
            border-color: #f87171;
        }

        .session-size {
            display: flex;
            gap: 8px;
            margin-bottom: 8px;
            flex-wrap: wrap;
        }

        .size-badge {
            font-size: 0.75em;
            padding: 2px 8px;
            border-radius: 4px;
            background: #e0e7ff;
            color: #4338ca;
        }

        .size-badge.total {
            background: #fef3c7;
            color: #b45309;
            font-weight: 600;
        }

        .project-size-info {
            background: #f0f9ff;
            padding: 10px 16px;
            border-bottom: 1px solid var(--border);
            display: flex;
            gap: 16px;
            font-size: 0.85em;
            color: #0369a1;
        }

        .project-size-info span {
            background: white;
            padding: 4px 10px;
            border-radius: 4px;
            border: 1px solid #bae6fd;
        }

        .confirm-dialog {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.5);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }

        .confirm-dialog-content {
            background: white;
            padding: 24px;
            border-radius: 12px;
            max-width: 400px;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1);
        }

        .confirm-dialog h3 {
            margin-bottom: 12px;
            color: #dc2626;
        }

        .confirm-dialog p {
            margin-bottom: 20px;
            color: var(--text-muted);
        }

        .confirm-dialog-buttons {
            display: flex;
            gap: 12px;
            justify-content: flex-end;
        }

        .confirm-dialog button {
            padding: 8px 16px;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            font-weight: 500;
        }

        .confirm-dialog .btn-cancel {
            background: #f3f4f6;
            color: var(--text);
        }

        .confirm-dialog .btn-confirm-delete {
            background: #dc2626;
            color: white;
        }

        .expand-icon {
            transition: transform 0.3s;
        }

        .expanded .expand-icon {
            transform: rotate(180deg);
        }

        .no-sessions {
            text-align: center;
            padding: 40px;
            color: var(--text-muted);
        }

        .refresh-notice {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: #10b981;
            color: white;
            padding: 12px 20px;
            border-radius: 8px;
            box-shadow: var(--shadow);
            display: none;
            animation: slideIn 0.3s ease;
        }

        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }

        .loading {
            display: inline-block;
            width: 16px;
            height: 16px;
            border: 2px solid rgba(255,255,255,0.3);
            border-radius: 50%;
            border-top-color: white;
            animation: spin 1s linear infinite;
            margin-right: 8px;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Claude Log Viewer</h1>
            <p>Select a project and session to view</p>
        </header>

        <div class="status-bar">
            <span id="lastUpdate">Last updated: ''' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '''</span>
            <button onclick="refreshLogs()">
                <span id="refreshIcon"></span>
                Refresh Logs
            </button>
        </div>
'''

    if not projects:
        html_content += '''
        <div class="project-card">
            <div class="no-sessions">
                <p><span class="loading"></span> Loading sessions... (regenerating in background)</p>
                <p style="margin-top: 10px; font-size: 0.9em;">If this takes too long, click "Refresh Logs" button above.</p>
            </div>
        </div>
        <script>
            // Auto-refresh when no projects (likely still regenerating)
            setTimeout(() => location.reload(), 5000);
        </script>
'''
    else:
        for folder_name, project in sorted(projects.items(), key=lambda x: x[1]['display_name']):
            sessions = project['sessions']
            combined_path = project['combined_path']
            display_name = project['display_name']

            html_content += f'''
        <div class="project-card">
            <div class="project-header" onclick="toggleProject(this)">
                <div>
                    <h2>{html.escape(display_name)}</h2>
                </div>
                <div class="actions">
                    <span class="badge">{len(sessions)} session{'s' if len(sessions) != 1 else ''}</span>
                    <a href="{html.escape(combined_path)}" target="_blank" onclick="event.stopPropagation()">View All</a>
                    <span class="expand-icon">▼</span>
                </div>
            </div>
            <div class="sessions-list">
'''
            # Sort sessions by end timestamp (newest first)
            sessions_sorted = sorted(sessions, key=lambda s: s.timestamp_end or '', reverse=True)
            # Calculate total size for project
            total_jsonl = sum(s.jsonl_size for s in sessions_sorted)
            total_html = sum(s.html_size for s in sessions_sorted)
            project_total_size = format_size(total_jsonl + total_html)

            html_content += f'''
                <div class="project-size-info">
                    <span>Total: {project_total_size}</span>
                    <span>Log: {format_size(total_jsonl)}</span>
                    <span>HTML: {format_size(total_html)}</span>
                </div>
'''
            for idx, session in enumerate(sessions_sorted):
                session_file = session.file_path or combined_path
                session_display = session.title if session.title else session.session_id[:8]
                # Format end timestamp for display
                last_update = ""
                if session.timestamp_end:
                    try:
                        from datetime import datetime as dt
                        ts = dt.fromisoformat(session.timestamp_end.replace('Z', '+00:00'))
                        last_update = ts.strftime('%Y-%m-%d %H:%M')
                    except:
                        last_update = session.timestamp_end[:16] if len(session.timestamp_end) > 16 else session.timestamp_end
                # Mark newest session
                newest_badge = '<span class="newest-badge">LATEST</span>' if idx == 0 else ''
                # Format file sizes
                jsonl_size_str = format_size(session.jsonl_size)
                html_size_str = format_size(session.html_size)
                total_size_str = format_size(session.jsonl_size + session.html_size)

                html_content += f'''
                <div class="session-item" data-session="{html.escape(session.session_id)}" data-project="{html.escape(folder_name)}">
                    <div class="session-title">
                        <span>{html.escape(session_display)} {newest_badge}</span>
                        <span class="id">{session.session_id[:8]}</span>
                    </div>
                    <div class="session-meta">
                        {session.messages} messages | <strong>Last: {last_update}</strong>
                    </div>
                    <div class="session-size">
                        <span class="size-badge">Log: {jsonl_size_str}</span>
                        <span class="size-badge">HTML: {html_size_str}</span>
                        <span class="size-badge total">Total: {total_size_str}</span>
                    </div>
                    <div class="session-tokens">{html.escape(session.tokens)}</div>
                    <div class="session-preview">{html.escape(session.preview[:150])}</div>
                    <div class="session-actions">
                        <a href="{html.escape(session_file)}" class="btn-view">View Session</a>
                        <a href="{html.escape(session_file)}" target="_blank" class="btn-new-tab">Open in New Tab</a>
                        <button class="btn-delete" onclick="deleteSession('{html.escape(folder_name)}', '{html.escape(session.session_id)}', event)">Delete</button>
                    </div>
                </div>
'''
            html_content += '''
            </div>
        </div>
'''

    html_content += '''
        <div class="refresh-notice" id="refreshNotice">
            Logs updated! Refreshing...
        </div>
    </div>

    <script>
        function toggleProject(header) {
            const sessionsList = header.nextElementSibling;
            sessionsList.classList.toggle('expanded');
            header.classList.toggle('expanded');
        }

        function refreshLogs() {
            const btn = event.target.closest('button');
            const icon = document.getElementById('refreshIcon');
            icon.innerHTML = '<span class="loading"></span>';
            btn.disabled = true;

            fetch('/api/refresh', { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    if (data.status === 'ok') {
                        document.getElementById('refreshNotice').style.display = 'block';
                        setTimeout(() => location.reload(), 1000);
                    } else {
                        alert('Refresh failed: ' + data.message);
                        icon.innerHTML = '';
                        btn.disabled = false;
                    }
                })
                .catch(err => {
                    alert('Error: ' + err);
                    icon.innerHTML = '';
                    btn.disabled = false;
                });
        }

        // Live reload: check for version changes
        let currentVersion = null;
        let pendingReload = false;
        const notice = document.getElementById('refreshNotice');
        const statusSpan = document.getElementById('lastUpdate');

        function checkVersion() {
            fetch('/api/version')
                .then(r => r.json())
                .then(data => {
                    // Show regenerating status
                    if (data.regenerating) {
                        statusSpan.innerHTML = '<span class="loading"></span> Regenerating logs...';
                        pendingReload = true;
                        return;
                    } else {
                        statusSpan.textContent = 'Last updated: ' + new Date().toLocaleString();
                    }

                    if (currentVersion === null) {
                        currentVersion = data.version;
                        console.log('Initial version:', currentVersion);
                    } else if ((data.version !== currentVersion && data.version > 0) || pendingReload) {
                        console.log('Version changed, reloading in 2s...');
                        pendingReload = false;
                        notice.style.display = 'block';
                        notice.textContent = 'Logs updated! Reloading...';
                        // Wait for files to be fully written
                        setTimeout(() => location.reload(), 2000);
                    }
                })
                .catch(err => console.log('Version check failed:', err));
        }

        // Check version every 3 seconds for live reload
        setInterval(checkVersion, 3000);
        checkVersion(); // Initial check

        // Expand first project by default
        document.addEventListener('DOMContentLoaded', () => {
            const firstProject = document.querySelector('.project-header');
            if (firstProject) {
                toggleProject(firstProject);
            }
        });

        // Delete session functionality
        function deleteSession(projectFolder, sessionId, event) {
            event.stopPropagation();

            // Create confirm dialog
            const dialog = document.createElement('div');
            dialog.className = 'confirm-dialog';
            dialog.innerHTML = `
                <div class="confirm-dialog-content">
                    <h3>Delete Session?</h3>
                    <p>This will permanently delete the session log file and HTML file. This action cannot be undone.</p>
                    <p style="font-size: 0.85em; color: #6b7280;">Session ID: ${sessionId.substring(0, 8)}...</p>
                    <div class="confirm-dialog-buttons">
                        <button class="btn-cancel" onclick="this.closest('.confirm-dialog').remove()">Cancel</button>
                        <button class="btn-confirm-delete" onclick="confirmDelete('${projectFolder}', '${sessionId}', this)">Delete</button>
                    </div>
                </div>
            `;
            document.body.appendChild(dialog);

            // Close on background click
            dialog.addEventListener('click', (e) => {
                if (e.target === dialog) dialog.remove();
            });
        }

        function confirmDelete(projectFolder, sessionId, btn) {
            btn.disabled = true;
            btn.textContent = 'Deleting...';

            fetch('/api/delete-session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project: projectFolder, session_id: sessionId })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'ok') {
                    // Remove the session card from UI
                    const sessionCard = document.querySelector(`[data-session="${sessionId}"]`);
                    if (sessionCard) {
                        sessionCard.style.animation = 'fadeOut 0.3s ease';
                        setTimeout(() => {
                            sessionCard.remove();
                            // Update session count badge
                            const projectCard = document.querySelector(`[data-project="${projectFolder}"]`)?.closest('.project-card');
                            if (projectCard) {
                                const badge = projectCard.querySelector('.badge');
                                const remaining = projectCard.querySelectorAll('.session-item').length;
                                badge.textContent = remaining + ' session' + (remaining !== 1 ? 's' : '');
                            }
                        }, 300);
                    }
                    document.querySelector('.confirm-dialog')?.remove();
                } else {
                    alert('Delete failed: ' + data.message);
                    btn.disabled = false;
                    btn.textContent = 'Delete';
                }
            })
            .catch(err => {
                alert('Error: ' + err);
                btn.disabled = false;
                btn.textContent = 'Delete';
            });
        }
    </script>
    <style>
        @keyframes fadeOut {
            from { opacity: 1; transform: translateX(0); }
            to { opacity: 0; transform: translateX(-20px); }
        }
    </style>
</body>
</html>
'''
    return html_content


class LogViewerHandler(http.server.SimpleHTTPRequestHandler):
    """Custom HTTP handler with API endpoints"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

    def end_headers(self):
        """Override to add no-cache headers for HTML files"""
        # Add no-cache headers for all responses
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def do_GET(self):
        try:
            if self.path == '/' or self.path == '/index.html':
                # Serve custom session selector
                content = generate_session_selector_html()
                content_bytes = content.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', len(content_bytes))
                self.end_headers()
                self.wfile.write(content_bytes)
            elif self.path == '/api/check-update':
                # Check if logs need update
                source_mtime = get_source_files_mtime(PROJECT_DIR)
                html_mtime = get_html_files_mtime(PROJECT_DIR)
                needs_update = source_mtime > html_mtime

                response = json.dumps({'needsUpdate': needs_update})
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(response.encode())
            elif self.path == '/api/version':
                # Return HTML files modification time for live reload
                html_mtime = get_html_files_mtime(PROJECT_DIR)
                with regeneration_lock:
                    in_progress = regeneration_in_progress
                response = json.dumps({
                    'version': html_mtime,
                    'regenerating': in_progress
                })
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(response.encode())
            elif self.path.endswith('.html'):
                # Serve HTML files with injected auto-refresh script
                file_path = PROJECT_DIR / self.path.lstrip('/')
                if file_path.exists() and file_path.is_file():
                    content = file_path.read_text(encoding='utf-8')
                    # Inject auto-refresh script before </body>
                    auto_refresh_script = '''
<script>
(function() {
    let currentVersion = null;
    let pendingReload = false;

    function checkVersion() {
        fetch('/api/version')
            .then(r => r.json())
            .then(data => {
                // Don't reload while regenerating
                if (data.regenerating) {
                    console.log('Regenerating in progress...');
                    pendingReload = true;
                    return;
                }

                if (currentVersion === null) {
                    currentVersion = data.version;
                } else if ((data.version !== currentVersion && data.version > 0) || pendingReload) {
                    console.log('Page updated, reloading in 2s...');
                    pendingReload = false;
                    // Wait a bit for file to be fully written
                    setTimeout(() => location.reload(), 2000);
                }
            })
            .catch(() => {});
    }
    setInterval(checkVersion, 3000);
    checkVersion();
})();
</script>
'''
                    content = content.replace('</body>', auto_refresh_script + '</body>')
                    content_bytes = content.encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', len(content_bytes))
                    self.end_headers()
                    self.wfile.write(content_bytes)
                else:
                    self.send_error(404, 'File not found')
            else:
                super().do_GET()
        except Exception as e:
            print(f"Error handling request: {e}")
            self.send_error(500, str(e))

    def do_POST(self):
        try:
            if self.path == '/api/refresh':
                # Trigger log regeneration in background
                threading.Thread(target=regenerate_logs, daemon=True).start()
                response = json.dumps({'status': 'ok', 'message': 'Regeneration started'})
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(response.encode())
            elif self.path == '/api/delete-session':
                # Delete a session (jsonl and html files)
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                data = json.loads(post_data.decode('utf-8'))

                project_folder = data.get('project', '')
                session_id = data.get('session_id', '')

                if not project_folder or not session_id:
                    response = json.dumps({'status': 'error', 'message': 'Missing project or session_id'})
                else:
                    # Validate session_id format (UUID)
                    import re
                    if not re.match(r'^[a-f0-9-]{36}$', session_id):
                        response = json.dumps({'status': 'error', 'message': 'Invalid session_id format'})
                    else:
                        project_path = PROJECT_DIR / project_folder
                        if not project_path.exists() or not project_path.is_dir():
                            response = json.dumps({'status': 'error', 'message': 'Project not found'})
                        else:
                            deleted_files = []
                            errors = []

                            # Delete JSONL file
                            jsonl_file = project_path / f"{session_id}.jsonl"
                            if jsonl_file.exists():
                                try:
                                    jsonl_file.unlink()
                                    deleted_files.append(jsonl_file.name)
                                    print(f"[Delete] Deleted {jsonl_file}")
                                except Exception as e:
                                    errors.append(f"Failed to delete {jsonl_file.name}: {e}")

                            # Delete HTML file
                            html_file = project_path / f"session-{session_id}.html"
                            if html_file.exists():
                                try:
                                    html_file.unlink()
                                    deleted_files.append(html_file.name)
                                    print(f"[Delete] Deleted {html_file}")
                                except Exception as e:
                                    errors.append(f"Failed to delete {html_file.name}: {e}")

                            # Delete combined_transcripts.html to trigger regeneration
                            combined_file = project_path / "combined_transcripts.html"
                            if combined_file.exists():
                                try:
                                    combined_file.unlink()
                                    print(f"[Delete] Deleted combined_transcripts.html for regeneration")
                                except Exception:
                                    pass

                            if errors:
                                response = json.dumps({'status': 'error', 'message': '; '.join(errors)})
                            elif deleted_files:
                                response = json.dumps({'status': 'ok', 'deleted': deleted_files})
                            else:
                                response = json.dumps({'status': 'error', 'message': 'No files found to delete'})

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(response.encode())
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            print(f"Error handling POST: {e}")
            self.send_error(500, str(e))

    def log_message(self, format, *args):
        # Log requests for debugging
        print(f"[Request] {args[0]}")


def file_watcher():
    """Background thread to watch for file changes and auto-regenerate"""
    last_source_mtime = get_source_files_mtime(PROJECT_DIR)
    last_change_time = 0
    DEBOUNCE_SECONDS = 60  # Wait 60 seconds of no changes before regenerating (increased to avoid infinite loop)
    MAX_REGEN_FREQUENCY = 300  # Don't regenerate more often than every 5 minutes
    last_regen_time = 0

    while True:
        time.sleep(WATCH_INTERVAL)
        current_source_mtime = get_source_files_mtime(PROJECT_DIR)

        if current_source_mtime > last_source_mtime:
            # Source files changed, record the time
            last_change_time = time.time()
            last_source_mtime = current_source_mtime
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Source files changed, waiting for stability...")

        # Only regenerate if:
        # 1. There were changes AND no new changes for DEBOUNCE_SECONDS
        # 2. Haven't regenerated in the last MAX_REGEN_FREQUENCY seconds
        time_since_last_regen = time.time() - last_regen_time
        if last_change_time > 0 and (time.time() - last_change_time) >= DEBOUNCE_SECONDS:
            if time_since_last_regen < MAX_REGEN_FREQUENCY:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Skipping regeneration (last regen was {time_since_last_regen:.0f}s ago, min interval is {MAX_REGEN_FREQUENCY}s)")
                last_change_time = 0  # Reset to avoid spamming this message
                continue
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] No changes for {DEBOUNCE_SECONDS}s, regenerating...")
            regenerate_logs()
            last_change_time = 0  # Reset after regeneration
            last_regen_time = time.time()


def initial_regeneration(force_clean=False):
    """Run initial regeneration in background"""
    source_mtime = get_source_files_mtime(PROJECT_DIR)
    html_mtime = get_html_files_mtime(PROJECT_DIR)

    # Always regenerate to ensure latest data, but don't delete old files unless forced
    if source_mtime > html_mtime:
        print(f"Source files are newer (source: {source_mtime:.0f}, html: {html_mtime:.0f})")
    else:
        print(f"HTML files appear up-to-date, but regenerating anyway to ensure freshness...")

    if force_clean:
        print("Cleaning old HTML files...")
        for html_file in PROJECT_DIR.rglob("*.html"):
            try:
                html_file.unlink()
            except Exception:
                pass

    print("Regenerating logs (this may take a moment)...")
    regenerate_logs(force_clear=force_clean)
    print("Initial regeneration complete.")


def main():
    print("=" * 60)
    print("Enhanced Claude Log Viewer Server")
    print("=" * 60)
    print(f"Project directory: {PROJECT_DIR}")
    print(f"Port: {PORT}")
    print()

    # Check if we need to regenerate
    has_html = any(PROJECT_DIR.rglob("*.html"))

    # Start server FIRST so it's immediately available
    # Use ThreadingTCPServer for concurrent requests
    class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def server_bind(self):
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            super().server_bind()

    httpd = ThreadedTCPServer(("", PORT), LogViewerHandler)

    print(f"Server running at http://127.0.0.1:{PORT}")
    print("Press Ctrl+C to stop")
    print("-" * 60)

    # Run initial regeneration in background thread
    if not has_html:
        print("\nNo HTML files found. Regenerating in background...")
        regen_thread = threading.Thread(target=lambda: initial_regeneration(force_clean=True), daemon=True)
        regen_thread.start()
    else:
        print("\nRegenerating to ensure latest data...")
        regen_thread = threading.Thread(target=initial_regeneration, daemon=True)
        regen_thread.start()

    # Start file watcher thread
    watcher_thread = threading.Thread(target=file_watcher, daemon=True)
    watcher_thread.start()
    print(f"File watcher started (checking every {WATCH_INTERVAL}s)\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
