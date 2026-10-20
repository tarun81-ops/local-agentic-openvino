#!/usr/bin/env bash
# One-time setup on Linux: Python venv + backend packages, then the UI packages.
# The Linux twin of scripts/setup.ps1. Run from anywhere:  bash scripts/setup.sh
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$root/backend"
PY="${PYTHON:-python3}"
# onnxruntime and the other wheels need Python 3.11 or newer (Ubuntu 24.04 ships 3.12; 22.04 only 3.10).
if ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo "Python 3.11+ is needed, found $("$PY" --version 2>&1). Install python3.12 and run: PYTHON=python3.12 bash scripts/setup.sh" >&2
  exit 1
fi
if [ ! -d .venv ]; then "$PY" -m venv .venv; fi
.venv/bin/python -m pip install --upgrade pip
# requirements-lock.txt is pinned on Windows, so Linux installs from the ranges in requirements*.txt.
.venv/bin/pip install -r requirements-dev.txt
if [ ! -f .env ]; then cp .env.example .env; fi

# The search re-ranker, fetched once from huggingface.co; search still works without it.
.venv/bin/python -m app.core.search.reranker || echo "WARNING: re-ranker not downloaded; search runs without it. Re-run setup when online."
# Voice models (about 200 MB), optional; Settings > Voice can download them later.
.venv/bin/python -m app.core.assistant.voice.stt || echo "WARNING: speech model not downloaded; voice input is off until it is (Settings > Voice)."
.venv/bin/python -m app.core.assistant.voice.tts || echo "WARNING: voice not downloaded; spoken answers are off until it is (Settings > Voice)."

cd "$root/ui"
npm install

echo
echo "Done. Export the models and start OpenVINO Model Server (docs/OPENVINO.md)."
echo "Then start the app with:  cd ui && npm run dev"
