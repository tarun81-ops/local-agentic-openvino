# Tests an update without publishing anything: serves ui\release\ (the NEW build: installer, .blockmap, latest.yml) on
# 127.0.0.1 and starts the OLDER build already installed on this PC with LFA_UPDATE_URL pointing at it. electron\updates.js
# honours that variable only for a loopback address; a normal start never sets it. Nothing leaves this PC.
# The installed build must itself be 0.3.0 or newer: 0.2.0 doesn't know LFA_UPDATE_URL and would ask GitHub instead.
#
#   1. Install the older build (for example 0.3.0) and start it once.
#   2. Bump "version" in ui\package.json (0.3.0 -> 0.3.1, don't commit it) and run: cd ui; npm run build
#   3. powershell -ExecutionPolicy Bypass -File scripts\test-update.ps1
param(
  [int]$Port = 8765,
  [string]$App = "$env:LOCALAPPDATA\Programs\Local File Assistant\Local File Assistant.exe"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$release = Join-Path $root "ui\release"
$py = Join-Path $root "backend\.venv\Scripts\python.exe"
$log = Join-Path $env:APPDATA "Local File Assistant\logs\main.log"

if (-not (Test-Path (Join-Path $release "latest.yml"))) { throw "No ui\release\latest.yml. Build the new version first: cd ui; npm run build" }
if (-not (Test-Path $App)) { throw "The installed app wasn't found at $App. Install the older build first, or pass -App <path to Local File Assistant.exe>." }
if (-not (Test-Path $py)) { throw "No Python at $py. Run scripts\setup.ps1 first." }
if (Get-Process "Local File Assistant" -ErrorAction SilentlyContinue) { throw "Local File Assistant is running. Quit it from the tray first (the update test needs a fresh start)." }

$new = (Select-String -Path (Join-Path $release "latest.yml") -Pattern '^version:\s*(.+)$').Matches[0].Groups[1].Value.Trim()
Write-Host "Serving ui\release\ (version $new) on http://127.0.0.1:$Port/ (this PC only)"
$server = Start-Process -FilePath $py -ArgumentList @("-m", "http.server", "$Port", "--bind", "127.0.0.1", "--directory", "`"$release`"") -PassThru -WindowStyle Hidden
try {
  Start-Sleep -Seconds 2
  $env:LFA_UPDATE_URL = "http://127.0.0.1:$Port/"
  Start-Process -FilePath $App   # inherits LFA_UPDATE_URL from this window only
  Remove-Item Env:\LFA_UPDATE_URL

  Write-Host ""
  Write-Host "What should happen:"
  Write-Host "  1. About 10 seconds after the window opens, main.log says:"
  Write-Host "       update test: checking http://127.0.0.1:$Port/ instead of GitHub"
  Write-Host "       update $new found, downloading in the background"
  Write-Host "       update $new downloaded; waiting for Restart now"
  Write-Host "  2. Bottom right: 'Update $new is ready.' with RESTART NOW and LATER. Nothing installs by itself."
  Write-Host "  3. LATER hides it and the app keeps working; quitting from the tray does NOT install it (run this script"
  Write-Host "     again and the bar comes back). Click RESTART NOW."
  Write-Host "  4. The installer runs, the app starts again. Settings > General > Privacy says Version $new; your chats, workspaces,"
  Write-Host "     indexed folders and settings are still there; the setup wizard does not come back."
  Write-Host ""
  Write-Host "Log: $log"
  Write-Host "Press Enter here when done to stop the local server."
  [void](Read-Host)
} finally {
  if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force }
  Write-Host "Local server stopped."
}
