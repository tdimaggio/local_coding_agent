# bootstrap.ps1 — Windows PowerShell wrapper around bootstrap.py.
# bootstrap.py handles its own prereq detection / install, so we run with
# system Python here (uv may not exist yet on a fresh machine).
# Usage: .\bootstrap.ps1 [--profile <name>] [--skip-models] [--skip-corpus] [--check] [--yes]

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (Get-Command python -ErrorAction SilentlyContinue) {
    & python "$RepoDir\bootstrap.py" @args
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    & python3 "$RepoDir\bootstrap.py" @args
} else {
    Write-Error "Python not found. Install Python 3.11+ from https://python.org"
    exit 1
}
