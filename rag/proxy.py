#!/usr/bin/env python3
"""
rag/proxy.py — OpenAI-compatible proxy that injects RAG context into Aider requests.

Sits between Aider and Ollama:
  Aider → proxy (:8766) → [fetch RAG context] → Ollama (:11434)

For every chat completion request, the proxy:
  1. Extracts the last user message as a RAG query
  2. Calls /retrieve on the RAG service
  3. Injects retrieved chunks into the system message
  4. Forwards the augmented request to Ollama
  5. Streams the response back transparently

Usage:
  uv run uvicorn rag.proxy:app --host 127.0.0.1 --port 8766
"""

import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

OLLAMA_BASE = "http://localhost:11434"
RAG_BASE = "http://localhost:8765"
RAG_TOP_K = 5
# Skip RAG for short messages — likely confirmations, file edits, y/n responses
MIN_QUERY_LEN = 40

app = FastAPI(title="RAG Proxy", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def fetch_rag_context(query: str) -> str:
    """Query RAG /retrieve and return a formatted context block, or '' on failure."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{RAG_BASE}/retrieve",
                json={"query": query, "top_k": RAG_TOP_K},
            )
            if resp.status_code != 200:
                return ""
            chunks = resp.json().get("chunks", [])
            if not chunks:
                return ""

        lines = ["## Retrieved Context (ServiceNow RAG)\n"]
        for c in chunks:
            score = c.get("score", 0)
            lines.append(
                f"[{c['source_type']} | score {score:.2f} | {c['title']}]\n{c['text']}"
            )
            lines.append("---")
        return "\n\n".join(lines)

    except Exception as e:
        print(f"  [proxy] RAG fetch failed (non-fatal): {e}")
        return ""


def inject_context(messages: list, context: str) -> list:
    """Prepend RAG context to the system message, or insert one if absent."""
    if not context:
        return messages

    messages = list(messages)  # don't mutate original
    if messages and messages[0]["role"] == "system":
        messages[0] = {
            **messages[0],
            "content": context + "\n\n" + messages[0]["content"],
        }
    else:
        messages.insert(0, {"role": "system", "content": context})
    return messages


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])

    # Extract last user message as RAG query
    user_content = next(
        (m["content"] for m in reversed(messages) if m["role"] == "user"),
        "",
    )

    if len(user_content) >= MIN_QUERY_LEN:
        context = await fetch_rag_context(user_content)
        if context:
            messages = inject_context(messages, context)
            print(f"  [proxy] RAG injected for: {user_content[:60]}...")
        else:
            print(f"  [proxy] No RAG context for: {user_content[:60]}...")
    else:
        print(f"  [proxy] Skipping RAG (short message): {user_content[:40]!r}")

    # Strip litellm provider prefix (e.g. "openai/deepseek-...") before forwarding.
    # Aider/litellm requires the prefix to route; Ollama wants the bare tag.
    model = body.get("model", "")
    if "/" in model:
        body["model"] = model.split("/", 1)[1]

    body = {**body, "messages": messages}
    stream = body.get("stream", False)

    if stream:
        async def generate():
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_BASE}/v1/chat/completions",
                    json=body,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk

        return StreamingResponse(generate(), media_type="text/event-stream")
    else:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{OLLAMA_BASE}/v1/chat/completions",
                json=body,
            )
            return JSONResponse(content=resp.json(), status_code=resp.status_code)


@app.get("/v1/models")
async def list_models():
    """Pass through model list from Ollama so Aider can enumerate models."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{OLLAMA_BASE}/v1/models")
        return JSONResponse(content=resp.json(), status_code=resp.status_code)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "ollama_base": OLLAMA_BASE,
        "rag_base": RAG_BASE,
        "rag_top_k": RAG_TOP_K,
        "min_query_len": MIN_QUERY_LEN,
    }
