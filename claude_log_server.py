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
PORT = 8080
WATCH_INTERVAL = 5  # seconds


class SessionInfo:
    """Store session metadata"""
    def __init__(self, session_id, title, timestamp_start, timestamp_end, messages, tokens, preview, file_path):
        self.session_id = session_id
        self.title = title
        self.timestamp_start = timestamp_start
        self.timestamp_end = timestamp_end
        self.messages = messages
        self.tokens = tokens
        self.preview = preview
        self.file_path = file_path


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


def regenerate_logs():
    """Run claude-code-log to regenerate HTML files"""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Regenerating logs...")
    try:
        result = subprocess.run(
            ["uvx", "claude-code-log@latest"],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=300
        )
        if result.returncode == 0:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Logs regenerated successfully")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Warning: claude-code-log returned non-zero")
            if result.stderr:
                print(f"  stderr: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Warning: claude-code-log timed out")
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error regenerating: {e}")


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

        # Find individual session file
        parent_dir = html_path.parent
        session_file = parent_dir / f"session-{session_id}.html"

        sessions.append(SessionInfo(
            session_id=session_id,
            title=title,
            timestamp_start=ts_start,
            timestamp_end=ts_end,
            messages=messages,
            tokens=tokens,
            preview=preview,
            file_path=str(session_file.relative_to(PROJECT_DIR)) if session_file.exists() else None
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
            for session in sessions:
                session_file = session.file_path or combined_path
                session_display = session.title if session.title else session.session_id[:8]

                html_content += f'''
                <div class="session-item" data-session="{html.escape(session.session_id)}">
                    <div class="session-title">
                        <span>{html.escape(session_display)}</span>
                        <span class="id">{session.session_id[:8]}</span>
                    </div>
                    <div class="session-meta">
                        {session.messages} messages
                    </div>
                    <div class="session-tokens">{html.escape(session.tokens)}</div>
                    <div class="session-preview">{html.escape(session.preview[:150])}</div>
                    <div class="session-actions">
                        <a href="{html.escape(session_file)}" class="btn-view">View Session</a>
                        <a href="{html.escape(session_file)}" target="_blank" class="btn-new-tab">Open in New Tab</a>
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

        // Auto-check for updates every 30 seconds
        setInterval(() => {
            fetch('/api/check-update')
                .then(r => r.json())
                .then(data => {
                    if (data.needsUpdate) {
                        document.getElementById('refreshNotice').style.display = 'block';
                        document.getElementById('refreshNotice').textContent = 'New logs detected! Click to refresh.';
                        document.getElementById('refreshNotice').onclick = () => location.reload();
                        document.getElementById('refreshNotice').style.cursor = 'pointer';
                    }
                });
        }, 30000);

        // Expand first project by default
        document.addEventListener('DOMContentLoaded', () => {
            const firstProject = document.querySelector('.project-header');
            if (firstProject) {
                toggleProject(firstProject);
            }
        });
    </script>
</body>
</html>
'''
    return html_content


class LogViewerHandler(http.server.SimpleHTTPRequestHandler):
    """Custom HTTP handler with API endpoints"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_DIR), **kwargs)

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

    while True:
        time.sleep(WATCH_INTERVAL)
        current_source_mtime = get_source_files_mtime(PROJECT_DIR)

        if current_source_mtime > last_source_mtime:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Source files changed, regenerating...")
            regenerate_logs()
            last_source_mtime = current_source_mtime


def initial_regeneration():
    """Run initial regeneration in background"""
    print("Cleaning old HTML files...")
    for html_file in PROJECT_DIR.rglob("*.html"):
        try:
            html_file.unlink()
        except Exception:
            pass
    print("Regenerating logs (this may take a moment)...")
    regenerate_logs()
    print("Initial regeneration complete. Refresh browser to see sessions.")


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

    httpd = ThreadedTCPServer(("", PORT), LogViewerHandler)

    print(f"Server running at http://127.0.0.1:{PORT}")
    print("Press Ctrl+C to stop")
    print("-" * 60)

    # Run initial regeneration in background thread
    if not has_html:
        print("\nNo HTML files found. Regenerating in background...")
    else:
        print("\nExisting HTML files found. Regenerating in background for fresh data...")

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
