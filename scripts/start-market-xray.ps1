<#
.SYNOPSIS
    Launches the TARS Market X-Ray dashboard: candle store check, backend, Vite
    dev server, then opens the dashboard in the default browser.

.DESCRIPTION
    One command to a working dashboard. The script refuses to start in a state
    that would show empty panels: if the Parquet candle store is missing it
    builds it first (or tells you why it cannot), and it waits for the backend
    to answer its own health endpoint before opening a browser window.

    Nothing here places a trade, and the dashboard needs no API key.

.PARAMETER BackendPort
    Port for the FastAPI backend. Default 8000.

.PARAMETER WebPort
    Port for the Vite dev server. Default 5173.

.PARAMETER Reingest
    Rebuild the candle store from the source CSVs even if it already exists.

.PARAMETER NoBrowser
    Start the servers without opening a browser window.

.EXAMPLE
    .\scripts\start-market-xray.ps1
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8000,
    [int]$WebPort = 5173,
    [switch]$Reingest,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot 'apps\backend'
$webDir = Join-Path $repoRoot 'apps\web'
$storeRoot = 'D:\TARS_DATA\market\xray_store'

function Write-Step { param([string]$Message) Write-Host "`n>> $Message" -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host "   OK  $Message" -ForegroundColor Green }
function Write-Warn2 { param([string]$Message) Write-Host "   !   $Message" -ForegroundColor Yellow }

Write-Host '==== TARS Market X-Ray ====' -ForegroundColor White

# ---- 1. candle store -------------------------------------------------------
Write-Step 'Checking the candle store'
$manifest = Join-Path $storeRoot 'manifest.json'
if ($Reingest -or -not (Test-Path $manifest)) {
    if ($Reingest) {
        Write-Warn2 'Rebuilding the candle store as requested (-Reingest).'
    } else {
        Write-Warn2 "No candle store at $storeRoot - building it now (about 35s)."
    }
    Push-Location $backendDir
    try {
        python -m market.ingest
        if ($LASTEXITCODE -ne 0) { throw "Ingestion failed with exit code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
}
if (-not (Test-Path $manifest)) {
    throw "Candle store still missing at $storeRoot. Check that the source CSVs exist under 'C:\Trade Data'."
}
$meta = Get-Content $manifest -Raw | ConvertFrom-Json
Write-Ok "$($meta.symbols_ok.Count) instruments, window from $($meta.window_start), built $($meta.generated_at)"

# ---- 2. backend ------------------------------------------------------------
Write-Step "Starting the backend on port $BackendPort"
$backend = Start-Process -FilePath 'python' `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$BackendPort" `
    -WorkingDirectory $backendDir -PassThru -WindowStyle Minimized

$healthUrl = "http://127.0.0.1:$BackendPort/api/v1/market/health"
$ready = $false
foreach ($attempt in 1..60) {
    Start-Sleep -Seconds 1
    if ($backend.HasExited) { throw "Backend exited early with code $($backend.ExitCode)." }
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
        if ($health.ok) { $ready = $true; break }
    } catch {
        # Not up yet; keep waiting.
    }
}
if (-not $ready) { throw "Backend did not become healthy at $healthUrl." }
Write-Ok "Backend healthy. Feed status: $($health.status.status) - $($health.status.detail)"

# ---- 3. frontend -----------------------------------------------------------
Write-Step 'Starting the Vite dev server'
if (-not (Test-Path (Join-Path $webDir 'node_modules'))) {
    Write-Warn2 'node_modules missing - running npm install (this takes a while).'
    Push-Location $webDir
    try { npm install } finally { Pop-Location }
}

$env:VITE_TARS_API_URL = "http://127.0.0.1:$BackendPort"
$web = Start-Process -FilePath 'cmd.exe' `
    -ArgumentList '/c', 'npm', 'run', 'dev', '--', '--port', "$WebPort", '--strictPort' `
    -WorkingDirectory $webDir -PassThru -WindowStyle Minimized

$dashboardUrl = "http://127.0.0.1:$WebPort/market.html"
$webReady = $false
foreach ($attempt in 1..60) {
    Start-Sleep -Seconds 1
    if ($web.HasExited) { throw "Vite exited early with code $($web.ExitCode)." }
    try {
        $null = Invoke-WebRequest -Uri $dashboardUrl -TimeoutSec 5 -UseBasicParsing
        $webReady = $true
        break
    } catch {
        # Not up yet; keep waiting.
    }
}
if (-not $webReady) { throw "Vite did not serve $dashboardUrl." }
Write-Ok "Dashboard served at $dashboardUrl"

# ---- 4. open ---------------------------------------------------------------
if (-not $NoBrowser) {
    Write-Step 'Opening the dashboard'
    Start-Process $dashboardUrl
}

Write-Host ''
Write-Host '  Dashboard : ' -NoNewline; Write-Host $dashboardUrl -ForegroundColor Cyan
Write-Host '  API       : ' -NoNewline; Write-Host "http://127.0.0.1:$BackendPort/api/v1/market/health" -ForegroundColor Cyan
Write-Host '  Companion : ' -NoNewline; Write-Host "http://127.0.0.1:$WebPort/" -ForegroundColor DarkGray
Write-Host ''
Write-Host "  Trading execution is DISABLED. Feed status: $($health.status.status)." -ForegroundColor DarkGray
Write-Host '  Press Ctrl+C to stop both servers.' -ForegroundColor DarkGray

try {
    while (-not $backend.HasExited -and -not $web.HasExited) { Start-Sleep -Seconds 2 }
} finally {
    Write-Host "`nStopping..." -ForegroundColor Yellow
    foreach ($proc in @($backend, $web)) {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
