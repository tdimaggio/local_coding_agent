#!/usr/bin/env python3
"""
bootstrap.py — interactive installer for the ServiceNow local coding agent.

Detects all prerequisites first, shows a summary table, then:
  - Auto-installs dev tools that live in user-space (uv, aider) without prompting.
  - Prompts before installing system-level tools (Ollama).
  - Refuses to install Python or git — those are system installs the user must
    handle themselves. Prints the correct install command and exits.

After prereqs, pulls models, runs `uv sync`, and fetches the docs corpus.
Cross-platform (macOS, Linux, Windows). Idempotent — safe to re-run.

Usage:
  python bootstrap.py [--profile <name>] [--skip-models] [--skip-corpus]
                      [--check] [--yes]
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO_DIR = Path(__file__).parent.resolve()
OLLAMA_API = "http://localhost:11434"

CANDIDATE_MODELS = [
    "deepseek-coder-v2:16b-lite-instruct-q4_K_M",  # Phase 1 winner — ~11s responses on M4 24GB
]
EMBED_MODEL = "nomic-embed-text"

SN_DOCS_REPO = "https://github.com/ServiceNow/ServiceNowDocs.git"
SN_DOCS_BRANCH = "australia"
SDK_LLMS_URL = "https://servicenow.github.io/sdk/llms.txt"

# aider-chat pulls scipy, which lacks prebuilt wheels for Python 3.14+ as of
# this writing. Pin uv's aider sandbox to 3.12 so install doesn't try to build
# scipy from source (which requires gfortran). Bump when scipy ships 3.14 wheels.
AIDER_PYTHON = "3.12"

LOCAL_BIN = Path.home() / ".local" / "bin"

IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"
IS_WIN = platform.system() == "Windows"


# ── Output helpers ────────────────────────────────────────────────────────────

def banner(msg: str) -> None:
    print(f"\n{'='*60}\n {msg}\n{'='*60}")

def step(msg: str) -> None:
    print(f"\n==> {msg}")

def ok(msg: str) -> None:
    print(f"    {msg}")

def warn(msg: str) -> None:
    print(f"    WARN: {msg}")

def fail(msg: str) -> None:
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(1)

def which(name: str) -> Optional[str]:
    return shutil.which(name)

def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kwargs)

def run_capture(cmd: list[str]) -> Optional[str]:
    """Run and return stripped stdout, or None on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

def add_to_path(p: Path) -> None:
    """Prepend a directory to PATH for this process so subprocess calls find it."""
    sep = os.pathsep
    if str(p) not in os.environ.get("PATH", "").split(sep):
        os.environ["PATH"] = f"{p}{sep}{os.environ.get('PATH', '')}"

def prompt_yn(question: str, default_yes: bool = True, assume_yes: bool = False) -> bool:
    if assume_yes:
        print(f"    {question} [auto-yes]")
        return True
    suffix = "[Y/n]" if default_yes else "[y/N]"
    while True:
        try:
            ans = input(f"    {question} {suffix}: ").strip().lower()
        except EOFError:
            return default_yes
        if not ans:
            return default_yes
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


# ── Prereq detection ──────────────────────────────────────────────────────────

@dataclass
class Prereq:
    name: str
    found: bool
    version: Optional[str]
    blocker: bool          # True: exit if missing (user must install themselves)
    auto_install: bool     # True: install silently after umbrella confirmation
    install_label: str     # short description of install action
    install_fn: Optional[Callable[[bool], None]] = None  # (assume_yes) -> None
    note: str = ""


def detect_python() -> Prereq:
    v = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok_version = sys.version_info >= (3, 11)
    return Prereq(
        name="python",
        found=ok_version,
        version=v if ok_version else f"{v} (need 3.11+)",
        blocker=True,
        auto_install=False,
        install_label="install from https://python.org (need 3.11+)",
        note="" if ok_version else "upgrade required",
    )


def detect_git() -> Prereq:
    out = run_capture(["git", "--version"])
    if out:
        return Prereq(name="git", found=True, version=out.replace("git version ", ""),
                      blocker=True, auto_install=False, install_label="")
    if IS_MAC:
        install_hint = "xcode-select --install   (or https://git-scm.com)"
    elif IS_LINUX:
        install_hint = "apt/dnf/yum install git   (or https://git-scm.com)"
    else:
        install_hint = "https://git-scm.com"
    return Prereq(name="git", found=False, version=None, blocker=True,
                  auto_install=False, install_label=install_hint)


def detect_uv() -> Prereq:
    # Also probe ~/.local/bin in case PATH isn't set up
    if not which("uv") and (LOCAL_BIN / "uv").exists():
        add_to_path(LOCAL_BIN)
    out = run_capture(["uv", "--version"])
    if out:
        return Prereq(name="uv", found=True, version=out.replace("uv ", "").split()[0],
                      blocker=False, auto_install=True, install_label="")
    label = "powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"" if IS_WIN \
            else "curl -LsSf https://astral.sh/uv/install.sh | sh"
    return Prereq(name="uv", found=False, version=None, blocker=False,
                  auto_install=True, install_label=label, install_fn=install_uv)


def detect_aider() -> Prereq:
    # uv-installed tools land in ~/.local/bin or %USERPROFILE%\.local\bin
    if not which("aider") and (LOCAL_BIN / "aider").exists():
        add_to_path(LOCAL_BIN)
    out = run_capture(["aider", "--version"])
    if out:
        return Prereq(name="aider", found=True, version=out.split()[-1],
                      blocker=False, auto_install=True, install_label="")
    return Prereq(
        name="aider", found=False, version=None, blocker=False,
        auto_install=True,
        install_label=f"uv tool install --python {AIDER_PYTHON} aider-chat",
        install_fn=install_aider,
        note=f"pinned to Python {AIDER_PYTHON} (scipy wheel constraint)",
    )


def detect_ollama_cli() -> Prereq:
    out = run_capture(["ollama", "--version"])
    if out:
        # `ollama --version` prints "ollama version is X.Y.Z" or "X.Y.Z"
        version = out.replace("ollama version is ", "").strip()
        return Prereq(name="ollama", found=True, version=version, blocker=False,
                      auto_install=False, install_label="")
    if IS_MAC:
        label = "brew install ollama   (or https://ollama.com)"
    elif IS_LINUX:
        label = "curl -fsSL https://ollama.com/install.sh | sh"
    else:
        label = "https://ollama.com (download Windows installer)"
    return Prereq(name="ollama", found=False, version=None, blocker=False,
                  auto_install=False, install_label=label, install_fn=install_ollama,
                  note="will prompt before installing")


def detect_ollama_running() -> Prereq:
    try:
        urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=3)
        return Prereq(name="ollama-svc", found=True, version=OLLAMA_API,
                      blocker=False, auto_install=False, install_label="")
    except (urllib.error.URLError, OSError):
        if IS_MAC:
            label = "open -a Ollama"
        elif IS_LINUX:
            label = "systemctl --user start ollama   (or: ollama serve)"
        else:
            label = "Start Ollama from the system tray"
        return Prereq(name="ollama-svc", found=False, version=None, blocker=False,
                      auto_install=False, install_label=label, install_fn=start_ollama)


# ── Installers ────────────────────────────────────────────────────────────────

def install_uv(_assume_yes: bool = False) -> None:
    ok("Installing uv...")
    if IS_WIN:
        run(["powershell", "-Command", "irm https://astral.sh/uv/install.ps1 | iex"])
    else:
        # Pipe-from-curl, official installer. Lands in ~/.local/bin.
        subprocess.run(
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            shell=True, check=True,
        )
    add_to_path(LOCAL_BIN)
    if not which("uv"):
        fail("uv installed but not on PATH. Open a new shell and re-run bootstrap.")
    ok(f"uv {run_capture(['uv', '--version'])}")


def install_aider(_assume_yes: bool = False) -> None:
    ok(f"Installing aider (uv tool install --python {AIDER_PYTHON} aider-chat)...")
    run(["uv", "tool", "install", "--python", AIDER_PYTHON, "aider-chat"])
    add_to_path(LOCAL_BIN)
    if not which("aider"):
        fail("aider installed but not on PATH. Open a new shell and re-run bootstrap.")
    ok(f"aider {run_capture(['aider', '--version']) or 'installed'}")


def install_ollama(assume_yes: bool = False) -> None:
    if IS_MAC:
        if which("brew"):
            if assume_yes or prompt_yn("Install Ollama via 'brew install ollama'?"):
                ok("Installing Ollama via Homebrew...")
                run(["brew", "install", "ollama"])
                ok("Ollama installed")
                return
        ok("Homebrew not detected. Download the macOS app from https://ollama.com")
        ok("Then re-run this bootstrap.")
        sys.exit(1)
    if IS_LINUX:
        if assume_yes or prompt_yn("Install Ollama via the official installer (curl | sh)?"):
            ok("Installing Ollama...")
            subprocess.run(
                "curl -fsSL https://ollama.com/install.sh | sh",
                shell=True, check=True,
            )
            ok("Ollama installed")
            return
        sys.exit(1)
    ok("Windows: download installer from https://ollama.com, then re-run bootstrap.")
    sys.exit(1)


def start_ollama(_assume_yes: bool = False) -> None:
    ok("Starting Ollama...")
    if IS_MAC:
        # Prefer the GUI app launch; falls back to `ollama serve` if app isn't installed.
        if subprocess.run(["open", "-a", "Ollama"], capture_output=True).returncode != 0:
            warn("`open -a Ollama` failed — try running `ollama serve` in another shell.")
            sys.exit(1)
    elif IS_LINUX:
        # systemd user service first; if that fails, fall back to backgrounding `ollama serve`.
        r = subprocess.run(["systemctl", "--user", "start", "ollama"], capture_output=True)
        if r.returncode != 0:
            warn("systemd start failed — start Ollama manually with `ollama serve` and re-run.")
            sys.exit(1)
    else:
        warn("Start Ollama from the system tray, then re-run bootstrap.")
        sys.exit(1)

    # Poll for readiness — up to 20s
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=1)
            ok(f"Ollama responding at {OLLAMA_API}")
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    fail("Ollama did not start within 20s. Check `ollama serve` manually.")


# ── Status display ────────────────────────────────────────────────────────────

def print_status_table(prereqs: list[Prereq]) -> None:
    rows = []
    for p in prereqs:
        if p.found:
            status = "OK"
        elif p.blocker:
            status = "MISSING*"
        else:
            status = "MISSING"
        version = p.version or "—"
        note = p.note or (p.install_label if not p.found else "")
        rows.append((p.name, status, version, note))

    name_w = max(len(r[0]) for r in rows)
    stat_w = max(len(r[1]) for r in rows)
    ver_w  = max(len(r[2]) for r in rows)

    header = f"  {'Tool':<{name_w}}  {'Status':<{stat_w}}  {'Version':<{ver_w}}  Notes"
    print(header)
    print("  " + "─" * (len(header) - 2))
    for name, status, version, note in rows:
        print(f"  {name:<{name_w}}  {status:<{stat_w}}  {version:<{ver_w}}  {note}")
    if any(p.blocker and not p.found for p in prereqs):
        print("\n  * blocker — must be installed manually before bootstrap can continue.")


def print_install_plan(to_auto: list[Prereq], to_prompt: list[Prereq],
                       to_start: list[Prereq]) -> None:
    if not (to_auto or to_prompt or to_start):
        return
    print()
    step("Install plan")
    if to_auto:
        ok("Will auto-install (no prompt):")
        for p in to_auto:
            ok(f"  - {p.name:<8}  {p.install_label}")
    if to_prompt:
        ok("Will prompt before installing:")
        for p in to_prompt:
            ok(f"  - {p.name:<8}  {p.install_label}")
    if to_start:
        ok("Will start:")
        for p in to_start:
            ok(f"  - {p.name:<8}  {p.install_label}")


# ── Models / env / corpus (unchanged behavior, just refactored output) ────────

def ollama_models() -> set[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_API}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
        return {m["name"].split(":")[0] for m in data.get("models", [])}
    except Exception:
        return set()


def pull_model(model: str) -> None:
    if which("ollama"):
        run(["ollama", "pull", model])
    else:
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
    step("Step 3/6 — Pulling models (slow on first run — grab a coffee)")
    if skip:
        ok("Skipping (--skip-models)")
        return
    present = ollama_models()
    if EMBED_MODEL in present:
        ok(f"{EMBED_MODEL} already present")
    else:
        ok(f"Pulling {EMBED_MODEL}...")
        pull_model(EMBED_MODEL)
        ok(f"{EMBED_MODEL} done")
    for model in CANDIDATE_MODELS:
        base = model.split(":")[0]
        if base in present:
            ok(f"{model} already present")
        else:
            ok(f"Pulling {model} (~10GB, this will take a while)...")
            pull_model(model)
            ok(f"{model} done")


def setup_python_env() -> None:
    step("Step 4/6 — Setting up Python environment via uv...")
    run(["uv", "sync"], cwd=REPO_DIR)
    ok("Python env ready (.venv/)")


def fetch_llms_txt(corpus_dir: Path) -> None:
    ok(f"Fetching Fluent SDK llms.txt from {SDK_LLMS_URL}...")
    try:
        with urllib.request.urlopen(SDK_LLMS_URL, timeout=30) as r:
            content = r.read()
        (corpus_dir / "llms.txt").write_bytes(content)
        ok("Saved to corpus/llms.txt")
    except Exception as e:
        warn(f"Could not fetch llms.txt: {e}")


def fetch_sn_docs(corpus_dir: Path) -> None:
    sn_dir = corpus_dir / "ServiceNowDocs"
    if sn_dir.exists():
        ok("ServiceNowDocs exists — pulling latest...")
        run(["git", "-C", str(sn_dir), "pull"])
    else:
        ok(f"Cloning ServiceNowDocs ({SN_DOCS_BRANCH} branch)...")
        run(["git", "clone", "--depth", "1", "--branch", SN_DOCS_BRANCH,
             SN_DOCS_REPO, str(sn_dir)])
    ok("ServiceNowDocs ready at corpus/ServiceNowDocs/")


def setup_corpus(skip: bool) -> None:
    step("Step 5/6 — Fetching corpus (Fluent SDK llms.txt + ServiceNowDocs)...")
    if skip:
        ok("Skipping (--skip-corpus)")
        return
    corpus_dir = REPO_DIR / "corpus"
    corpus_dir.mkdir(exist_ok=True)
    fetch_llms_txt(corpus_dir)
    fetch_sn_docs(corpus_dir)
    ok("Corpus ready.")


def setup_rag_index(skip: bool) -> None:
    step("Step 6/6 — Building RAG index (chunk + embed corpus into sqlite-vec)...")
    if skip:
        ok("Skipping (--skip-ingest)")
        return
    rag_db = REPO_DIR / "rag" / "data" / "rag.db"
    if rag_db.exists():
        size_mb = rag_db.stat().st_size / (1024 * 1024)
        ok(f"RAG index already exists at rag/data/rag.db ({size_mb:.1f} MB) — skipping.")
        ok("To rebuild from scratch: uv run python rag/ingest.py --reset")
        return
    ok("This embeds the focused core corpus (~3.4k files). ~15 min on M4.")
    run(["uv", "run", "python", "rag/ingest.py"], cwd=REPO_DIR)
    ok("RAG index built at rag/data/rag.db")


# ── Main ──────────────────────────────────────────────────────────────────────

def detect_all() -> list[Prereq]:
    return [
        detect_python(),
        detect_git(),
        detect_uv(),
        detect_aider(),
        detect_ollama_cli(),
        detect_ollama_running(),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="ServiceNow local agent bootstrap")
    parser.add_argument("--profile", default="default", help="Profile name")
    parser.add_argument("--skip-models", action="store_true", help="Skip Ollama model pulls")
    parser.add_argument("--skip-corpus", action="store_true", help="Skip corpus fetch")
    parser.add_argument("--skip-ingest", action="store_true",
                        help="Skip building the RAG index (skip the ~15 min embedding step)")
    parser.add_argument("--check", action="store_true",
                        help="Detect prereqs and show summary, then exit")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Auto-confirm all prompts (non-interactive)")
    args = parser.parse_args()

    banner(f"ServiceNow Local Agent — Bootstrap  (profile: {args.profile})")
    print(f"  OS:   {platform.system()} {platform.release()}")
    print(f"  Arch: {platform.machine()}")

    step("Step 1/6 — Detecting prerequisites...")
    prereqs = detect_all()
    print()
    print_status_table(prereqs)

    blockers = [p for p in prereqs if p.blocker and not p.found]
    if blockers:
        print()
        for p in blockers:
            print(f"  {p.name}: {p.install_label}")
        fail("Required tools missing. Install the ones marked above and re-run bootstrap.")

    to_auto    = [p for p in prereqs if not p.found and p.auto_install and p.install_fn]
    to_prompt  = [p for p in prereqs if not p.found and not p.auto_install
                  and p.install_fn and p.name != "ollama-svc"]
    to_start   = [p for p in prereqs if not p.found and p.name == "ollama-svc"
                  and p.install_fn]

    if args.check:
        print_install_plan(to_auto, to_prompt, to_start)
        sys.exit(0)

    if to_auto or to_prompt or to_start:
        step("Step 2/6 — Installing missing prerequisites")
        print_install_plan(to_auto, to_prompt, to_start)
        print()
        if not prompt_yn("Proceed with installation?", assume_yes=args.yes):
            fail("Aborted.")

        for p in to_auto:
            p.install_fn(args.yes)
        for p in to_prompt:
            p.install_fn(args.yes)

        # If ollama was just installed but not running, plan a start
        if any(p.name == "ollama" for p in to_prompt):
            running = detect_ollama_running()
            if not running.found and running.install_fn:
                running.install_fn(args.yes)
        else:
            for p in to_start:
                p.install_fn(args.yes)

        # Re-detect and fail fast if anything is still off
        post = detect_all()
        still_missing = [p for p in post if not p.found]
        if still_missing:
            print()
            print_status_table(post)
            fail("Some prereqs are still missing after install. See table above.")

    setup_models(args.skip_models)
    setup_python_env()
    setup_corpus(args.skip_corpus)
    setup_rag_index(args.skip_ingest)

    banner("Bootstrap complete!")
    print("""
  Next steps:
    1. (Optional) Add ServiceNow creds for live schema validation:
         cp .env.example .env  &&  edit .env

    2. Start the agent:
         ./scripts/start.sh
""")


if __name__ == "__main__":
    main()
