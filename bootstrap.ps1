# bootstrap.ps1 — Windows PowerShell convenience wrapper around bootstrap.py
# Run from the repo root: .\bootstrap.ps1 [--profile <name>] [--skip-models] [--skip-corpus]

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Prefer uv-managed Python, fall back to system Python
if (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv run python "$RepoDir\bootstrap.py" @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python "$RepoDir\bootstrap.py" @args
} else {
    Write-Error "Python not found. Install from https://python.org (3.11+)"
    exit 1
}
