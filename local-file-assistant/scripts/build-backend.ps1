# Freezes the Python backend into backend\dist\lfa-backend\lfa-backend.exe with PyInstaller,
# checks that no secrets were bundled, then starts the frozen exe once to prove it runs.
# electron-builder then ships that folder inside the installer (see ui\package.json extraResources).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location "$root\backend"
$py = ".venv\Scripts\python.exe"

$extra = @()
# OCR is optional; bundle its models only when it's installed.
$ErrorActionPreference = "Continue"
& $py -c "import rapidocr_onnxruntime" 2>$null | Out-Null
$hasOcr = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if ($hasOcr) { $extra += @("--collect-all", "rapidocr_onnxruntime") }

& ".venv\Scripts\pyinstaller.exe" run.py `
  --name lfa-backend `
  --noconfirm --clean --onedir --console `
  --collect-all onnxruntime `
  --collect-all tokenizers `
  --collect-all lancedb `
  --collect-all pyarrow `
  --collect-all pymupdf `
  --add-data "app\db\migrations;app\db\migrations" `
  --collect-all dateparser `
  --collect-all tzlocal `
  --collect-all tzdata `
  --collect-all faster_whisper `
  --collect-all ctranslate2 `
  --collect-all piper `
  --collect-all av `
  --collect-submodules uvicorn `
  --collect-submodules app `
  --collect-submodules send2trash `
  --collect-submodules watchdog `
  @extra
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$dist = "$root\backend\dist\lfa-backend"
$exe = "$dist\lfa-backend.exe"

# The search re-ranker ships next to the exe (config.py looks there when frozen), so the
# installed app never downloads anything. Missing locally = the build ships without it.
$model = & $py -c "from app.config import settings; from app.core.search import reranker; print(reranker.model_dir(settings.rerank_model) if reranker.available() else '')"
if ($model) {
  Copy-Item $model -Destination "$dist\models\$(Split-Path $model -Leaf)" -Recurse -Force
} else {
  Write-Warning "No re-ranker model found; run scripts\setup.ps1 first to include it."
}

# OpenVINO build: the installer ships OpenVINO Model Server too (ui\package.json extraResources -> resources\ovms), so
# nobody installs it separately. The packaged app downloads missing models automatically on first launch. OVMS_DIR
# picks another copy.
$ovmsSrc = if ($env:OVMS_DIR) { $env:OVMS_DIR } else { Join-Path $env:LOCALAPPDATA "ovms" }
if (-not (Test-Path (Join-Path $ovmsSrc "ovms.exe"))) { throw "ovms.exe not found in $ovmsSrc. Unzip the OVMS release there (docs\OPENVINO.md) or set OVMS_DIR." }
$ovmsDst = "$root\backend\dist\ovms"
if (Test-Path $ovmsDst) { Remove-Item $ovmsDst -Recurse -Force }
robocopy $ovmsSrc $ovmsDst /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying OVMS failed (robocopy $LASTEXITCODE)" }
$LASTEXITCODE = 0
Write-Host ("OVMS bundled: {0:N0} MB" -f ((Get-ChildItem $ovmsDst -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))

# 1. Secrets must never ship: .env holds the local API token and settings.
$leaked = Get-ChildItem $dist -Recurse -Force -Include ".env", "api_token.txt", "*.db"
if ($leaked) { throw "Refusing to ship secrets/data: $($leaked.FullName -join ', ')" }

# 2. Smoke test: the frozen exe starts, answers /health, and rejects a missing token.
$port = 18756
$tmp = Join-Path $env:TEMP "lfa-smoke-$(Get-Random)"
$env:PORT = "$port"; $env:API_TOKEN = "smoke-token"; $env:DATA_DIR = $tmp
$env:DB_PATH = "$tmp\index.db"; $env:VECTOR_DB_DIR = "$tmp\lancedb"; $env:ASSISTANT_DB_PATH = "$tmp\assistant.db"; $env:VOICE_MODELS_DIR = "$tmp\models"
$proc = Start-Process $exe -WorkingDirectory $dist -PassThru -WindowStyle Hidden
try {
  $ok = $false
  for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    try { if ((Invoke-RestMethod "http://127.0.0.1:$port/health").ok) { $ok = $true; break } } catch {}
  }
  if (-not $ok) { throw "Frozen backend did not answer /health within 30 s" }
  $headers = @{ Authorization = "Bearer smoke-token" }
  $s = Invoke-RestMethod "http://127.0.0.1:$port/settings" -Headers $headers
  Write-Host "Smoke test OK. File types: $($s.file_types -join ', '). Re-ranker: $(if ($s.reranker) { $s.reranker } else { 'none' })"
  # The assistant features: migrations were bundled (the tables exist), dates parse (dateparser data),
  # the voice libraries import, and the new routes answer.
  $json = @{ "Content-Type" = "application/json" }
  $null = Invoke-RestMethod "http://127.0.0.1:$port/conversations" -Headers $headers
  $null = Invoke-RestMethod "http://127.0.0.1:$port/memory" -Headers $headers
  $null = Invoke-RestMethod "http://127.0.0.1:$port/tasks" -Headers $headers
  # Personalization: the new router answers with defaults, and migration 0007 (collections, memory status) ran.
  $pz = Invoke-RestMethod "http://127.0.0.1:$port/personalize" -Headers $headers
  if ($pz.answer_style -ne "concise" -or $pz.appearance.theme -ne "system" -or $pz.perf_profile -ne "balanced") { throw "Personalization defaults are wrong in the frozen backend" }
  $null = Invoke-RestMethod "http://127.0.0.1:$port/collections" -Headers $headers
  $null = Invoke-RestMethod "http://127.0.0.1:$port/memory?status=pending" -Headers $headers
  $null = Invoke-RestMethod "http://127.0.0.1:$port/personalize/performance" -Headers $headers
  $parsed = Invoke-RestMethod "http://127.0.0.1:$port/tasks/parse" -Method Post -Headers ($headers + $json) -Body '{"text":"remind me to call mom tomorrow at 5pm"}'
  if (-not $parsed.when -or $parsed.title -ne "Call mom") { throw "Date parsing failed in the frozen backend" }
  $voice = Invoke-RestMethod "http://127.0.0.1:$port/voice/status" -Headers $headers
  Write-Host "Assistant smoke test OK (conversations, memory, tasks, dates). Voice models present: stt=$($voice.stt.available) tts=$($voice.tts.available)"
  # First-run setup answers (routes_setup.py was bundled) and says what a fresh PC is missing.
  $setup = Invoke-RestMethod "http://127.0.0.1:$port/setup/status" -Headers $headers
  if ($null -eq $setup.missing -or $setup.done) { throw "/setup/status is wrong in the frozen backend" }
  if ($setup.engine.state -eq "missing") { throw "The frozen backend did not find the bundled OVMS (backend\dist\ovms)" }
  Write-Host "Setup status OK. Engine: $($setup.engine.state). Missing: $($setup.missing -join ', ')"
  try { Invoke-RestMethod "http://127.0.0.1:$port/settings" | Out-Null; throw "API answered without a token" }
  catch { if ($_.Exception.Response.StatusCode.value__ -ne 401) { throw } }
} finally {
  if (-not $proc.HasExited) { taskkill /pid $proc.Id /T /F | Out-Null }
  Remove-Item Env:PORT, Env:API_TOKEN, Env:DATA_DIR, Env:DB_PATH, Env:VECTOR_DB_DIR, Env:ASSISTANT_DB_PATH, Env:VOICE_MODELS_DIR -ErrorAction SilentlyContinue
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Built backend\dist\lfa-backend. Now: cd ui; npm run build"
