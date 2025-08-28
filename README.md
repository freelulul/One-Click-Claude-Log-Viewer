# One-Click Claude Log Viewer

A simple shell script to automate the process of cleaning, regenerating, and viewing your Claude Code logs in a local web server.

## Overview

The `claude-code-log` tool is fantastic for converting conversation transcripts into readable HTML. However, the typical workflow involves manually deleting old reports, running the generator, and then starting a web server.

This script streamlines that entire process into a single, easy-to-run command.

## ✨ Features

-   **Automatic Cleanup**: Deletes all old `*.html` reports from your project directory before each run.
-   **Report Regeneration**: Automatically calls `claude-code-log` to generate fresh, up-to-date reports.
-   **Instant Viewing**: Starts a local web server using `live-server` (with auto-reload) or falls back to Python's built-in server.
-   **Cross-Platform**: Works on any Unix-like system (Linux, macOS, WSL on Windows).

## 🔧 Prerequisites

Before running the script, make sure you have the following installed:

1.  **uv**: The script uses `uvx` to run `claude-code-log`. You can install it via `pip install uv`.
2.  **A Local Server (one of the following)**:
    -   **Recommended**: `live-server` for a better experience with auto-reloading. Install it via Node.js:
        ```sh
        npm install -g live-server
        ```
    -   **Fallback**: **Python 3** is required if `live-server` is not found. Most systems have this pre-installed.

## 🚀 Getting Started

1.  **Download the Script**
    Save the script from this repository as `claude_log_viewer.sh` on your local machine.

2.  **Make it Executable**
    Open your terminal and run the following command to grant execute permissions:
    ```sh
    chmod +x claude_log_viewer.sh
    ```

## ⚙️ Usage

Simply run the script from your terminal:

```sh
./claude_log_viewer.sh
```

The script will perform all steps automatically, and your default web browser will open with the main `index.html` log page.

## 💡 How It Works

The script executes three main steps in sequence:

1.  **Navigates** to the default Claude projects directory (`~/.claude/projects`).
2.  **Cleans** all existing `*.html` files to prevent outdated reports.
3.  **Generates** new HTML reports by running `uvx claude-code-log@latest`.
4.  **Serves** the current directory on `http://127.0.0.1:8080` and opens it in your browser.

### Customization

You can easily change the project directory or port by editing the variables at the top of the script file:

```bash
# --- Configuration ---
PROJECT_DIR="$HOME/.claude/projects"
PORT=8080
```
