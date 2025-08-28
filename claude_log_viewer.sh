#!/bin/bash
# ==============================================================================
# One-Click Claude Log Viewer & Server
#
# Workflow:
# 1. Clean:     Recursively deletes all old .html log files to ensure a fresh start.
# 2. Generate:  Invokes the claude-code-log tool to regenerate all project reports.
# 3. View:      Starts a local web server to browse the newly generated reports.
# ==============================================================================

# --- Configuration ---
# Default path for Claude Code projects.
# Change this variable if your setup is different.
PROJECT_DIR="$HOME/.claude/projects"
PORT=8080

# --- Script Execution ---

echo "Navigating to the Claude project directory: $PROJECT_DIR"
cd "$PROJECT_DIR" || { echo "Error: Directory not found: $PROJECT_DIR"; exit 1; }
echo "Currently in: $(pwd)"
echo

# Step 1: Clean - Recursively delete all .html files in the directory
echo ">>> Step 1: Cleaning up all old .html reports..."
find . -type f -name "*.html" -delete
echo "Cleanup complete."
echo

# Step 2: Generate - Use claude-code-log to regenerate reports
echo ">>> Step 2: Regenerating all reports using claude-code-log..."
uvx claude-code-log@latest
if [ $? -ne 0 ]; then
    echo "Error: claude-code-log failed to generate reports. Please check the output above. Aborting."
    exit 1
fi
echo "Reports generated successfully."
echo

# Step 3: View - Start a local web server for browsing
echo ">>> Step 3: Starting a web server for browsing..."
if command -v live-server >/dev/null 2>&1; then
    echo "Starting live-server with auto-reload at http://127.0.0.1:$PORT..."
    # Explicitly serve the current directory "." to avoid argument parsing issues.
    live-server . --port $PORT --open
else
    echo "live-server not found, falling back to Python's http.server."
    echo "Hint: For auto-reload functionality, install live-server with: npm install -g live-server"
    echo "Please manually open http://127.0.0.1:$PORT in your browser."
    python3 -m http.server $PORT
fi
