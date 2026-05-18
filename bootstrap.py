#!/usr/bin/env python3
"""
bootstrap.py — cross-platform setup for the ServiceNow local coding agent.
Works on macOS, Linux, and Windows. Requires: Python 3.11+, uv, git, Ollama.

Usage:
  python bootstrap.py [--profile <name>] [--skip-models] [--skip-corpus]
"""

import argparse
import json
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_DIR = Path(__file__).parent.resolve()
OLLAMA_API = "http://localhost:11434"

CANDIDATE_MODELS = [
    "deepseek-coder-v2:16b-lite-instruct-q4_K_M",  # Phase 1 winner — ~11s responses on M4 24GB
]
EMBED_MODEL = "nomic-embed-text"

SN_DOCS_REPO = "https://github.com/ServiceNow/ServiceNowDocs.git"
SN_DOCS_BRANCH = "australia"
SDK_LLMS_URL = "https://servicenow.github.io/sdk/llms.txt"


# ── Helpers ───────────────────────────────────────────────────────────────────

def banner(msg: str) -> None:
    print(f"\n{'='*48}\n {msg}\n{'='*48}")

def step(msg: str) -> None:
    print(f"\n==> {msg}")

def ok(msg: str) -> None:
    print(f"    {msg}")

def fail(msg: str) -> None:
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(1)

def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kwargs)

def which(name: str) -> bool:
    return shutil.which(name) is not None


# ── Checks ────────────────────────────────────────────────────────────────────

def check_python() -> None:
    if sys.version_info < (3, 11):
        fail(f"Python 3.11+ required, found {sys.version}. Install from https://python.org")
    ok(f"Python {sys.version.split()[0]}")

def check_uv() -> None:
    if not which("uv"):
        fail("uv not found. Install from https://github.com/astral-sh/uv\n"
             "    macOS/Linux: curl -LsSf https://astral.sh/uv/install.sh | sh\n"
             "    Windows:     powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"")
    result = subprocess.run(["uv", "--version"], capture_output=True, text=True)
    ok(f"uv {result.stdout.strip()}")

def check_git() -> None:
    if not which("git"):
        fail("git not found. Install from https://git-scm.com")
    result = subprocess.run(["git", "--version"], capture_output=True, text=True)
    ok(result.stdout.strip())

def check_ollama() -> None:
    # Try HTTP first (works even if CLI isn't in PATH on Windows)
    try:
        urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=3)
        ok(f"Ollama running at {OLLAMA_API}")
        return
    except (urllib.error.URLError, OSError):
        pass

    # CLI fallback
    if which("ollama"):
        fail("Ollama is installed but not running.\n"
             "    macOS/Linux: ollama serve\n"
             "    Windows:     Start the Ollama app from the system tray")
    else:
        os_hint = {
            "Darwin":  "https://ollama.com (download the macOS app)",
            "Linux":   "curl -fsSL https://ollama.com/install.sh | sh",
            "Windows": "https://ollama.com (download the Windows installer)",
        }.get(platform.system(), "https://ollama.com")
        fail(f"Ollama not found. Install from: {os_hint}")


# ── Models ────────────────────────────────────────────────────────────────────

def ollama_models() -> set[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
        return {m["name"].split(":")[0] for m in data.get("models", [])}
    except Exception:
        return set()

def pull_model(model: str) -> None:
    # Use CLI if available, otherwise REST API
    if which("ollama"):
        run(["ollama", "pull", model])
    else:
        # REST API pull (streams JSON lines)
        req = urllib.request.Request(
            f"{OLLAMA_API}/api/pull",
            data=json.dumps({"name": model}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            for line in r:
                status = json.loads(line).get("status", "")
                if status:
                    print(f"\r    {status:<60}", end="", flush=True)
        print()

def setup_models(skip: bool) -> None:
    if skip:
        ok("Skipping model pulls (--skip-models)")
        return

    step("Pulling models (slow on first run — grab a coffee)")
    present = ollama_models()

    # Embeddings first — small
    if "nomic-embed-text" in present:
        ok(f"{EMBED_MODEL} already present")
    else:
        ok(f"Pulling {EMBED_MODEL}...")
        pull_model(EMBED_MODEL)
        ok(f"{EMBED_MODEL} done")

    # Candidate models
    for model in CANDIDATE_MODELS:
        base = model.split(":")[0]
        if base in present:
            ok(f"{model} already present")
        else:
            ok(f"Pulling {model} (~20GB, this will take a while)...")
            pull_model(model)
            ok(f"{model} done")


# ── Python env ────────────────────────────────────────────────────────────────

def setup_python_env() -> None:
    step("Setting up Python environment via uv...")
    run(["uv", "sync"], cwd=REPO_DIR)
    ok("Python env ready (.venv/)")


# ── Corpus ────────────────────────────────────────────────────────────────────

def fetch_llms_txt(corpus_dir: Path) -> None:
    ok(f"Fetching Fluent SDK llms.txt from {SDK_LLMS_URL}...")
    try:
        with urllib.request.urlopen(SDK_LLMS_URL, timeout=30) as r:
            content = r.read()
        (corpus_dir / "llms.txt").write_bytes(content)
        ok("Saved to corpus/llms.txt")
    except Exception as e:
        print(f"    WARNING: Could not fetch llms.txt: {e}")

def fetch_sn_docs(corpus_dir: Path) -> None:
    sn_dir = corpus_dir / "ServiceNowDocs"
    if sn_dir.exists():
        ok("ServiceNowDocs exists — pulling latest...")
        run(["git", "-C", str(sn_dir), "pull"])
    else:
        ok(f"Cloning ServiceNowDocs ({SN_DOCS_BRANCH} branch)...")
        run([
            "git", "clone",
            "--depth", "1",
            "--branch", SN_DOCS_BRANCH,
            SN_DOCS_REPO,
            str(sn_dir),
        ])
    ok(f"ServiceNowDocs ready at corpus/ServiceNowDocs/")

def setup_corpus(skip: bool) -> None:
    if skip:
        ok("Skipping corpus fetch (--skip-corpus)")
        return

    step("Fetching corpus...")
    corpus_dir = REPO_DIR / "corpus"
    corpus_dir.mkdir(exist_ok=True)
    fetch_llms_txt(corpus_dir)
    fetch_sn_docs(corpus_dir)
    ok("Corpus ready. Next: uv run python rag/ingest.py")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="ServiceNow local agent bootstrap")
    parser.add_argument("--profile", default="default", help="Profile name")
    parser.add_argument("--skip-models", action="store_true", help="Skip Ollama model pulls")
    parser.add_argument("--skip-corpus", action="store_true", help="Skip corpus fetch")
    args = parser.parse_args()

    banner(f"ServiceNow Local Agent — Bootstrap  (profile: {args.profile})")
    print(f"  OS:   {platform.system()} {platform.release()}")
    print(f"  Arch: {platform.machine()}")

    step("Checking prerequisites...")
    check_python()
    check_uv()
    check_git()
    check_ollama()

    setup_models(args.skip_models)
    setup_python_env()
    setup_corpus(args.skip_corpus)

    banner("Bootstrap complete!")
    print("""
  Next steps:
    1. Run the Phase 1 model gate:
         Mac/Linux:  ./scripts/phase1-test.sh
         Windows:    python scripts/phase1_test.py  (coming soon)

    2. Pick the winner, update config/aider.conf.yml → model: <winner>

    3. Ingest corpus:
         uv run python rag/ingest.py

    4. Start the agent:
         Mac/Linux:  ./scripts/start.sh
         Windows:    uv run python scripts/start.py  (coming soon)
""")

if __name__ == "__main__":
    main()
