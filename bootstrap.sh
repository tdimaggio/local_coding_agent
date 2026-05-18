#!/usr/bin/env bash
# bootstrap.sh — idempotent setup for the ServiceNow local coding agent
# Usage: ./bootstrap.sh [--profile <name>]
set -e

PROFILE="${2:-default}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================"
echo " ServiceNow Local Agent — Bootstrap"
echo " Profile: $PROFILE"
echo "============================================"
echo ""

# ── 1. Check Ollama ──────────────────────────────────────────────────────────
echo "==> Checking Ollama..."
if ! command -v ollama &>/dev/null; then
  echo "ERROR: Ollama not found. Install from https://ollama.com and re-run."
  exit 1
fi
if ! ollama list &>/dev/null; then
  echo "ERROR: Ollama is not running. Start it with: ollama serve"
  exit 1
fi
echo "    Ollama OK"

# ── 2. Pull models ────────────────────────────────────────────────────────────
echo ""
echo "==> Pulling models (this will be slow on first run)..."

# Embeddings — small, pull first
ollama pull nomic-embed-text

# Main model candidates — pull whichever aren't present
# Edit MAIN_MODEL below after Phase 1 gate to lock in your winner
CANDIDATE_MODELS=(
  "qwen2.5-coder:32b"
  "deepseek-coder-v2:16b-lite-instruct"
)
for model in "${CANDIDATE_MODELS[@]}"; do
  if ollama list | grep -q "$(echo "$model" | cut -d: -f1)"; then
    echo "    $model already present, skipping"
  else
    echo "    Pulling $model..."
    ollama pull "$model"
  fi
done

# ── 3. Python environment ─────────────────────────────────────────────────────
echo ""
echo "==> Setting up Python environment via uv..."
if ! command -v uv &>/dev/null; then
  echo "ERROR: uv not found. Install from https://github.com/astral-sh/uv and re-run."
  exit 1
fi
cd "$REPO_DIR"
uv sync
echo "    Python env ready"

# ── 4. Corpus ─────────────────────────────────────────────────────────────────
echo ""
echo "==> Fetching corpus..."
bash "$REPO_DIR/corpus/fetch-docs.sh"

# ── 5. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "============================================"
echo " Bootstrap complete!"
echo ""
echo " Next steps:"
echo "   1. Run both models against the Phase 1 test prompt:"
echo "      ./scripts/phase1-test.sh"
echo "   2. Pick the winner, set MAIN_MODEL in scripts/start.sh"
echo "   3. Ingest corpus: uv run python rag/ingest.py"
echo "   4. Start RAG service: ./scripts/start.sh"
echo "============================================"
