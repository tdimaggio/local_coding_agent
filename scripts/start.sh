#!/usr/bin/env bash
# start.sh — launch the RAG service and Aider coding agent
# Usage: ./scripts/start.sh [--profile <name>]
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="default"

while [[ $# -gt 0 ]]; do
  case $1 in
    --profile) PROFILE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "Starting ServiceNow Local Agent (profile: $PROFILE)..."

# Check Ollama is alive
if ! curl -sf http://localhost:11434/ &>/dev/null; then
  echo "ERROR: Ollama not responding at localhost:11434. Run: ollama serve"
  exit 1
fi

# Start RAG service in background
echo "==> Starting RAG service on :8765..."
uv run uvicorn rag.server:app --host 127.0.0.1 --port 8765 --reload &
RAG_PID=$!
echo "    RAG PID: $RAG_PID"

# Wait for RAG to be ready
sleep 2

# Launch Aider
echo "==> Launching Aider..."
echo "    Context: @config/system-prompt.md loaded automatically"
echo "    Use @corpus/llms.txt or @corpus/ServiceNowDocs/... to reference docs"
echo ""
aider --config "$REPO_DIR/config/aider.conf.yml"

# Cleanup on exit
kill $RAG_PID 2>/dev/null
echo "RAG service stopped."
