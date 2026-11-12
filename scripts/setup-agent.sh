#!/usr/bin/env bash
# One-time agent setup on Linux / macOS: venv with the pinned dependencies, then the tests.
#   bash scripts/setup-agent.sh          (needs python3.13 on PATH, or PYTHON=/path/to/python3.13)
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then
    "${PYTHON:-python3.13}" -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -e ".[dev]"
PYTHONPATH=src QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
