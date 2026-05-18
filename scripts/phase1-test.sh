#!/usr/bin/env bash
# phase1-test.sh — run Phase 1 model gate: same prompt against both candidates
# Produces TypeScript files you can type-check to evaluate output quality
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$REPO_DIR/phase1-output"
mkdir -p "$OUT_DIR"

PROMPT="Using the ServiceNow Fluent SDK, create an AI Agent named 'IncidentTriageAgent' that:
1. Has a CRUD tool to query the incident table (filter: active=true, priority=1)
2. Has a script tool that calculates SLA breach risk based on opened_at and sla_due fields
3. Is scoped to application scope 'x_demo_triage'
4. Has appropriate ACLs so it can only be invoked by the itil role
Import from @servicenow/sdk/core. Include all necessary types and exports."

MODELS=(
  "qwen2.5-coder:32b"
  "deepseek-coder-v2:16b-lite-instruct"
)

for model in "${MODELS[@]}"; do
  safe_name="${model//[^a-zA-Z0-9]/_}"
  out_file="$OUT_DIR/${safe_name}.ts"

  echo ""
  echo "==> Testing $model..."
  echo "    Output: $out_file"

  # Prepend system prompt to user prompt
  system=$(cat "$REPO_DIR/config/system-prompt.md")
  full_prompt="$system

---

$PROMPT"

  curl -sf http://localhost:11434/api/generate \
    -H 'Content-Type: application/json' \
    -d "$(jq -n \
      --arg model "$model" \
      --arg prompt "$full_prompt" \
      '{model: $model, prompt: $prompt, stream: false}')" \
    | jq -r '.response' \
    > "$out_file"

  echo "    Done. $(wc -c < "$out_file") bytes written."
done

echo ""
echo "============================================"
echo " Phase 1 outputs saved to phase1-output/"
echo ""
echo " To type-check:"
echo "   cd phase1-output"
echo "   npm init -y && npm install @servicenow/sdk typescript"
echo "   npx tsc --noEmit --strict *.ts"
echo ""
echo " Review both outputs, pick the winner, then:"
echo "   Edit config/aider.conf.yml → set model: <winner>"
echo "============================================"
