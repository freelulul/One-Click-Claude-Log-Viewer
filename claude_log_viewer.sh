#!/usr/bin/env bash
# One-click launcher for cc-log-viewer.
# Picks a Python 3.9+ interpreter (zoneinfo is required for IANA timezones).
# All script flags pass through to the cc_log_viewer module.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Try a series of Python interpreters, prefer newest. zoneinfo needs >=3.9.
candidates=(
  "${CC_LOG_PYTHON:-}"
  /opt/conda/bin/python3
  python3.13 python3.12 python3.11 python3.10 python3.9
  python3 python
)

PY=""
for cand in "${candidates[@]}"; do
  [ -z "$cand" ] && continue
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys, zoneinfo' >/dev/null 2>&1; then
      PY="$cand"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "Error: cc-log-viewer needs Python 3.9+ (zoneinfo). None of:" >&2
  printf '  %s\n' "${candidates[@]}" >&2
  echo "  worked. Set CC_LOG_PYTHON=/path/to/python and retry." >&2
  exit 1
fi

exec "$PY" -m cc_log_viewer "$@"
