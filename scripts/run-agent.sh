#!/usr/bin/env bash
# Run the agent CLI from the venv (Linux / macOS).
#   bash scripts/run-agent.sh sample --count 10
#   bash scripts/run-agent.sh diag --out diag.json
#   bash scripts/run-agent.sh serve --pair
AGENT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONPATH="$AGENT/src" exec "$AGENT/.venv/bin/python" -m pcdeck_agent "$@"
