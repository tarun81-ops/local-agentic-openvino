# Starts OpenVINO Model Server on this PC only (127.0.0.1) with the models in
# %APPDATA%\LocalFileAssistant\ovms\models. OVMS is expected in %LOCALAPPDATA%\ovms (unzipped there
# from the ovms_windows_<version>_python_on.zip release, see docs\OPENVINO.md); pass -OvmsDir otherwise.
param(
  [string]$OvmsDir = (Join-Path $env:LOCALAPPDATA "ovms"),
  [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
$ovms = Join-Path $OvmsDir "ovms.exe"
if (-not (Test-Path $ovms)) { throw "ovms.exe not found in $OvmsDir. Unzip the OVMS release there, or pass -OvmsDir <folder>." }
# OVMS needs its environment (DLL and Python paths) in this window.
. (Join-Path $OvmsDir "setupvars.ps1")

$root = Join-Path $env:APPDATA "LocalFileAssistant\ovms"
$config = Join-Path $root "models\config.json"
if (-not (Test-Path $config)) { throw "No models yet: $config is missing. Pull or export the models first (docs\OPENVINO.md)." }
# The compiled-model cache makes every start after the first one fast (NPU/GPU compile once).
$cache = Join-Path $root "cache"
New-Item -ItemType Directory -Force $cache | Out-Null
Write-Host "OpenVINO Model Server on http://127.0.0.1:$Port/v3 (Ctrl+C to stop)"
& $ovms --rest_port $Port --rest_bind_address 127.0.0.1 --config_path $config --cache_dir $cache
