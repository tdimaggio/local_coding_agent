# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local, privacy-preserving coding agent for ServiceNow AI Agents and Fluent SDK development. Everything runs on-machine: Ollama for inference, sqlite-vec for retrieval, FastAPI for the RAG service, Aider as the editor loop. No data leaves the host. Targeted at customer POC scenarios where cloud coding tools are blocked by enterprise data sensitivity policies.

Repo: https://github.com/tdimaggio/local_coding_agent

## Phase status

- **Phase 1** ✅ — Model selection done. `deepseek-coder-v2:16b-lite-instruct-q4_K_M` won over Qwen2.5-Coder-32B because the MoE architecture keeps responses ~11s on M4 24GB; Qwen 32B was memory-bandwidth-bound at >1min/response.
- **Phase 2** ✅ plumbing / ⚠️ corpus — RAG service, proxy, schema validation hook, and audit log all live and tested end-to-end. **Known limitation**: the corpus fetch only pulls `llms.txt` (~32 KB, a link index), not the actual Fluent SDK docs content. As a result the RAG store under-serves Fluent SDK code generation — the model gets vocabulary scraps in retrieval and hallucinates structure. See "Outstanding work" below.
- **Phase 3** ⏳ — Profile-based deployability. Deferred until customer 2.

## Outstanding work (priority order)

1. **Corpus content fetch (the unlock).** Build `corpus/fetch-sdk-content.sh` that parses `corpus/llms.txt` for `?embed=true` URLs (~75 of them, e.g. `https://servicenow.github.io/sdk/api/aiagent-api?embed=true`), fetches each to `corpus/sdk-embed/<slug>.md`, then triggers `uv run python rag/ingest.py --reset`. Wire into `bootstrap.py` as a step 5b or fold into step 5. Discovery: `https://servicenow.github.io/sdk/versions.json` lists versions; pin to most recent non-prerelease.
2. **Re-test the AiTool prompt after the corpus fix lands.** The system prompt was tightened with anti-hallucination rules on 2026-05-18 (after the first failing test), so the rules have not yet been validated against real output.
3. **Configure Aider's context window** in `config/aider.conf.yml`. Aider currently warns *"Unknown context window size"* and applies a conservative default; the model is actually 128 K. Set `extra-model-settings` or per-model overrides.
4. **Wire `--profile`** through start.sh + bootstrap.py for Phase 3 when a second customer engagement arrives.

## Commands

All commands assume `uv` is installed (the bootstrap will install it if not) and `ollama serve` is running.

```bash
# Setup — interactive 6-step installer (idempotent, safe to re-run)
./bootstrap.sh                # Mac/Linux
.\bootstrap.ps1               # Windows
python3 bootstrap.py          # any platform — directly invoke

# Flags
./bootstrap.sh --check        # detect prereqs, show summary, exit (no installs)
./bootstrap.sh --yes          # non-interactive — auto-confirm all prompts
./bootstrap.sh --skip-models  # skip Ollama model pull
./bootstrap.sh --skip-corpus  # skip corpus fetch
./bootstrap.sh --skip-ingest  # skip RAG index build (~6-15 min step)

# Manual ingest (also runs as step 6 of bootstrap)
uv run python rag/ingest.py
uv run python rag/ingest.py --dirs platform-security integrate-applications  # add specific dirs
uv run python rag/ingest.py --reset                                          # rebuild from scratch

# Run the agent stack (RAG service :8765 + proxy :8766 + Aider)
./scripts/start.sh

# Run individual services (debugging)
uv run uvicorn rag.server:app --host 127.0.0.1 --port 8765 --reload
uv run uvicorn rag.proxy:app  --host 127.0.0.1 --port 8766 --reload

# Tests / lint
uv run pytest                            # full suite
uv run pytest tests/test_x.py::test_y    # single test
uv run ruff check .
uv run ruff check --fix .

# Audit log export (sqlite at rag/data/audit.db)
./scripts/audit-export.sh --format json --out audit/session.json
./scripts/audit-export.sh --format csv
```

## Bootstrap — what each step does

The installer detects prereqs, prints a status table, then runs six numbered phases. `--check` exits after step 1.

1. **Detect prerequisites** — probes Python (≥3.11), git, uv, aider, ollama (CLI + service). Blockers (Python, git) cause exit with install hint; user-space tools (uv, aider) are auto-install; Ollama is prompted.
2. **Install missing prerequisites** — only runs if step 1 found gaps. Auto-installs `uv` (curl installer) and `aider` (`uv tool install --python 3.12 aider-chat`). The `--python 3.12` pin is intentional: scipy lacks wheels for Python 3.14+ as of writing, and aider depends on scipy.
3. **Pull models** — `deepseek-coder-v2:16b-lite-instruct-q4_K_M` (~10 GB) and `nomic-embed-text` (~274 MB). Idempotent — skips already-present.
4. **Set up Python env** — `uv sync` in repo root. Creates `.venv/`.
5. **Fetch corpus** — `corpus/fetch-docs.sh` clones ServiceNowDocs (australia branch, ~46k files) and fetches `llms.txt`. See "Outstanding work" — this step needs an extension to pull SDK content.
6. **Build RAG index** — `uv run python rag/ingest.py`. Embeds focused core (~3,364 files, 8,197 chunks, ~6 min on M4). Idempotent: detects existing `rag/data/rag.db` and skips with a hint to `--reset`.

## Architecture (the part that needs reading multiple files to grok)

Request flow is **Aider → proxy → RAG service → Ollama**:

1. **`rag/proxy.py` (`:8766`)** — OpenAI-compatible shim that Aider points at instead of Ollama directly. For every chat completion, it pulls the last user message, calls `/retrieve` on the RAG service, injects returned chunks into the system message, then streams the augmented request to Ollama at `:11434`. User messages shorter than `MIN_QUERY_LEN` (40 chars) skip RAG. Also **strips the litellm provider prefix** (e.g. `openai/deepseek-...` → `deepseek-...`) before forwarding to Ollama, because Aider/litellm requires the prefix to dispatch and Ollama wants the bare tag.

2. **`rag/server.py` (`:8765`)** — FastAPI service with `/retrieve`, `/generate`, `/health`. Owns the sqlite-vec DB (`rag/data/rag.db`) and the audit log (`rag/data/audit.db`). Two behaviors worth knowing:
   - **Source-weighted retrieval**: `SOURCE_BOOST` boosts `fluent-sdk` chunks (from `llms.txt`) by 1.3× because the base model has near-zero Fluent SDK training data. Don't remove this.
   - **On-demand expansion**: if top retrieval scores fall below `ON_DEMAND_THRESHOLD` (0.4), the server greps the full ServiceNowDocs clone, embeds matching files, and re-retrieves. New chunks persist.

3. **`rag/ingest.py`** — Chunking + embedding pipeline. `llms.txt` is split on `##` headings; markdown docs use 512-token chunks with 64-token overlap. Embeddings via `nomic-embed-text` at 768-dim.

4. **`rag/sn_schema.py`** — When `.env` has `SN_INSTANCE` + creds, the RAG server detects `sn_*`/`sys_*` table refs in the query, hits Table REST API on `sys_db_object` and `sys_dictionary`, and injects a "Live ServiceNow Schema" block into the system prompt. Without `.env` this silently skips. Verify activation via `GET /health` — `sn_schema_validation` field shows `enabled` or `disabled`.

5. **`config/system-prompt.md`** — Load-bearing. Top section is "Anti-hallucination rules" (added 2026-05-18 after the model invented imports/classes in early testing). Loaded by Aider via `read:` in `config/aider.conf.yml`. If you change retrieval shape or schema-injection format, update this file in lockstep.

6. **`config/aider.conf.yml`** — Points Aider at `:8766` (the proxy), not Ollama directly. **Model name must carry the `openai/` prefix** so litellm dispatches via the OpenAI-compatible path — the proxy strips it before Ollama. `auto-commits` and `dirty-commits` are off intentionally.

## Key conventions

- **No Docker.** Ollama in Docker on Mac loses Metal GPU passthrough (5-10× slower). Don't propose containerizing the inference layer.
- **No fine-tuning.** RAG covers 80-90% of the value; the architecture is committed to retrieval, not weight updates.
- **Audit schema is locked from day 1.** `id | timestamp | query | retrieved_sources | generated_output | model | profile | latency_ms`. Add columns by migration, never by rewriting history.
- **`corpus/` is gitignored.** Fetched via `corpus/fetch-docs.sh` (called by bootstrap). Full ServiceNowDocs clone is ~46k files.
- **`rag/data/` is gitignored.** Both `rag.db` and `audit.db` are local-only. Customer-facing audit exports go through `scripts/audit-export.sh`.
- **`.env` is gitignored.** Use `.env.example` as the template; never commit credentials.

## When making changes

- Touching retrieval (`rag/server.py`, `rag/ingest.py`) → run a `/retrieve` smoke test before declaring done. Currently the best test is the AiTool prompt from system-prompt history; once the corpus fix lands, build a tests/ harness.
- Touching the proxy (`rag/proxy.py`) → verify Aider still streams responses end-to-end. The OpenAI-compatible streaming contract is what Aider depends on. Also verify the model-name prefix strip still works for both `openai/foo` and bare `foo`.
- Touching `config/system-prompt.md` → check that retrieved chunks and live-schema blocks still land in positions the prompt references.
- Changing the model in `config/aider.conf.yml` → also update `MAIN_MODEL` in `rag/server.py`; two sources of truth that need to agree. Keep the `openai/` prefix on the Aider side; do NOT add it to `MAIN_MODEL` in the server.
