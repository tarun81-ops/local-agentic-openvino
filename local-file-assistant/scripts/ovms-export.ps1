# One-time: turn Hugging Face models into OpenVINO models that OpenVINO Model Server (OVMS) serves
# under the names "chat" and "embed" (the app's defaults in backend\.env). Uses OVMS's own export
# script. Re-run with other -ChatModel / -ChatDevice values to try another model; the app needs no
# change because the names stay the same. Restart OVMS afterwards.
#
#   scripts\ovms-export.ps1                              # defaults below
#   scripts\ovms-export.ps1 -ChatDevice NPU              # chat model compiled for the NPU
#
# Check that the model is on the OVMS supported list for the device you pick (NPU especially).
param(
  [string]$ChatModel = "Qwen/Qwen2.5-3B-Instruct",
  [string]$EmbedModel = "ibm-granite/granite-embedding-278m-multilingual",
  [ValidateSet("CPU", "GPU", "NPU")] [string]$ChatDevice = "GPU",
  [ValidateSet("CPU", "GPU")] [string]$EmbedDevice = "CPU",
  [ValidateSet("int4", "int8", "fp16")] [string]$ChatWeights = "int4",
  # Use the OVMS release tag that matches your ovms.exe, e.g. "releases/2025/3".
  [string]$OvmsRef = "main"
)
$ErrorActionPreference = "Stop"
$root = Join-Path $env:APPDATA "LocalFileAssistant\ovms"
$models = Join-Path $root "models"
$config = Join-Path $models "config.json"
New-Item -ItemType Directory -Force $models | Out-Null

$venv = Join-Path $root "export-venv"
if (-not (Test-Path $venv)) { python -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"
$exporter = Join-Path $root "export_model.py"
if (-not (Test-Path $exporter)) {
  $base = "https://raw.githubusercontent.com/openvinotoolkit/model_server/$OvmsRef/demos/common/export_models"
  Invoke-WebRequest "$base/export_model.py" -OutFile $exporter
  Invoke-WebRequest "$base/requirements.txt" -OutFile (Join-Path $root "export-requirements.txt")
  & $py -m pip install --upgrade pip
  & $py -m pip install -r (Join-Path $root "export-requirements.txt")
  if ($LASTEXITCODE -ne 0) { throw "Installing the export tools failed." }
}

& $py $exporter text_generation --source_model $ChatModel --model_name chat `
  --weight-format $ChatWeights --target_device $ChatDevice `
  --config_file_path $config --model_repository_path $models --overwrite_models
if ($LASTEXITCODE -ne 0) { throw "Exporting the chat model failed." }

& $py $exporter embeddings_ov --source_model $EmbedModel --model_name embed `
  --weight-format int8 --target_device $EmbedDevice --pooling CLS `
  --config_file_path $config --model_repository_path $models --overwrite_models
if ($LASTEXITCODE -ne 0) { throw "Exporting the embedding model failed." }

Write-Host ""
Write-Host "Exported to $models"
Write-Host "Start the server with:  scripts\ovms-start.ps1"
Write-Host "Then rescan your folders once: vectors from another embedding model don't match."
