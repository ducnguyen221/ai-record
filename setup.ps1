# ai-record - one-time setup: create .venv and install all dependencies.
# Run this ONCE. After it finishes, use run.ps1 (or the desktop shortcut) to launch.
#
# IMPORTANT: CUDA torch is installed LAST. Some packages (pyannote.audio /
# torchaudio) pull a CPU-only torch as a dependency and would clobber a GPU
# build installed earlier -> we install the CUDA torch at the very end and
# force-reinstall so GPU is preserved.
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
& $vpy -m pip install --upgrade pip

# 2) app runtime deps (no build tools needed: webrtcvad-wheels is prebuilt)
Write-Host "Installing app requirements..." -ForegroundColor Yellow
& $vpy -m pip install -r requirements.txt

# 3) M2-M4 extras. resemblyzer depends on the C-extension 'webrtcvad'; we ship
# prebuilt 'webrtcvad-wheels' (in requirements.txt) and install resemblyzer with
# --no-deps, so no Microsoft C++ Build Tools are required.
Write-Host "Installing M2-M4 extras (transformers / pyannote / librosa)..." -ForegroundColor Yellow
& $vpy -m pip install transformers sentencepiece ctranslate2 "pyannote.audio>=3.1" librosa
Write-Host "Installing resemblyzer (no-deps; uses webrtcvad-wheels)..." -ForegroundColor Yellow
& $vpy -m pip install --no-deps resemblyzer

# 4) CUDA torch LAST (force-reinstall to override any CPU torch pulled above).
# Change the index-url if your CUDA toolkit differs (cu121 / cu126 ...).
Write-Host "Installing CUDA torch (cu124) - LAST so GPU is preserved..." -ForegroundColor Yellow
& $vpy -m pip install --force-reinstall torch torchaudio --index-url https://download.pytorch.org/whl/cu124

# 5) verify
Write-Host "Verifying..." -ForegroundColor Yellow
& $vpy -c "import torch, faster_whisper, soundcard, silero_vad, transformers, resemblyzer; print('torch', torch.__version__, '| cuda:', torch.cuda.is_available(), '|', (torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only'))"

Write-Host ""
Write-Host "Setup complete. Launch with:  .\run.ps1   (or the desktop shortcut)" -ForegroundColor Green
Write-Host "First launch downloads models (~4-6 GB) and may take a few minutes." -ForegroundColor Green
