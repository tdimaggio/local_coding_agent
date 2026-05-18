#!/usr/bin/env python3
"""
rag/ingest.py — chunk, embed, and index the ServiceNow corpus into sqlite-vec.

Usage:
  uv run python rag/ingest.py [--corpus <path>] [--db <path>] [--reset]

Chunking strategy:
  - llms.txt:          section-level splits on "##" headings
  - *.md (docs):       paragraph-level, 512-token max, 64-token overlap
  - Source priority:   llms.txt chunks get a boosted source tag so the
                       retriever can weight them higher at query time
"""

import argparse
import hashlib
import json
import re
import sqlite3
import struct
import time
from pathlib import Path

import httpx
import sqlite_vec

REPO_DIR = Path(__file__).parent.parent.resolve()
DEFAULT_CORPUS = REPO_DIR / "corpus"
DEFAULT_DB = REPO_DIR / "rag" / "data" / "rag.db"
OLLAMA_API = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768  # nomic-embed-text output dimension

CHUNK_SIZE_TOKENS = 512
CHUNK_OVERLAP_TOKENS = 64
MAX_FILE_TOKENS = 50_000  # skip files larger than this


# ── Tokenisation (approximate, tiktoken cl100k) ───────────────────────────────

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    def token_count(text: str) -> int:
        return len(_enc.encode(text))
    def token_split(text: str, size: int, overlap: int) -> list[str]:
        tokens = _enc.encode(text)
        chunks = []
        i = 0
        while i < len(tokens):
            chunk_tokens = tokens[i:i + size]
            chunks.append(_enc.decode(chunk_tokens))
            i += size - overlap
        return chunks
except ImportError:
    # Fallback: rough word-based split
    def token_count(text: str) -> int:
        return len(text.split()) * 4 // 3
    def token_split(text: str, size: int, overlap: int) -> list[str]:
        words = text.split()
        approx_words = size * 3 // 4
        overlap_words = overlap * 3 // 4
        chunks, i = [], 0
        while i < len(words):
            chunks.append(" ".join(words[i:i + approx_words]))
            i += approx_words - overlap_words
        return chunks


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_llms_txt(text: str, source_path: str) -> list[dict]:
    """Split llms.txt on ## section headings — each section is one chunk."""
    sections = re.split(r'\n(?=## )', text.strip())
    chunks = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        title = section.splitlines()[0].lstrip('#').strip()
        chunks.append({
            "text": section,
            "source": source_path,
            "source_type": "fluent-sdk",  # boosted priority at retrieval
            "title": title,
        })
    return chunks


def chunk_markdown(text: str, source_path: str) -> list[dict]:
    """Paragraph-level chunking with token-size cap and overlap."""
    # Split on double newlines (paragraph boundaries)
    paragraphs = re.split(r'\n\n+', text.strip())

    chunks = []
    buffer = ""
    title = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # Track the most recent heading as the chunk title
        if para.startswith('#'):
            title = para.lstrip('#').strip()

        if token_count(buffer + "\n\n" + para) > CHUNK_SIZE_TOKENS and buffer:
            # Flush current buffer as a chunk
            chunks.append({
                "text": buffer.strip(),
                "source": source_path,
                "source_type": "servicenow-docs",
                "title": title,
            })
            # Carry overlap: last paragraph(s) into next chunk
            overlap_text = para
            buffer = overlap_text
        else:
            buffer = (buffer + "\n\n" + para).strip() if buffer else para

    if buffer:
        chunks.append({
            "text": buffer.strip(),
            "source": source_path,
            "source_type": "servicenow-docs",
            "title": title,
        })

    return chunks


def chunk_file(path: Path, corpus_dir: Path) -> list[dict]:
    """Route a file to the right chunker based on name/type."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"    SKIP (read error): {path.name} — {e}")
        return []

    if token_count(text) > MAX_FILE_TOKENS:
        print(f"    SKIP (too large): {path.name}")
        return []

    rel = str(path.relative_to(corpus_dir))

    if path.name == "llms.txt":
        return chunk_llms_txt(text, rel)
    elif path.suffix == ".md":
        return chunk_markdown(text, rel)
    else:
        return []


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed(texts: list[str]) -> list[list[float]]:
    """Batch embed via Ollama nomic-embed-text."""
    vectors = []
    client = httpx.Client(timeout=60)
    for text in texts:
        resp = client.post(
            f"{OLLAMA_API}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
        )
        resp.raise_for_status()
        vectors.append(resp.json()["embedding"])
    return vectors


def pack_vector(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


# ── Database ──────────────────────────────────────────────────────────────────

def init_db(db_path: Path, reset: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    if reset:
        conn.execute("DROP TABLE IF EXISTS chunks")
        conn.execute("DROP TABLE IF EXISTS chunks_vec")

    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS chunks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            hash        TEXT UNIQUE,
            source      TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'servicenow-docs',
            title       TEXT,
            text        TEXT NOT NULL,
            token_count INTEGER,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            embedding FLOAT[{EMBED_DIM}]
        );
    """)
    conn.commit()
    return conn


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def upsert_chunk(conn: sqlite3.Connection, chunk: dict, vector: list[float]) -> bool:
    """Insert chunk if not already present (by content hash). Returns True if inserted."""
    h = content_hash(chunk["text"])
    existing = conn.execute("SELECT id FROM chunks WHERE hash = ?", (h,)).fetchone()
    if existing:
        return False

    cur = conn.execute(
        """INSERT INTO chunks (hash, source, source_type, title, text, token_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (h, chunk["source"], chunk["source_type"], chunk.get("title", ""),
         chunk["text"], token_count(chunk["text"]))
    )
    chunk_id = cur.lastrowid
    conn.execute(
        "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, pack_vector(vector))
    )
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Ingest corpus into sqlite-vec")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="Path to corpus dir")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to sqlite-vec DB")
    parser.add_argument("--reset", action="store_true", help="Drop and rebuild the DB")
    parser.add_argument("--batch", type=int, default=16, help="Embedding batch size")
    args = parser.parse_args()

    corpus_dir = Path(args.corpus)
    db_path = Path(args.db)

    print(f"Corpus: {corpus_dir}")
    print(f"DB:     {db_path}")
    print(f"Reset:  {args.reset}")
    print()

    # Collect files
    files = list(corpus_dir.rglob("*.md")) + list(corpus_dir.glob("llms.txt"))
    print(f"Found {len(files)} files to process")

    conn = init_db(db_path, reset=args.reset)

    total_chunks = 0
    total_inserted = 0
    t0 = time.time()

    pending_chunks: list[dict] = []

    def flush_batch(batch: list[dict]) -> int:
        if not batch:
            return 0
        texts = [c["text"] for c in batch]
        vectors = embed(texts)
        inserted = 0
        for chunk, vec in zip(batch, vectors):
            if upsert_chunk(conn, chunk, vec):
                inserted += 1
        conn.commit()
        return inserted

    for i, file_path in enumerate(files):
        rel = file_path.relative_to(corpus_dir)
        chunks = chunk_file(file_path, corpus_dir)
        if not chunks:
            continue

        print(f"[{i+1}/{len(files)}] {rel} → {len(chunks)} chunks", end="", flush=True)
        total_chunks += len(chunks)

        pending_chunks.extend(chunks)

        if len(pending_chunks) >= args.batch:
            inserted = flush_batch(pending_chunks)
            total_inserted += inserted
            print(f" ({inserted} new)", flush=True)
            pending_chunks = []
        else:
            print()

    # Flush remainder
    if pending_chunks:
        inserted = flush_batch(pending_chunks)
        total_inserted += inserted

    elapsed = time.time() - t0
    total_in_db = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    print()
    print(f"Done in {elapsed:.1f}s")
    print(f"  Chunks processed : {total_chunks}")
    print(f"  Newly inserted   : {total_inserted}")
    print(f"  Total in DB      : {total_in_db}")
    print(f"  DB path          : {db_path}")


if __name__ == "__main__":
    main()
