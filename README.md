# ServiceNow Local Coding Agent

> A local, privacy-preserving AI coding agent for ServiceNow AI Agents and Fluent SDK development. Runs entirely on your machine. No data leaves. No cloud API calls.

Built for enterprise POC scenarios where tools like Claude Code are blocked by data sensitivity policies. The pitch: *"we built this on a laptop — here's how it works in your VPC."*

---

## TL;DR

```bash
git clone https://github.com/tdimaggio/local_coding_agent.git
cd local_coding_agent
cp .env.example .env  # add your SN instance creds for schema validation

# Mac/Linux
./bootstrap.sh

# Windows
.\bootstrap.ps1
```

Then ingest the corpus and start coding:

```bash
uv run python rag/ingest.py   # ~15 min focused ingest
./scripts/start.sh            # RAG service + Aider
```

---

## Why this exists

Enterprise customers want AI-assisted ServiceNow development. Many won't (or can't) send their schemas, configs, and runbooks to a cloud LLM. That's a real blocker to AI adoption.

This agent flips the script: everything runs locally, the corpus is indexed locally, and the audit log stays on-machine. If a customer asks *"what did your AI do with our data?"* — you have a complete answer.

**Secondary benefits:** offline work, zero API costs, full control over what's in context.

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│  Zed + Aider  (coding agent loop)                │
│  Terminal-native, git-aware, no telemetry        │
└────────────────────┬─────────────────────────────┘
                     │  OpenAI-compatible API
                     ▼
┌──────────────────────────────────────────────────┐
│  RAG Proxy  (localhost:8766)                     │
│  Intercepts every Aider request, prepends RAG    │
│  context into the system message, then forwards  │
└──────┬──────────────────────┬────────────────────┘
       │  /retrieve           │  augmented request
       ▼                      ▼
┌─────────────────┐  ┌────────────────────────────┐
│  FastAPI RAG    │  │  Ollama  (localhost:11434)  │
│  Service :8765  │  │  ├── DeepSeek-Coder-V2-Lite │
│  ├── sqlite-vec │  │  │     ~11s on M4 24GB      │
│  ├── On-demand  │  │  └── nomic-embed-text        │
│  ├── SN schema  │  └────────────────────────────┘
│  └── Audit log  │
└────────┬────────┘
         │  Table REST API (optional)
         ▼
┌──────────────────────────────────────────────────┐
│  ServiceNow Instance  (optional, for guardrails) │
│  ├── sys_db_object   table validation            │
│  └── sys_dictionary  field name verification     │
└──────────────────────────────────────────────────┘
```

### Key design decisions

| Decision | Rationale |
|---|---|
| RAG over fine-tuning | Gets 80-90% of the way there in a fraction of the time. |
| DeepSeek-Coder-V2-Lite over Qwen 32B | MoE architecture: 2.4B active params, ~11s responses. Qwen 32B hit memory bandwidth limits on M4 24GB (>1min/response). |
| Focused corpus + on-demand expansion | Core 3.4k files ingest in ~15 min. Low-confidence queries automatically grep the full 46k clone, embed matching files, and re-retrieve. |
| Live schema injection | Before generation, RAG server pulls real table/field names from the connected SN instance via Table REST API. Model codes against verified schema, not training data. |
| sqlite-vec over Chroma | Single file, no daemon, backs up like any SQLite DB. |
| Aider → RAG proxy | Aider points at a thin local proxy (:8766) instead of Ollama directly. Proxy fetches RAG context and injects it into every request transparently — zero Aider workflow change. |
| No Docker | Ollama in Docker on Mac kills Metal GPU passthrough — 5-10x slower inference. |
| Audit log from day 1 | Schema locked from the start: timestamp, query, retrieved sources, output, model, profile. |

---

## Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | 3.11+ | [python.org](https://python.org) |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| git | any | [git-scm.com](https://git-scm.com) |
| Ollama | latest | [ollama.com](https://ollama.com) |
| Aider | latest | `uv tool install aider-chat` |

Ollama must be **running** before bootstrap. On Mac/Linux: `ollama serve`. On Windows: start from system tray.

---

## Quick start

### 1. Bootstrap

```bash
./bootstrap.sh        # Mac/Linux
.\bootstrap.ps1       # Windows
python bootstrap.py   # Any platform
```

Bootstrap is idempotent — safe to re-run. It checks prereqs, pulls models, runs `uv sync`, and fetches the corpus.

> Model pull: `deepseek-coder-v2:16b-lite-instruct-q4_K_M` (~10GB) + `nomic-embed-text` (~274MB)

### 2. Configure credentials (optional — enables live schema guardrails)

```bash
cp .env.example .env
# Set SN_INSTANCE, SN_USERNAME, SN_PASSWORD
```

Without `.env`, the agent works fine — schema validation is silently skipped.

### 3. Ingest the corpus

```bash
uv run python rag/ingest.py
```

Embeds the focused core corpus (~3.4k files: app dev, workflows, AI intelligence + `llms.txt`). Takes ~15 minutes. Fluent SDK chunks get boosted retrieval weight.

To ingest additional directories manually:

```bash
uv run python rag/ingest.py --dirs platform-security integrate-applications
```

To ingest everything (slow):

```bash
uv run python rag/ingest.py --dirs   # no args = all 46k files
```

### 4. Start the agent

```bash
./scripts/start.sh
```

Launches the RAG service (`:8765`), the RAG proxy (`:8766`), then opens Aider. Every prompt you type in Aider automatically gets ServiceNow docs retrieved and injected — no extra steps.

Windows:
```bash
uv run uvicorn rag.server:app --host 127.0.0.1 --port 8765 &
uv run uvicorn rag.proxy:app --host 127.0.0.1 --port 8766 &
aider --config config/aider.conf.yml
```

---

## Repo structure

```
local_coding_agent/
├── bootstrap.py           # Cross-platform setup (Mac/Linux/Windows)
├── bootstrap.sh           # Mac/Linux wrapper
├── bootstrap.ps1          # Windows PowerShell wrapper
├── pyproject.toml         # uv-managed Python deps
├── .env.example           # SN credential template (copy to .env)
│
├── config/
│   ├── aider.conf.yml     # Aider → Ollama, loads system prompt
│   └── system-prompt.md   # ServiceNow guardrails, Fluent SDK idioms, live schema instructions
│
├── corpus/                # Not committed — fetched at setup
│   ├── fetch-docs.sh      # Pulls ServiceNowDocs + llms.txt
│   ├── llms.txt           # Fluent SDK LLM-optimized reference
│   └── ServiceNowDocs/    # Full 46k-file clone (australia branch)
│
├── rag/
│   ├── server.py          # FastAPI RAG service (localhost:8765)
│   ├── proxy.py           # OpenAI-compatible proxy — Aider → RAG → Ollama (localhost:8766)
│   ├── ingest.py          # Chunking + embedding pipeline
│   ├── sn_schema.py       # Live ServiceNow schema validation
│   └── data/              # sqlite-vec DB + audit log (not committed)
│
├── scripts/
│   ├── start.sh           # Launch RAG service + Aider
│   ├── phase1-test.sh     # Model gate test script
│   └── audit-export.sh    # Export audit log to CSV or JSON
│
└── phase1-output/         # Model gate test outputs (reference)
```

---

## Corpus strategy

Three layers, loaded progressively:

**Layer 1 — Focused core (ingested upfront, ~15 min)**
- `llms.txt` — Fluent SDK API reference, boosted retrieval weight
- `application-development/` — Fluent SDK, app engine, Now SDK (1,498 files)
- `build-workflows/` — Flow Designer, WFA, subflows (695 files)
- `now-intelligence/` — AI Agents, Now Assist, ML (1,170 files)

**Layer 2 — On-demand expansion (automatic)**
When a query scores below confidence threshold (0.4), the server greps the full local clone for matching files, embeds them, and re-retrieves. Newly embedded files stay in the DB — the agent gets smarter with every query.

**Layer 3 — Engagement-specific**
Drop customer runbooks, schema exports, or governance docs into `corpus/customer-docs/` and re-run ingest. The agent becomes their-implementation-aware, not just ServiceNow-aware — and none of it left the machine.

---

## Live schema validation

When `.env` is configured with ServiceNow credentials, the RAG server queries the live instance before each generation:

1. Detects `sn_*` / `sys_*` table names in the query
2. Calls `sys_db_object` to verify the table exists
3. Calls `sys_dictionary` to get real field names and types
4. Injects a "Live ServiceNow Schema" block into the model's context

The model is instructed to use only field names from that block and to flag any table marked `TABLE NOT FOUND ON THIS INSTANCE`. Prevents hallucinated table/field names from making it into generated code.

---

## Audit log

Every query, retrieval, and generated output is logged to `rag/data/audit.db`.

Schema: `id | timestamp | query | retrieved_sources | generated_output | model | profile | latency_ms`

Export for customer review:

```bash
./scripts/audit-export.sh --format json --out audit/session-export.json
./scripts/audit-export.sh --format csv
```

---

## Deployability

### Default: git clone and go

Fresh machine to working agent in ~30 minutes (mostly model download).

### Multi-customer: profile-based (Phase 3)

```
profiles/
├── default.yml          # Generic ServiceNow
├── customer-acme.yml    # ACME's corpus, system prompt variant, audit destination
└── customer-foo.yml
```

```bash
./scripts/start.sh --profile customer-acme
```

---

## Phase status

| Phase | Status | Summary |
|---|---|---|
| Phase 1 | ✅ Done | DeepSeek-Coder-V2-Lite selected. ~11s responses on M4 24GB. |
| Phase 2 | ✅ Done | RAG service live. 8,197 chunks indexed (179 Fluent SDK + 8,018 docs). Schema validation wired. Scores validated. |
| Phase 3 | Planned | Profile-based deployability when customer 2 appears. |

---

## Reference

- [ServiceNow Fluent SDK docs](https://servicenow.github.io/sdk/)
- [ServiceNowDocs repo](https://github.com/ServiceNow/ServiceNowDocs)
- [Ollama](https://ollama.com)
- [Aider](https://aider.chat)
- [sqlite-vec](https://github.com/asg017/sqlite-vec)
- [uv](https://github.com/astral-sh/uv)
- [DeepSeek-Coder-V2-Lite](https://huggingface.co/deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct)
