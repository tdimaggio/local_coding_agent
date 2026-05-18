# ServiceNow Local Coding Agent

> A local, privacy-preserving AI coding agent for ServiceNow AI Agents and Fluent SDK development. Runs entirely on your machine. No data leaves. No cloud API calls.

Built for enterprise POC scenarios where tools like Claude Code are blocked by data sensitivity policies. The pitch: *"we built this on a laptop — here's how it works in your VPC."*

---

## TL;DR

```bash
git clone https://github.com/tdimaggio/local_coding_agent.git
cd local_coding_agent

# Mac/Linux
./bootstrap.sh

# Windows
.\bootstrap.ps1
```

Then run the Phase 1 gate to pick your model, ingest the corpus, and start coding.

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
│  Ollama  (localhost:11434)                       │
│  ├── Main model  DeepSeek-Coder-V2-Lite Q4       │
│  │     Phase 1 winner — ~11s on M4 24GB          │
│  └── nomic-embed-text  (embeddings)              │
└────────────────────┬─────────────────────────────┘
                     │  RAG retrieval
                     ▼
┌──────────────────────────────────────────────────┐
│  FastAPI RAG Service  (localhost:8765)           │
│  ├── sqlite-vec  vector store                    │
│  ├── Audit log   every query + result, on-disk   │
│  └── Corpus                                      │
│        ServiceNowDocs/  (46k+ markdown files)    │
│        llms.txt          Fluent SDK reference    │
│        customer-docs/    your engagement corpus  │
└──────────────────────────────────────────────────┘
```

### Key design decisions

| Decision | Rationale |
|---|---|
| RAG over fine-tuning | Gets 80-90% of the way there in a fraction of the time. Revisit only if RAG hits a clear wall. |
| sqlite-vec over Chroma | Single file, no daemon, backs up like any SQLite DB. |
| Aider over Cline | Terminal-native, git-aware, transparent. `--openai-api-base` points it at Ollama. |
| No Docker | Ollama in Docker on Mac kills Metal GPU passthrough — 5-10x slower inference. |
| Audit log from day 1 | Retrofitting structured logging is painful. Schema is locked from the start. |
| System prompt as load-bearing | ServiceNow scope rules, `sn_aia_*` conventions, Fluent SDK idioms — without guardrails the model hallucinates plausible-but-broken GlideScript. |

---

## Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | 3.11+ | [python.org](https://python.org) |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| git | any | [git-scm.com](https://git-scm.com) |
| Ollama | latest | [ollama.com](https://ollama.com) |
| Aider | latest | `uv tool install aider-chat` |

Ollama must be **running** before bootstrap. On Mac/Linux: `ollama serve`. On Windows: start the Ollama app from the system tray.

---

## Quick start

### 1. Bootstrap

```bash
# Mac/Linux
./bootstrap.sh

# Windows
.\bootstrap.ps1

# Or directly, any platform
python bootstrap.py
```

Bootstrap is idempotent — safe to re-run. It:
- Checks all prerequisites
- Pulls `nomic-embed-text`, `qwen2.5-coder:32b`, and `deepseek-coder-v2:16b-lite-instruct` via Ollama
- Sets up the Python env via `uv sync`
- Clones ServiceNowDocs and fetches the Fluent SDK `llms.txt`

> Model pulls are ~20GB total. Plan accordingly.

### 2. Run the Phase 1 gate

```bash
./scripts/phase1-test.sh
```

Sends the same Fluent SDK prompt to both candidate models. Outputs TypeScript files to `phase1-output/`. Type-check them to pick the winner:

```bash
cd phase1-output
npm init -y && npm install @servicenow/sdk typescript
npx tsc --noEmit --strict *.ts
```

Whichever model produces valid TypeScript is your main model. Update `config/aider.conf.yml`:

```yaml
model: qwen2.5-coder:32b  # or deepseek-coder-v2:16b-lite-instruct
```

### 3. Ingest the corpus

```bash
uv run python rag/ingest.py
```

Chunks and embeds ServiceNowDocs + `llms.txt` into the local sqlite-vec database. Fluent SDK chunks get retrieval priority — the base model has near-zero training data on it.

### 4. Start the agent

```bash
# Mac/Linux
./scripts/start.sh

# Windows (coming soon — use uv run directly for now)
uv run uvicorn rag.server:app --host 127.0.0.1 --port 8765 &
aider --config config/aider.conf.yml
```

Aider launches with the ServiceNow system prompt pre-loaded. Use `@`-references to pull specific docs into context:

```
@corpus/llms.txt explain the AIAgent() API
@corpus/ServiceNowDocs/markdown/application-development/... build me a business rule
```

---

## Repo structure

```
local_coding_agent/
├── bootstrap.py           # Cross-platform bootstrap (Mac/Linux/Windows)
├── bootstrap.sh           # Mac/Linux wrapper
├── bootstrap.ps1          # Windows PowerShell wrapper
├── pyproject.toml         # uv-managed Python deps
│
├── config/
│   ├── aider.conf.yml     # Aider config — points at Ollama, loads system prompt
│   └── system-prompt.md   # ServiceNow guardrails, Fluent SDK idioms, scope rules
│
├── corpus/                # Fetched at setup, not committed
│   ├── .gitignore
│   ├── fetch-docs.sh      # Pulls ServiceNowDocs + llms.txt
│   ├── llms.txt           # Fluent SDK LLM-optimized reference
│   └── ServiceNowDocs/    # 46k+ markdown files, australia branch
│
├── rag/
│   ├── server.py          # FastAPI RAG service (localhost:8765)
│   ├── ingest.py          # Chunking + embedding pipeline
│   └── data/              # sqlite-vec DB (not committed)
│
└── scripts/
    ├── start.sh           # Launch RAG service + Aider
    ├── phase1-test.sh     # Phase 1 model gate
    └── audit-export.sh    # Export audit log to CSV or JSON
```

---

## Corpus

The corpus is the secret weapon. Two layers:

**Layer 1 — ServiceNow platform docs**
- Full [ServiceNowDocs](https://github.com/ServiceNow/ServiceNowDocs) repo, `australia` branch
- 46k+ markdown files across all product areas
- Refreshed via `corpus/fetch-docs.sh`

**Layer 2 — Fluent SDK reference**
- `llms.txt` from [servicenow.github.io/sdk](https://servicenow.github.io/sdk/llms.txt)
- LLM-optimized index of all Fluent SDK guides and API references
- Gets boosted retrieval weight — the base model has essentially zero Fluent training data

**Layer 3 (engagement-specific)**
- Drop customer docs, runbooks, or schema exports into `corpus/customer-docs/`
- Re-run `rag/ingest.py` to index them
- Now the agent knows their CMDB conventions and governance rules — and none of it left the machine

---

## Audit log

Every query, every retrieval, every generated output is logged to a local SQLite database at `rag/data/audit.db`.

Schema: `id | timestamp | query | retrieved_sources | generated_output | model | profile`

Export for customer review:

```bash
./scripts/audit-export.sh --format json --out audit/session-export.json
./scripts/audit-export.sh --format csv
```

---

## Deployability

### Default: git clone and go

Fresh machine to working agent in ~30 minutes (mostly model download). Run `bootstrap.py`, done.

### Multi-customer: profile-based (Phase 3)

When a second customer engagement appears, the repo grows a `profiles/` directory:

```
profiles/
├── default.yml          # Generic ServiceNow
├── customer-acme.yml    # ACME's corpus path, system prompt variant, audit destination
└── customer-foo.yml
```

```bash
./scripts/start.sh --profile customer-acme
```

Each profile swaps the corpus, system prompt, and audit destination without touching the base config.

---

## Phase plan

| Phase | Status | Goal |
|---|---|---|
| Phase 1 | **Current** | Validate raw Ollama + Aider experience. Gate: TypeScript type-check pass. |
| Phase 2 | Planned | FastAPI RAG service, structured audit log, corpus ingestion pipeline. |
| Phase 3 | Future | Profile-based deployability when customer 2 appears. |

---

## Reference

- [ServiceNow Fluent SDK docs](https://servicenow.github.io/sdk/)
- [ServiceNowDocs repo](https://github.com/ServiceNow/ServiceNowDocs)
- [Ollama](https://ollama.com)
- [Aider](https://aider.chat)
- [sqlite-vec](https://github.com/asg017/sqlite-vec)
- [uv](https://github.com/astral-sh/uv)
