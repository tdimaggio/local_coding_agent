#!/usr/bin/env bash
# audit-export.sh — export the audit log to a readable format
# Usage: ./scripts/audit-export.sh [--format csv|json] [--out <path>]
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB_PATH="$REPO_DIR/rag/data/audit.db"
FORMAT="json"
OUT_PATH="$REPO_DIR/audit/export-$(date +%Y%m%d-%H%M%S).$FORMAT"

while [[ $# -gt 0 ]]; do
  case $1 in
    --format) FORMAT="$2"; shift 2 ;;
    --out)    OUT_PATH="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$(dirname "$OUT_PATH")"

if [ ! -f "$DB_PATH" ]; then
  echo "ERROR: Audit DB not found at $DB_PATH"
  exit 1
fi

echo "Exporting audit log to $OUT_PATH..."

if [ "$FORMAT" = "csv" ]; then
  sqlite3 -csv -header "$DB_PATH" \
    "SELECT id, timestamp, query, retrieved_sources, generated_output, model, profile FROM audit_log ORDER BY timestamp;" \
    > "$OUT_PATH"
else
  sqlite3 -json "$DB_PATH" \
    "SELECT id, timestamp, query, retrieved_sources, generated_output, model, profile FROM audit_log ORDER BY timestamp;" \
    > "$OUT_PATH"
fi

echo "Done. $(wc -l < "$OUT_PATH") lines written to $OUT_PATH"
