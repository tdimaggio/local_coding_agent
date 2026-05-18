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
    """
    Split llms.txt into one chunk per guide/API entry.
    Each bullet line like '- [business-rule-guide](...): description' becomes its own chunk,
    grouped with its parent section heading for context.
    Falls back to section-level splitting for non-list content.
    """
    chunks = []
    current_section = ""
    buffer_lines: list[str] = []

    def flush_entry(lines: list[str], section: str) -> None:
        text_block = "\n".join(lines).strip()
        if not text_block:
            return
        # Extract title from first line (markdown link label)
        title_match = re.search(r'\[([^\]]+)\]', lines[0]) if lines else None
        title = title_match.group(1) if title_match else section
        chunks.append({
            "text": f"[{section}]\n{text_block}",
            "source": source_path,
            "source_type": "fluent-sdk",
            "title": title,
        })

    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("# "):
            if buffer_lines:
                flush_entry(buffer_lines, current_section)
                buffer_lines = []
            current_section = line.lstrip("#").strip()
        elif line.startswith("- ") and buffer_lines:
            # Start of a new entry within a section — flush previous
            flush_entry(buffer_lines, current_section)
            buffer_lines = [line]
        elif line.startswith("- "):
            buffer_lines = [line]
        elif buffer_lines:
            buffer_lines.append(line)
        else:
            # Preamble before first section
            if line.strip():
                buffer_lines.append(line)

    if buffer_lines:
        flush_entry(buffer_lines, current_section)

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

def embed(texts: list[str]) -> list[list[float] | None]:
    """Batch embed via Ollama nomic-embed-text. Returns None for failed chunks."""
    vectors = []
    client = httpx.Client(timeout=60)
    for text in texts:
        # Truncate to ~8000 chars if enormous (nomic has a token limit)
        truncated = text[:8000] if len(text) > 8000 else text
        success = False
        for attempt in range(3):
            try:
                resp = client.post(
                    f"{OLLAMA_API}/api/embeddings",
                    json={"model": EMBED_MODEL, "prompt": truncated},
                )
                resp.raise_for_status()
                vectors.append(resp.json()["embedding"])
                success = True
                break
            except Exception as e:
                if attempt == 2:
                    print(f"\n    WARN: embed failed after 3 attempts — skipping chunk ({e})")
                    vectors.append(None)
                else:
                    time.sleep(1)
        if not success and len(vectors) < len(texts):
            vectors.append(None)
    return vectors


def normalize(v: list[float]) -> list[float]:
    """L2-normalize a vector to unit length for cosine similarity via L2 distance."""
    import math
    mag = math.sqrt(sum(x * x for x in v))
    return [x / mag for x in v] if mag > 0 else v

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
    vector = normalize(vector)
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

# Focused core directories — everything else available on-demand
CORE_DIRS = [
    "application-development",
    "build-workflows",
    "now-intelligence",
]


def collect_files(corpus_dir: Path, dirs: list[str] | None = None) -> list[Path]:
    """Collect files to ingest. If dirs specified, only those subdirs of ServiceNowDocs/markdown/."""
    files: list[Path] = []

    # Always include llms.txt at corpus root
    llms = corpus_dir / "llms.txt"
    if llms.exists():
        files.append(llms)

    sn_docs = corpus_dir / "ServiceNowDocs" / "markdown"

    if dirs:
        for d in dirs:
            target = sn_docs / d
            if target.exists():
                files.extend(target.rglob("*.md"))
            else:
                print(f"  WARN: directory not found: {target}")
    else:
        if sn_docs.exists():
            files.extend(sn_docs.rglob("*.md"))

    return files


def ingest_files(files: list[Path], corpus_dir: Path, db_path: Path,
                 reset: bool = False, batch_size: int = 16) -> dict:
    """Embed and store a list of files. Returns stats dict."""
    conn = init_db(db_path, reset=reset)
    total_chunks = total_inserted = 0
    t0 = time.time()
    pending: list[dict] = []

    def flush(batch: list[dict]) -> int:
        if not batch:
            return 0
        vectors = embed([c["text"] for c in batch])
        n = 0
        for chunk, vec in zip(batch, vectors):
            if vec is not None and upsert_chunk(conn, chunk, vec):
                n += 1
        conn.commit()
        return n

    for i, file_path in enumerate(files):
        chunks = chunk_file(file_path, corpus_dir)
        if not chunks:
            continue
        rel = file_path.relative_to(corpus_dir)
        print(f"[{i+1}/{len(files)}] {rel} → {len(chunks)} chunks", end="", flush=True)
        total_chunks += len(chunks)
        pending.extend(chunks)
        if len(pending) >= batch_size:
            n = flush(pending)
            total_inserted += n
            print(f" ({n} new)", flush=True)
            pending = []
        else:
            print()

    if pending:
        total_inserted += flush(pending)

    elapsed = time.time() - t0
    total_in_db = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    conn.close()
    return {
        "files": len(files),
        "chunks": total_chunks,
        "inserted": total_inserted,
        "total_in_db": total_in_db,
        "elapsed_s": round(elapsed, 1),
    }


def main():
    parser = argparse.ArgumentParser(description="Ingest corpus into sqlite-vec")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="Path to corpus dir")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to sqlite-vec DB")
    parser.add_argument("--reset", action="store_true", help="Drop and rebuild the DB")
    parser.add_argument("--batch", type=int, default=16, help="Embedding batch size")
    parser.add_argument(
        "--dirs", nargs="*", default=None,
        help=f"Subdirs of ServiceNowDocs/markdown/ to ingest (default: {CORE_DIRS}). "
             "Pass --dirs with no args to ingest ALL directories."
    )
    args = parser.parse_args()

    corpus_dir = Path(args.corpus)
    db_path = Path(args.db)

    # --dirs with no values means all; omitted means CORE_DIRS
    if args.dirs is None:
        dirs = CORE_DIRS
    elif len(args.dirs) == 0:
        dirs = None  # all
    else:
        dirs = args.dirs

    print(f"Corpus: {corpus_dir}")
    print(f"DB:     {db_path}")
    print(f"Reset:  {args.reset}")
    print(f"Dirs:   {dirs or 'ALL'}")
    print()

    files = collect_files(corpus_dir, dirs)
    print(f"Found {len(files)} files to process\n")

    stats = ingest_files(files, corpus_dir, db_path, reset=args.reset, batch_size=args.batch)

    print()
    print(f"Done in {stats['elapsed_s']}s")
    print(f"  Files processed  : {stats['files']}")
    print(f"  Chunks processed : {stats['chunks']}")
    print(f"  Newly inserted   : {stats['inserted']}")
    print(f"  Total in DB      : {stats['total_in_db']}")
    print(f"  DB path          : {db_path}")


if __name__ == "__main__":
    main()
