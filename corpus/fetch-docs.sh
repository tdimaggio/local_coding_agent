#!/usr/bin/env bash
# fetch-docs.sh — pull ServiceNow SDK docs and ServiceNowDocs repo into corpus/
set -e

CORPUS_DIR="$(cd "$(dirname "$0")" && pwd)"
SDK_LLMS_URL="https://servicenow.github.io/sdk/llms.txt"
SN_DOCS_REPO="https://github.com/ServiceNow/ServiceNowDocs.git"
SN_DOCS_BRANCH="australia"

echo "==> Fetching Fluent SDK llms.txt..."
curl -fsSL "$SDK_LLMS_URL" -o "$CORPUS_DIR/llms.txt"
echo "    Saved to corpus/llms.txt"

echo "==> Cloning ServiceNowDocs (australia branch, full repo)..."
if [ -d "$CORPUS_DIR/ServiceNowDocs" ]; then
  echo "    ServiceNowDocs already exists, pulling latest..."
  git -C "$CORPUS_DIR/ServiceNowDocs" pull
else
  git clone \
    --depth 1 \
    --branch "$SN_DOCS_BRANCH" \
    "$SN_DOCS_REPO" \
    "$CORPUS_DIR/ServiceNowDocs"
fi
echo "    ServiceNowDocs ready at corpus/ServiceNowDocs/"

echo ""
echo "Corpus ready. Run 'python rag/ingest.py' to embed and index."
