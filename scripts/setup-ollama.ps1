# setup-ollama.ps1 - OPTIONAL local-summarizer setup for AI Record.
#
# You only need this if you want 100%-offline summarization (the "Ollama" provider).
# The default cloud/CLI providers (Claude CLI, Gemini) do NOT need Ollama at all.
#
# What it does:
#   1. Installs Ollama (via winget) if it is not already on PATH.
#   2. Pulls the chosen model (default: qwen2.5:7b - best balance for Vietnamese).
#
# Pick a different model per machine hardware (see ai_record\summarizer_models.json):
#   .\scripts\setup-ollama.ps1                        # qwen2.5:7b  (8-12GB VRAM)
#   .\scripts\setup-ollama.ps1 -Model qwen2.5:14b     # top quality (12-16GB VRAM)
#   .\scripts\setup-ollama.ps1 -Model qwen2.5:3b      # low-VRAM laptop (<8GB)
#
# After it finishes, open AI Record -> Settings and set:
#   Summarizer provider = Ollama, model = <the model you pulled>.

param(
    [string]$Model = "qwen2.5:7b"
)

$ErrorActionPreference = "Stop"

function Test-Ollama {
    return [bool](Get-Command ollama -ErrorAction SilentlyContinue)
}

if (-not (Test-Ollama)) {
    Write-Host "Ollama not found on PATH. Attempting to install via winget..."
    $installed = $false
    try {
        winget install --id Ollama.Ollama -e --silent --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -eq 0) { $installed = $true }
    } catch {
        Write-Host "winget install failed: $($_.Exception.Message)"
    }

    # winget may need a fresh shell for PATH; re-check.
    if (-not (Test-Ollama) -and -not $installed) {
        Write-Host ""
        Write-Host "Could not install Ollama automatically." -ForegroundColor Red
        Write-Host "Download and install it manually from: https://ollama.com/download"
        Write-Host "Then re-run:  .\scripts\setup-ollama.ps1 -Model $Model"
        exit 1
    }

    if (-not (Test-Ollama)) {
        Write-Host ""
        Write-Host "Ollama was installed but is not on PATH in this shell yet." -ForegroundColor Yellow
        Write-Host "Open a NEW terminal and run:  ollama pull $Model"
        Write-Host "Or install manually from: https://ollama.com/download"
        exit 1
    }
}

Write-Host "Pulling model: $Model ..."
ollama pull $Model
if ($LASTEXITCODE -ne 0) {
    Write-Host "ollama pull '$Model' failed (exit $LASTEXITCODE)." -ForegroundColor Red
    Write-Host "Check the tag exists in the Ollama library: https://ollama.com/library"
    exit 1
}

Write-Host ""
Write-Host "Success. '$Model' is ready." -ForegroundColor Green
Write-Host "In AI Record -> Settings: Summarizer provider = Ollama, model = $Model"
