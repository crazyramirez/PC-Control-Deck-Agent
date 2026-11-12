# Run the agent CLI from the venv.
#   scripts/run-agent.ps1 sample --count 10
#   scripts/run-agent.ps1 diag --out diag.json
$agent = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $agent "src"
& (Join-Path $agent ".venv\Scripts\python.exe") -m pcdeck_agent @args
exit $LASTEXITCODE
