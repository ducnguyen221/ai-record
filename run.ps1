# ai-record - launch the app. Assumes setup.ps1 was run once.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$vpy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    Write-Host "No .venv found. Run setup.ps1 first." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "Starting ai-record... (a small always-on-top window will open)" -ForegroundColor Cyan
Write-Host "If no window appears, this console prints the URL to open in a browser." -ForegroundColor DarkGray
& $vpy -m ai_record
