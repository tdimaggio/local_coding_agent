#!/usr/bin/env bash
# bootstrap.sh — Mac/Linux convenience wrapper around bootstrap.py
# For Windows: run `python bootstrap.py` directly
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# Prefer uv-managed Python, fall back to system Python
if command -v uv &>/dev/null; then
  exec uv run python "$REPO_DIR/bootstrap.py" "$@"
else
  exec python3 "$REPO_DIR/bootstrap.py" "$@"
fi
