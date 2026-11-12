# One-time agent setup: venv with pinned dependencies + verified LibreHardwareMonitor DLLs.
#   scripts/setup-agent.ps1
$ErrorActionPreference = "Stop"
$agent = Split-Path -Parent $PSScriptRoot
Push-Location $agent
try {
    if (-not (Test-Path .venv)) { python -m venv .venv }
    .\.venv\Scripts\python.exe -m pip install --disable-pip-version-check -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    .\.venv\Scripts\python.exe tools\fetch_lhm.py
    if ($LASTEXITCODE -ne 0) { throw "LibreHardwareMonitor download/verification failed" }
    .\.venv\Scripts\python.exe -m pytest -q
} finally {
    Pop-Location
}
