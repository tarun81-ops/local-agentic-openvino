# One-time setup: Python venv + backend packages, then the UI packages.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Set-Location "$root\backend"
if (-not (Test-Path ".venv")) { python -m venv .venv }
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
if (Test-Path "requirements-lock.txt") {
  # Exact versions from scripts\lock-backend.ps1, then the dev tools on top.
  & ".venv\Scripts\pip.exe" install -r requirements-lock.txt
}
& ".venv\Scripts\pip.exe" install -r requirements-dev.txt
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
# The search re-ranker, fetched once from huggingface.co; search still works without it.
& ".venv\Scripts\python.exe" -m app.core.search.reranker
if ($LASTEXITCODE -ne 0) { Write-Warning "Re-ranker not downloaded; search runs without it. Re-run setup when online." }

# Voice models (speech recognition + a voice), about 200 MB, fetched once. Voice is optional;
# Settings > Voice can download them later.
& ".venv\Scripts\python.exe" -m app.core.assistant.voice.stt
if ($LASTEXITCODE -ne 0) { Write-Warning "Speech model not downloaded; voice input is off until it is (Settings > Voice)." }
& ".venv\Scripts\python.exe" -m app.core.assistant.voice.tts
if ($LASTEXITCODE -ne 0) { Write-Warning "Voice not downloaded; spoken answers are off until it is (Settings > Voice)." }

Set-Location "$root\ui"
npm install

Write-Host ""
Write-Host "Done. Export the models once and start OpenVINO Model Server:"
Write-Host "  scripts\ovms-export.ps1"
Write-Host "  scripts\ovms-start.ps1"
Write-Host "Then start the app with:  cd ui; npm run dev"
