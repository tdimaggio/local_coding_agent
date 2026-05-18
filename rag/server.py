#!/usr/bin/env python3
"""
rag/server.py — FastAPI RAG service for the ServiceNow local coding agent.

Endpoints:
  POST /retrieve   — embed query, retrieve top-k chunks, return with metadata
  POST /generate   — retrieve + send augmented prompt to Ollama, log to audit DB
  GET  /health     — liveness check

Usage:
  uv run uvicorn rag.server:app --host 127.0.0.1 --port 8765 --reload
"""

import json
import sqlite3
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import sqlite_vec
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from rag.sn_schema import build_schema_context, is_configured as sn_configured

REPO_DIR = Path(__file__).parent.parent.resolve()
DEFAULT_DB = REPO_DIR / "rag" / "data" / "rag.db"
AUDIT_DB = REPO_DIR / "rag" / "data" / "audit.db"
OLLAMA_API = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
MAIN_MODEL = "deepseek-coder-v2:16b-lite-instruct-q4_K_M"
EMBED_DIM = 768

# Source type retrieval boost — llms.txt chunks rank higher
SOURCE_BOOST = {
    "fluent-sdk": 1.3,
    "servicenow-docs": 1.0,
}

app = FastAPI(title="ServiceNow Local Agent — RAG Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_rag_conn() -> sqlite3.Connection:
    if not DEFAULT_DB.exists():
        raise HTTPException(
            status_code=503,
            detail=f"RAG DB not found at {DEFAULT_DB}. Run: uv run python rag/ingest.py"
        )
    conn = sqlite3.connect(DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def get_audit_conn() -> sqlite3.Connection:
    AUDIT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(AUDIT_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp         TEXT NOT NULL,
            query             TEXT NOT NULL,
            retrieved_sources TEXT,
            generated_output  TEXT,
            model             TEXT,
            profile           TEXT DEFAULT 'default',
            latency_ms        INTEGER
        );
    """)
    conn.commit()
    return conn


def pack_vector(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed_query(query: str) -> list[float]:
    resp = httpx.post(
        f"{OLLAMA_API}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": query},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


# ── Schemas ───────────────────────────────────────────────────────────────────

class RetrieveRequest(BaseModel):
    query: str
    top_k: int = 5
    source_type: Optional[str] = None  # filter to "fluent-sdk" or "servicenow-docs"

class RetrieveResponse(BaseModel):
    query: str
    chunks: list[dict]
    latency_ms: int

class GenerateRequest(BaseModel):
    query: str
    top_k: int = 5
    model: Optional[str] = None
    profile: str = "default"
    stream: bool = False

class GenerateResponse(BaseModel):
    query: str
    context_chunks: list[dict]
    response: str
    model: str
    latency_ms: int


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    db_ok = DEFAULT_DB.exists()
    return {
        "status": "ok" if db_ok else "degraded",
        "rag_db": str(DEFAULT_DB),
        "rag_db_exists": db_ok,
        "ollama_api": OLLAMA_API,
        "embed_model": EMBED_MODEL,
        "main_model": MAIN_MODEL,
        "sn_schema_validation": "enabled" if sn_configured() else "disabled (no .env credentials)",
    }


@app.post("/retrieve", response_model=RetrieveResponse)
def retrieve(req: RetrieveRequest):
    t0 = time.time()

    vec = embed_query(req.query)
    packed = pack_vector(vec)

    conn = get_rag_conn()

    # Vector similarity search via sqlite-vec
    source_filter = ""
    params: list = [packed, req.top_k * 3]  # over-fetch for boosting
    if req.source_type:
        source_filter = "AND c.source_type = ?"
        params.append(req.source_type)

    rows = conn.execute(f"""
        SELECT
            c.id,
            c.source,
            c.source_type,
            c.title,
            c.text,
            c.token_count,
            v.distance
        FROM chunks_vec v
        JOIN chunks c ON c.id = v.chunk_id
        WHERE v.embedding MATCH ?
          AND k = ?
          {source_filter}
        ORDER BY v.distance
    """, params).fetchall()

    # Apply source-type boost and re-rank
    boosted = []
    for row in rows:
        boost = SOURCE_BOOST.get(row["source_type"], 1.0)
        boosted.append({
            "id": row["id"],
            "source": row["source"],
            "source_type": row["source_type"],
            "title": row["title"],
            "text": row["text"],
            "token_count": row["token_count"],
            "distance": row["distance"],
            "score": (1 - row["distance"]) * boost,
        })

    boosted.sort(key=lambda x: x["score"], reverse=True)
    results = boosted[:req.top_k]

    return RetrieveResponse(
        query=req.query,
        chunks=results,
        latency_ms=int((time.time() - t0) * 1000),
    )


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    t0 = time.time()
    model = req.model or MAIN_MODEL

    # Retrieve relevant chunks
    retrieved = retrieve(RetrieveRequest(query=req.query, top_k=req.top_k))
    context_chunks = retrieved.chunks

    # Build augmented prompt
    context_text = "\n\n---\n\n".join(
        f"[Source: {c['source']} | {c['source_type']}]\n{c['text']}"
        for c in context_chunks
    )

    system_prompt_path = REPO_DIR / "config" / "system-prompt.md"
    system_prompt = system_prompt_path.read_text() if system_prompt_path.exists() else ""

    # Pre-generation: inject live schema from connected ServiceNow instance
    schema_context = build_schema_context(req.query)

    augmented_prompt = f"""{system_prompt}

## Relevant documentation

{context_text}
{f'''
{schema_context}''' if schema_context else ""}
---

## Task

{req.query}"""

    # Call Ollama
    resp = httpx.post(
        f"{OLLAMA_API}/api/generate",
        json={
            "model": model,
            "prompt": augmented_prompt,
            "stream": False,
        },
        timeout=300,
    )
    resp.raise_for_status()
    response_text = resp.json()["response"]
    latency_ms = int((time.time() - t0) * 1000)

    # Audit log
    try:
        audit = get_audit_conn()
        audit.execute(
            """INSERT INTO audit_log
               (timestamp, query, retrieved_sources, generated_output, model, profile, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.utcnow().isoformat(),
                req.query,
                json.dumps([c["source"] for c in context_chunks]),
                response_text,
                model,
                req.profile,
                latency_ms,
            )
        )
        audit.commit()
    except Exception as e:
        print(f"Audit log error (non-fatal): {e}")

    return GenerateResponse(
        query=req.query,
        context_chunks=context_chunks,
        response=response_text,
        model=model,
        latency_ms=latency_ms,
    )
