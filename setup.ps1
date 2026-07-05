# ai-record - one-time setup: create .venv and install all dependencies.
# Run this ONCE. After it finishes, use run.ps1 (or the desktop shortcut) to launch.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
Write-Host "==== ai-record setup ====" -ForegroundColor Cyan

# 1) venv
if (-not (Test-Path ".venv")) {
    Write-Host "Creating .venv (Python 3.12)..."
    py -3.12 -m venv .venv
}
$vpy = Join-Path $root ".venv\Scripts\python.exe"

# 2) pip + CUDA torch (RTX 4070). Change the index-url if your CUDA differs.
& $vpy -m pip install --upgrade pip
Write-Host "Installing CUDA torch (cu124)..." -ForegroundColor Yellow
& $vpy -m pip install "torch>=2.2" --index-url https://download.pytorch.org/whl/cu124

# 3) app runtime deps
Write-Host "Installing app requirements..." -ForegroundColor Yellow
& $vpy -m pip install -r requirements.txt

# 4) M2-M4 extras (translation + diarization)
Write-Host "Installing M2-M4 extras (transformers / resemblyzer / pyannote)..." -ForegroundColor Yellow
& $vpy -m pip install transformers sentencepiece ctranslate2 resemblyzer "pyannote.audio>=3.1"

# 5) verify
Write-Host "Verifying torch CUDA..." -ForegroundColor Yellow
& $vpy -c "import torch; print('torch', torch.__version__, '| cuda available:', torch.cuda.is_available(), '|', (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only'))"

Write-Host ""
Write-Host "Setup complete. Launch with:  .\run.ps1   (or the desktop shortcut)" -ForegroundColor Green
Write-Host "First launch downloads models (~4-6 GB) and may take a few minutes." -ForegroundColor Green
