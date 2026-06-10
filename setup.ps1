# ============================================================
#  Song Downloader v2 - Windows Setup Script
#  Run in PowerShell as Administrator
#  Right-click PowerShell -> "Run as Administrator", then:
#  cd "D:\AI STUFF\PROJECTS\video"
#  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#  .\setup.ps1
# ============================================================

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n[...] $msg" -ForegroundColor Yellow }
function Write-OK($msg)   { Write-Host "[OK]  $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "[ERR] $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Song Downloader v2 - Windows Setup" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# ── Check Python ───────────────────────────────────────────
Write-Step "Checking Python..."
try {
    $pyver = python --version 2>&1
    Write-OK "Python found: $pyver"
} catch {
    Write-Fail "Python not found! Download from https://python.org and re-run this script."
    exit 1
}

# ── Install yt-dlp ─────────────────────────────────────────
Write-Step "Installing yt-dlp..."
pip install yt-dlp --quiet --upgrade
if ($LASTEXITCODE -eq 0) {
    $ytver = yt-dlp --version
    Write-OK "yt-dlp installed: $ytver"
} else {
    Write-Fail "yt-dlp install failed. Try: pip install yt-dlp"
    exit 1
}

# ── Install spotdl (Spotify support) ───────────────────────
Write-Step "Installing spotdl (Spotify integration)..."
pip install spotdl --quiet --upgrade
if ($LASTEXITCODE -eq 0) {
    Write-OK "spotdl installed"
} else {
    Write-Host "  spotdl install failed. Spotify playlists won't work." -ForegroundColor Yellow
    Write-Host "  Try manually: pip install spotdl" -ForegroundColor Yellow
}

# ── Check/Install FFmpeg via winget ────────────────────────
Write-Step "Checking FFmpeg..."
$ffmpegPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpegPath) {
    Write-OK "FFmpeg already installed"
} else {
    Write-Step "FFmpeg not found - installing via winget..."
    try {
        winget install --id Gyan.FFmpeg -e --silent
        Write-OK "FFmpeg installed. You may need to restart PowerShell for PATH to update."
    } catch {
        Write-Host ""
        Write-Host "  Could not auto-install FFmpeg." -ForegroundColor Yellow
        Write-Host "  Please download it manually:" -ForegroundColor Yellow
        Write-Host "  1. Go to https://ffmpeg.org/download.html" -ForegroundColor White
        Write-Host "  2. Download the Windows build" -ForegroundColor White
        Write-Host "  3. Extract and add the \bin folder to your PATH" -ForegroundColor White
        Write-Host "  4. Re-run this script" -ForegroundColor White
        Write-Host ""
    }
}

# ── Check Node / Install n8n ───────────────────────────────
Write-Step "Checking Node.js..."
try {
    $nodever = node --version
    Write-OK "Node.js found: $nodever"
} catch {
    Write-Fail "Node.js not found! Download from https://nodejs.org (LTS version) and re-run."
    exit 1
}

Write-Step "Installing n8n..."
$n8nCheck = Get-Command n8n -ErrorAction SilentlyContinue
if ($n8nCheck) {
    Write-OK "n8n already installed"
} else {
    npm install -g n8n
    if ($LASTEXITCODE -eq 0) { Write-OK "n8n installed" }
    else { Write-Fail "n8n install failed"; exit 1 }
}

# ── Set NODES_EXCLUDE permanently ──────────────────────────
Write-Step "Enabling Execute Command node for n8n (permanent)..."
[Environment]::SetEnvironmentVariable("NODES_EXCLUDE", "[]", "User")
$env:NODES_EXCLUDE = "[]"
Write-OK "NODES_EXCLUDE set permanently - n8n start will always work now"

# ── Create folders ─────────────────────────────────────────
Write-Step "Creating output folders..."
$musicDir = "$env:USERPROFILE\Music\AutoDownloads"
$autoDir  = "$env:USERPROFILE\automation"
New-Item -ItemType Directory -Force -Path $musicDir | Out-Null
New-Item -ItemType Directory -Force -Path $autoDir  | Out-Null
Write-OK "Created: $musicDir"
Write-OK "Created: $autoDir"

# ── Copy Python script ─────────────────────────────────────
Write-Step "Copying download_song.py..."
$scriptSrc = Join-Path $PSScriptRoot "download_song.py"
$scriptDst = "$autoDir\download_song.py"
if (Test-Path $scriptSrc) {
    Copy-Item $scriptSrc $scriptDst -Force
    Write-OK "Copied to $scriptDst"
} else {
    Write-Host "  download_song.py not found next to setup.ps1 - copy it manually to $autoDir" -ForegroundColor Yellow
}

# ── Done ───────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Quick start:" -ForegroundColor White
Write-Host ""
Write-Host "  1. Start n8n (just this, no env vars needed!):" -ForegroundColor Cyan
Write-Host "     n8n start"
Write-Host "     Then open: http://localhost:5678"
Write-Host ""
Write-Host "  2. Import the workflow:" -ForegroundColor Cyan
Write-Host "     In n8n -> Import from file -> n8n_workflow.json"
Write-Host ""
Write-Host "  3. Test standalone download:" -ForegroundColor Cyan
Write-Host "     python $scriptDst `"Bohemian Rhapsody Queen`""
Write-Host ""
Write-Host "  MP3s save to: $musicDir" -ForegroundColor White
Write-Host "  Download log: $env:USERPROFILE\Music\download_log.csv" -ForegroundColor White
Write-Host ""
