<#
.SYNOPSIS
    Launches the TARS Market Intelligence desktop application (Tauri 2).

.DESCRIPTION
    Opens the Market X-Ray as a native Windows desktop window inside the
    existing TARS desktop shell -- not a browser tab. The Tauri shell's own
    BackendSupervisor adopts or spawns the Python backend, so this script does
    not start one itself; it only makes sure the candle store exists first,
    because a dashboard with no store would open onto an error panel.

    Building the desktop shell requires the MSVC C++ build tools (they provide
    link.exe). This script checks for them up front and explains exactly what
    to install rather than failing later with a linker error.

    Trading execution is disabled. No API key is required.

.PARAMETER Dev
    Run through `tauri dev` (Vite dev server + debug binary) instead of the
    built application. For frontend iteration, not for normal use.

.PARAMETER Rebuild
    Force a fresh release build even if a binary already exists.

.PARAMETER SkipIngest
    Do not build the candle store even if it is missing.

.EXAMPLE
    .\scripts\start-market-desktop.ps1
#>
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Rebuild,
    [switch]$SkipIngest,
    # The port the Tauri shell's BackendSupervisor uses. Changing it here does
    # not move the backend -- it only changes where this script looks.
    [int]$BackendPort = 8000
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot 'apps\backend'
$webDir = Join-Path $repoRoot 'apps\web'
# Ask the backend where its store is rather than restating the path here:
# it resolves TARS_DATA_ROOT (see apps/backend/market/ingest.py, P1-C2), so a
# machine with its data elsewhere needs no change to this script.
$storeRoot = & python -c "import sys; sys.path.insert(0, r'$backendDir'); from market.ingest import DEFAULT_STORE_ROOT; print(DEFAULT_STORE_ROOT)"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($storeRoot)) {
    throw "Could not resolve the candle-store root from the backend. Is Python on PATH?"
}
$storeRoot = $storeRoot.Trim()

function Write-Step { param([string]$Message) Write-Host "`n>> $Message" -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host "   OK  $Message" -ForegroundColor Green }
function Write-Warn2 { param([string]$Message) Write-Host "   !   $Message" -ForegroundColor Yellow }

Write-Host '==== TARS Market Intelligence (desktop) ====' -ForegroundColor White

# ---- 1. toolchain preflight ------------------------------------------------
# Tauri compiles a Rust binary, which needs the MSVC linker. Checking here
# turns an opaque "link.exe returned an unexpected error" into a clear
# instruction.
Write-Step 'Checking the desktop build toolchain'

$cargoExe = Join-Path $env:USERPROFILE '.cargo\bin\cargo.exe'
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    if (Test-Path $cargoExe) {
        $env:PATH = "$(Split-Path $cargoExe);$env:PATH"
    } else {
        throw "Rust/cargo not found. Install it from https://rustup.rs and re-run this script."
    }
}
Write-Ok "cargo $((cargo --version) -replace '^cargo\s+','')"

# Locate a real MSVC linker. Git ships a POSIX `link.exe` that is NOT a
# linker, and if it is found first the build fails confusingly -- so look for
# the MSVC toolchain explicitly rather than trusting PATH.
$msvcLink = $null
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path $vswhere) {
    $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
    if ($vsPath) {
        $msvcLink = Get-ChildItem (Join-Path $vsPath 'VC\Tools\MSVC') -Recurse -Filter 'link.exe' -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like '*Hostx64\x64*' } | Select-Object -First 1 -ExpandProperty FullName
    }
}

if (-not $msvcLink) {
    Write-Host ''
    Write-Host '  BLOCKED: the MSVC C++ build tools are not installed.' -ForegroundColor Red
    Write-Host ''
    Write-Host '  Tauri builds a native Windows binary, which needs the MSVC linker (link.exe).'
    Write-Host '  Rust and Node are present, but the C++ toolchain is not.'
    Write-Host ''
    Write-Host '  Install ONE of these, then re-run this script:' -ForegroundColor Yellow
    Write-Host '    * Visual Studio Build Tools, selecting the'
    Write-Host '      "Desktop development with C++" workload:'
    Write-Host '      https://visualstudio.microsoft.com/visual-cpp-build-tools/'
    Write-Host '    * or, via winget:'
    Write-Host '      winget install --id Microsoft.VisualStudio.2022.BuildTools --override ' -NoNewline
    Write-Host '"--quiet --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"'
    Write-Host ''
    Write-Host '  Until then the same dashboard runs in a browser window via:' -ForegroundColor DarkGray
    Write-Host '    scripts\start-market-xray.cmd' -ForegroundColor DarkGray
    Write-Host ''
    throw 'MSVC C++ build tools are required to build the desktop application.'
}
Write-Ok "MSVC linker $msvcLink"

# ---- 2. candle store -------------------------------------------------------
Write-Step 'Checking the candle store'
$manifest = Join-Path $storeRoot 'manifest.json'
if (-not (Test-Path $manifest)) {
    if ($SkipIngest) {
        Write-Warn2 'Candle store missing and -SkipIngest was given; the dashboard will show an error panel.'
    } else {
        Write-Warn2 "No candle store at $storeRoot - building it now (about 35s)."
        Push-Location $backendDir
        try {
            python -m market.ingest
            if ($LASTEXITCODE -ne 0) { throw "Ingestion failed with exit code $LASTEXITCODE." }
        } finally {
            Pop-Location
        }
    }
}
if (Test-Path $manifest) {
    $meta = Get-Content $manifest -Raw | ConvertFrom-Json
    Write-Ok "$($meta.symbols_ok.Count) instruments, built $($meta.generated_at)"
}

# ---- 3. node dependencies --------------------------------------------------
if (-not (Test-Path (Join-Path $webDir 'node_modules'))) {
    Write-Step 'Installing web dependencies'
    Push-Location $webDir
    try { npm install } finally { Pop-Location }
}

# ---- 4. launch the desktop app --------------------------------------------
# Tells the shell to open the Market Intelligence window at startup rather
# than waiting for the tray menu.
$env:TARS_OPEN_MARKET = '1'
$healthUrl = "http://127.0.0.1:$BackendPort/api/v1/market/health"
$exe = Join-Path $webDir 'src-tauri\target\release\tars-companion.exe'

if ($Dev) {
    # Dev mode serves the UI from Vite. Note that tauri.conf.json points the
    # webview at a fixed port: if Vite finds it busy it silently moves to the
    # next one, and the app then loads whatever is still sitting on the
    # original port -- possibly a stale server from an earlier run. Stop any
    # leftover `vite` before using this.
    Write-Step 'Starting in DEV mode (Vite dev server + debug binary)'
    Push-Location $webDir
    try {
        & npm run tauri dev
        if ($LASTEXITCODE -ne 0) { throw "The desktop application exited with code $LASTEXITCODE." }
    } finally {
        Pop-Location
        Remove-Item Env:\TARS_OPEN_MARKET -ErrorAction SilentlyContinue
    }
    return
}

if ($Rebuild -or -not (Test-Path $exe)) {
    Write-Step 'Building the desktop application (release)'
    Write-Host '   The first build compiles the Rust shell and takes several minutes.' -ForegroundColor DarkGray
    Push-Location $webDir
    try {
        & npm run tauri build
        if ($LASTEXITCODE -ne 0) { throw "The desktop build failed with exit code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
}
if (-not (Test-Path $exe)) { throw "Build reported success but $exe is missing." }
Write-Ok "binary $exe"

Write-Step 'Launching Market Intelligence'
# A second launch of an already-running TARS is handled by the single-instance
# plugin, which opens the Market window on the existing instance instead of
# starting a duplicate.
$running = Get-Process -Name 'tars-companion' -ErrorAction SilentlyContinue
if ($running) {
    Write-Warn2 "TARS is already running (pid $($running.Id -join ', ')); opening its Market window."
    & $exe --market
} else {
    Start-Process -FilePath $exe | Out-Null
}

$deadline = (Get-Date).AddMinutes(3)
$health = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    try {
        $probe = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
        if ($probe.ok) { $health = $probe; break }
    } catch {
        # Not up yet; keep waiting.
    }
}
Remove-Item Env:\TARS_OPEN_MARKET -ErrorAction SilentlyContinue

if ($health) {
    Write-Ok "Backend healthy. Feed status: $($health.status.status) - $($health.status.detail)"
} else {
    Write-Warn2 'The app launched but its backend did not report healthy within 3 minutes.'
}

Write-Host ''
Write-Host '  Market Intelligence is open as a desktop window.' -ForegroundColor Cyan
Write-Host '  Reopen it any time from the tray menu, or with:' -ForegroundColor DarkGray
Write-Host "    `"$exe`" --market" -ForegroundColor DarkGray
Write-Host '  Quit cleanly (this also stops the Python backend) with:' -ForegroundColor DarkGray
Write-Host "    `"$exe`" --quit" -ForegroundColor DarkGray
Write-Host '  Trading execution is DISABLED.' -ForegroundColor DarkGray
