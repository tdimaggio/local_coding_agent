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
uv run uvicorn rag.server:app --host 127.0.0.1 --port 8765 &
RAG_PID=$!
echo "    RAG PID: $RAG_PID"

# Start RAG proxy in background (Aider → proxy → Ollama)
echo "==> Starting RAG proxy on :8766..."
uv run uvicorn rag.proxy:app --host 127.0.0.1 --port 8766 &
PROXY_PID=$!
echo "    Proxy PID: $PROXY_PID"

# Wait for services to be ready
sleep 3

# Verify proxy is up
if ! curl -sf http://localhost:8766/health &>/dev/null; then
  echo "WARN: proxy not responding — Aider may lack RAG context"
fi

# Launch Aider
echo "==> Launching Aider (RAG context injected automatically)..."
echo "    Aider → :8766 (proxy) → RAG → Ollama"
echo ""
aider --config "$REPO_DIR/config/aider.conf.yml"

# Cleanup on exit
kill $RAG_PID $PROXY_PID 2>/dev/null
echo "RAG service and proxy stopped."
