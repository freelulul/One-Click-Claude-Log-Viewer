#!/bin/bash
# ==============================================================================
# One-Click Claude Log Viewer & Server
#
# Features:
# - Auto-regenerates HTML when source log files change
# - Session selector UI - view individual sessions instead of all at once
# - Real-time update detection
#
# Usage:
#   ./claude_log_viewer.sh           # Run enhanced server (default)
#   ./claude_log_viewer.sh --simple  # Run simple mode (original behavior)
# ==============================================================================

# --- Configuration ---
PROJECT_DIR="$HOME/.claude/projects"
PORT=8080
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Parse arguments ---
SIMPLE_MODE=false
if [[ "$1" == "--simple" ]]; then
    SIMPLE_MODE=true
fi

echo "============================================================"
echo "  Claude Log Viewer"
echo "============================================================"
echo

if [[ "$SIMPLE_MODE" == true ]]; then
    # Original simple mode
    echo "Running in simple mode..."
    echo "Project directory: $PROJECT_DIR"
    cd "$PROJECT_DIR" || { echo "Error: Directory not found: $PROJECT_DIR"; exit 1; }
    echo

    echo ">>> Step 1: Cleaning up old HTML files..."
    find . -type f -name "*.html" -delete
    echo "Done."
    echo

    echo ">>> Step 2: Regenerating reports..."
    uvx claude-code-log@latest
    if [ $? -ne 0 ]; then
        echo "Error: claude-code-log failed. Aborting."
        exit 1
    fi
    echo "Done."
    echo

    echo ">>> Step 3: Starting server..."
    if command -v live-server >/dev/null 2>&1; then
        echo "Starting live-server at http://127.0.0.1:$PORT..."
        live-server . --port $PORT --open
    else
        echo "Starting Python server at http://127.0.0.1:$PORT..."
        python3 -m http.server $PORT
    fi
else
    # Enhanced mode with Python server
    echo "Running enhanced mode with:"
    echo "  - Auto-regeneration on file changes"
    echo "  - Session selector UI"
    echo "  - Individual session viewing"
    echo
    python3 "$SCRIPT_DIR/claude_log_server.py"
fi
