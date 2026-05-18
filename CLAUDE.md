# Local LLM Coding Agent — Claude Code Operating Instructions

## What this project is

A local, privacy-preserving coding agent specialized for ServiceNow AI Agents and the Fluent SDK. Runs entirely on an M4 MacBook Air (24GB). No data leaves the machine. Primary use case: customer POC demos where cloud tools are blocked by enterprise data sensitivity.

GitHub: https://github.com/tdimaggio/local_coding_agent

## Stack (decided)

- **Ollama** — local inference, localhost:11434
- **Main model** — TBD from Phase 1 gate (Qwen2.5-Coder-32B Q4 vs. DeepSeek-Coder-V2-Lite)
- **Embeddings** — nomic-embed-text via Ollama
- **Coding agent** — Aider (`--openai-api-base http://localhost:11434/v1`), Zed as editor
- **RAG service** — FastAPI + sqlite-vec (single file, no daemon)
- **Audit log** — structured SQLite table, schema locked from day 1
- **Package manager** — uv
- **No Docker** — kills Metal GPU passthrough on Mac

## Phase plan

### Phase 1 (current) — Validate before building
Goal: prove the raw Ollama + Aider experience is workable.

1. Pull both candidate models + nomic-embed-text
2. Clone ServiceNowDocs (australia branch, `markdown/application-development/servicenow-sdk`)
3. Grab `llms-full.txt` from the Fluent SDK docs site
4. Write a skeleton system prompt (`config/system-prompt.md`) with ServiceNow scope guardrails
5. Run both models against the same Fluent SDK prompt while `@`-referencing docs
6. **Gate:** can either model produce a Fluent SDK artifact that passes TypeScript type checking? Pick the winner.

### Phase 2 — RAG + audit
- FastAPI RAG service with sqlite-vec
- Structured audit log schema (timestamp, query, retrieved chunks with source, generated output)
- Chunking: AST/function-boundary for code, paragraph-level for narrative docs
- Retrieval boost for `llms-full.txt` chunks (base model has near-zero Fluent SDK training data)
- System prompt iteration

### Phase 3 — Profile-based deployability
- `profiles/` directory when customer 2 appears
- Per-profile: corpus path, system prompt, audit destination
- `./start.sh --profile customer-acme`

## Key architectural decisions

- RAG over fine-tuning — gets 80-90% of the way for far less effort
- Model selection is a Phase 1 output, not a backlog item
- System prompt is load-bearing from Phase 1 — ServiceNow scope handling, `sn_aia_*` conventions, Fluent SDK idioms
- Audit log schema locked from day 1 — retrofitting is painful
- Chunking strategy: split approach (code vs. docs) from day 1
- Everything is files in a git repo — no services, no Docker

## Planned repo structure

```
locall-llm-agent/
├── CLAUDE.md
├── README.md
├── bootstrap.sh           # idempotent setup: checks Ollama, pulls models, sets up env, ingests corpus
├── pyproject.toml
├── config/
│   ├── aider.conf.yml
│   └── system-prompt.md
├── corpus/
│   ├── .gitignore
│   └── fetch-docs.sh
├── rag/
│   ├── server.py
│   ├── ingest.py
│   └── data/              # sqlite-vec DB
└── scripts/
    ├── start.sh
    └── audit-export.sh
```

## Session context

- Tony is comfortable in the terminal, Zed-based, macOS conventions, hands-on tinkerer
- Companion Obsidian note lives at: `TD_Vault/🚀 Projects/👤 Personal/AI/Local LLM Coding Agent.md`
- Superpowers plugin (tdclaw MCP) is installed — use it for document reads, drive file ops, search, and other tdclaw tools when relevant
- Architecture decisions above are current consensus, not locked in
- No Docker, no fine-tuning, no USB delivery scope
