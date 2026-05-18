#!/usr/bin/env bash
# bootstrap.sh — Mac/Linux wrapper around bootstrap.py.
# bootstrap.py handles its own prereq detection / install, so we run with
# system python3 here (uv may not exist yet on a fresh machine).
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install Python 3.11+ from https://python.org" >&2
  exit 1
fi

exec python3 "$REPO_DIR/bootstrap.py" "$@"
