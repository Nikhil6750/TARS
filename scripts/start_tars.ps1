#Requires -Version 5.1
<#
.SYNOPSIS
    TARS single-command launcher. Starts the backend, validates real
    (non-mock) voice providers are configured, waits for it to be healthy,
    then launches the native TARS app. One PowerShell window, one command.

.DESCRIPTION
    1. Detects and force-stops any stale native tars-companion.exe process
       from this worktree so the release binary is never locked -- the user
       never has to manually kill TARS before restarting it.
    2. Detects and stops a stale TARS backend already listening on the
       configured port, but only if it's actually this repo's backend
       (checked via its process command line) -- never kills an unrelated
       process that happens to hold the port.
    3. Loads .env into the process environment (with worktree discovery).
    4. Validates ASSISTANT_PROVIDER/STT_PROVIDER/TTS_PROVIDER against the
       real providers TARS expects (mirrors app/readiness.py) -- warns
       rather than blocking, since mock mode is a legitimate dev mode, but
       never pretends misconfiguration is fine.
    5. Starts the backend (python run.py, apps/backend's own entrypoint).
    6. Polls GET /api/v1/health until it reports "ok".
    7. Computes a source fingerprint over the native/frontend inputs that
       feed the release binary. If a prior build's recorded provenance
       matches (same source fingerprint, same exe hash), it reuses the
       existing verified binary instead of rebuilding. Otherwise it builds
       once and records new provenance. Pass -Rebuild to force a rebuild.
    8. Launches the (possibly reused) native build and confirms
       MainWindowHandle and native window visibility.
    9. Reports TARS READY only when full runtime readiness is verified.

.PARAMETER Rebuild
    Force a native rebuild even if recorded provenance matches current
    source.
#>

param(
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RepoRoot 'apps\backend'
$WebDir = Join-Path $RepoRoot 'apps\web'
$EnvFile = Join-Path $RepoRoot '.env'
$BackendPort = 8000
$HealthUrl = "http://127.0.0.1:$BackendPort/api/v1/health"
$ReadinessUrl = "http://127.0.0.1:$BackendPort/api/v1/runtime/readiness"

if (Get-Command npm -ErrorAction SilentlyContinue) {
    if ($env:NPM_CONFIG_CACHE) {
        npm config set cache $env:NPM_CONFIG_CACHE --global 2>&1 | Out-Null
    }
}

function Write-Step($msg) { Write-Host $msg -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host $msg -ForegroundColor Green }
function Write-Warn($msg) { Write-Warning $msg }

# Computes a stable SHA-256 fingerprint over every file that feeds the
# native release binary (Rust source, Cargo manifests/lockfile, Tauri
# config, and frontend source/build inputs), excluding build outputs and
# dependency caches. Used to decide whether a rebuild is actually needed.
function Get-SourceFingerprint([string]$WebDir) {
    $roots = @(
        (Join-Path $WebDir 'src'),
        (Join-Path $WebDir 'src-tauri\src'),
        (Join-Path $WebDir 'public')
    ) | Where-Object { Test-Path $_ }

    $singleRelativeFiles = @(
        'index.html', 'package.json', 'package-lock.json',
        'tsconfig.json', 'tsconfig.node.json', 'vite.config.ts',
        'src-tauri\Cargo.toml', 'src-tauri\Cargo.lock',
        'src-tauri\tauri.conf.json', 'src-tauri\build.rs'
    )

    $files = @()
    foreach ($root in $roots) {
        $files += Get-ChildItem -Path $root -Recurse -File -ErrorAction SilentlyContinue
    }
    foreach ($rel in $singleRelativeFiles) {
        $full = Join-Path $WebDir $rel
        if (Test-Path $full) { $files += Get-Item -Path $full }
    }

    $sb = New-Object System.Text.StringBuilder
    $files | Sort-Object FullName | ForEach-Object {
        $relPath = $_.FullName.Substring($WebDir.Length).TrimStart('\', '/')
        $fileHash = (Get-FileHash -Path $_.FullName -Algorithm SHA256).Hash
        [void]$sb.AppendLine("$relPath`:$fileHash")
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($sb.ToString())
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha256.ComputeHash($bytes)
    } finally {
        $sha256.Dispose()
    }
    return [System.BitConverter]::ToString($hashBytes).Replace('-', '')
}

# Force-stops any running instance of this worktree's native exe (matched
# by exact process Path, so other worktrees' TARS instances are never
# touched) and waits for the OS to release the file handle.
function Stop-StaleNativeProcess([string]$ExePath) {
    $matches = Get-Process -Name 'tars-companion' -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and ($_.Path -eq $ExePath) }
    if (-not $matches) { return }
    foreach ($proc in $matches) {
        Write-Warn "  Stopping stale native TARS process (PID $($proc.Id))..."
        try {
            Stop-Process -Id $proc.Id -Force -Confirm:$false -ErrorAction Stop
        } catch {
            Write-Warn "  Could not stop PID $($proc.Id): $_"
        }
    }
    for ($i = 0; $i -lt 20; $i++) {
        $stillRunning = Get-Process -Name 'tars-companion' -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and ($_.Path -eq $ExePath) }
        if (-not $stillRunning) { break }
        Start-Sleep -Milliseconds 250
    }
    # Give Windows a moment to fully release the file handle after exit.
    Start-Sleep -Milliseconds 250
}

Write-Host "=== TARS Launcher ===" -ForegroundColor Magenta

# ---- 1. Detect/kill a stale native TARS process from this worktree ------
Write-Step "[1/8] Checking for a stale native TARS process..."
$exe = Join-Path $WebDir 'src-tauri\target\release\tars-companion.exe'
Stop-StaleNativeProcess -ExePath $exe
Write-Host "  No stale native TARS process is holding the release binary."

# ---- 2. Detect/kill a stale TARS backend owned by this repo -------------
Write-Step "[2/8] Checking for a stale backend on port $BackendPort..."
$existing = $null
try {
    $existing = Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue
} catch {
    $existing = $null
}
if ($existing) {
    foreach ($conn in $existing) {
        try {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction Stop
            $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.Id)" -ErrorAction SilentlyContinue).CommandLine
            if ($cmdLine -and $cmdLine -match [regex]::Escape($BackendDir)) {
                Write-Warn "  Stopping stale TARS backend process (PID $($proc.Id))..."
                Stop-Process -Id $proc.Id -Force -Confirm:$false
                Start-Sleep -Seconds 1
            } else {
                Write-Error "Port $BackendPort is held by PID $($proc.Id) ($($proc.ProcessName)), not this worktree. Stop it first; refusing to launch against a wrong-worktree backend."
                exit 1
            }
        } catch {
            Write-Warn "  Could not inspect the process holding port $BackendPort`: $_"
        }
    }
} else {
    Write-Host "  Port $BackendPort is free."
}

# ---- 3. Load .env (with worktree discovery) --------------------------------
Write-Step "[3/8] Loading .env..."
$parentEnv = Join-Path (Split-Path -Parent $RepoRoot) '.env'
$exampleEnv = Join-Path $RepoRoot '.env.example'
$primaryEnv = $null
try {
    $registeredWorktrees = & git -C $RepoRoot worktree list --porcelain 2>$null
    $primaryLine = $registeredWorktrees |
        Where-Object { $_ -match '^worktree .*[\\/]TARS-Overnight-Integration$' } |
        Select-Object -First 1
    if ($primaryLine) {
        $primaryPath = $primaryLine.Substring('worktree '.Length).Trim()
        $primaryEnv = Join-Path $primaryPath '.env'
    }
} catch {
    $primaryEnv = $null
}
$localIsUntouchedExample = (Test-Path $EnvFile) -and (Test-Path $exampleEnv) -and
    ((Get-FileHash $EnvFile -Algorithm SHA256).Hash -eq
     (Get-FileHash $exampleEnv -Algorithm SHA256).Hash)
if (-not (Test-Path $EnvFile) -or $localIsUntouchedExample) {
    if (Test-Path $parentEnv) {
        Copy-Item $parentEnv $EnvFile
        Write-Host "  Discovered and copied parent .env to $EnvFile"
    } elseif ($primaryEnv -and (Test-Path $primaryEnv)) {
        Copy-Item $primaryEnv $EnvFile
        Write-Host "  Discovered and copied primary-checkout .env to this worktree."
    } elseif (-not (Test-Path $EnvFile) -and (Test-Path $exampleEnv)) {
        Copy-Item $exampleEnv $EnvFile
        Write-Host "  Initialized $EnvFile from .env.example"
    }
}
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $parts = $line.Split('=', 2)
            [System.Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), 'Process')
        }
    }
    Write-Host "  Loaded $EnvFile"
}

# ---- 4. Validate providers -------------------------------------------------
Write-Step "[4/8] Validating provider configuration..."
$assistantProvider = [System.Environment]::GetEnvironmentVariable('ASSISTANT_PROVIDER')
$sttProvider = [System.Environment]::GetEnvironmentVariable('STT_PROVIDER')
$ttsProvider = [System.Environment]::GetEnvironmentVariable('TTS_PROVIDER')

$problems = @()
if ($assistantProvider -ne 'claude_code') { $problems += "ASSISTANT_PROVIDER=$assistantProvider (expected claude_code)" }
if ($sttProvider -ne 'faster_whisper') { $problems += "STT_PROVIDER=$sttProvider (expected faster_whisper)" }
if ($ttsProvider -ne 'kokoro') { $problems += "TTS_PROVIDER=$ttsProvider (expected kokoro)" }

if ($problems.Count -gt 0) {
    Write-Warn "TARS voice services are not configured."
    foreach ($p in $problems) { Write-Warn "  - $p" }
    Write-Warn "  Continuing in degraded/mock mode -- check GET $ReadinessUrl once the backend is up."
} else {
    Write-Ok "  ASSISTANT_PROVIDER, STT_PROVIDER, TTS_PROVIDER all configured for real interaction."
}

$claudeCmd = [System.Environment]::GetEnvironmentVariable('CLAUDE_CODE_COMMAND')
if (-not $claudeCmd) { $claudeCmd = 'claude' }
$claudeResolved = Get-Command $claudeCmd -ErrorAction SilentlyContinue
if (-not $claudeResolved) {
    Write-Warn "  Claude Code CLI ('$claudeCmd') not found on PATH -- ClaudeCodeProvider will fail until this is installed/configured."
} else {
    Write-Ok "  Claude Code CLI found at $($claudeResolved.Source)"
}

# ---- 5. Start backend -------------------------------------------------------
Write-Step "[5/8] Starting TARS backend..."
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "python not found on PATH. Install Python 3.12+ and backend dependencies (apps/backend/requirements*.txt) first."
    exit 1
}
$runPyPath = Join-Path $BackendDir 'run.py'
$backendProc = Start-Process -FilePath $pythonCmd.Source -ArgumentList $runPyPath -WorkingDirectory $BackendDir -PassThru -WindowStyle Hidden
Write-Host "  Backend process started (PID $($backendProc.Id))."

# ---- 6. Wait for /health ----------------------------------------------------
Write-Step "[6/8] Waiting for backend health at $HealthUrl..."
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $resp = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2 -ErrorAction Stop
        if ($resp.status -eq 'ok') { $healthy = $true; break }
    } catch {
        # keep polling
    }
    Start-Sleep -Seconds 1
}
if (-not $healthy) {
    Write-Error "Backend did not report healthy within 60s. Check the backend process (PID $($backendProc.Id))."
    exit 1
}
Write-Ok "  Backend healthy (PID $($backendProc.Id))."

$readiness = $null
for ($r = 0; $r -lt 60; $r++) {
    try {
        $readiness = Invoke-RestMethod -Uri $ReadinessUrl -TimeoutSec 5 -ErrorAction Stop
        if ($readiness.ready -eq $true) {
            break
        }
    } catch {
        $readiness = $null
    }
    Start-Sleep -Seconds 1
}
if ($readiness) {
    Write-Host "  Readiness: assistant=$($readiness.assistant.ready) stt=$($readiness.stt.ready) tts=$($readiness.tts.ready) wake=$($readiness.wake.ready) claude_cli=$($readiness.claude_cli.ready) database=$($readiness.database.ready) -> ready=$($readiness.ready)"
    if ($readiness.ready -eq $false -and $readiness.message) {
        Write-Warn "  $($readiness.message)"
    }
} else {
    Write-Warn "  Could not fetch readiness snapshot from $ReadinessUrl"
}

# ---- 7. Build (only if needed) and launch native TARS -----------------------
Write-Step "[7/8] Preparing source-matched native TARS..."
$npmCmd = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npmCmd) {
    Write-Error "npm not found on PATH. Install Node.js before launching TARS."
    exit 1
}
$cargoCmd = Get-Command cargo -ErrorAction SilentlyContinue
if (-not $cargoCmd) {
    $rustBin = Join-Path $env:USERPROFILE '.rustup\toolchains\stable-x86_64-pc-windows-msvc\bin'
    $directCargo = Join-Path $rustBin 'cargo.exe'
    if (-not (Test-Path $directCargo)) {
        Write-Error "cargo not found on PATH and no stable Rust toolchain was found at $directCargo"
        exit 1
    }
    $env:PATH = "$rustBin;$env:PATH"
}
$env:CARGO_TARGET_DIR = Join-Path $WebDir 'src-tauri\target'
$env:TARS_BACKEND_URL = "http://127.0.0.1:$BackendPort"

$provenanceFile = Join-Path $WebDir 'src-tauri\target\release\.tars-build-provenance.json'
$currentFingerprint = Get-SourceFingerprint -WebDir $WebDir

$needsBuild = $true
if (-not $Rebuild -and (Test-Path $exe) -and (Test-Path $provenanceFile)) {
    try {
        $provenance = Get-Content $provenanceFile -Raw | ConvertFrom-Json
        $currentExeHash = (Get-FileHash -Path $exe -Algorithm SHA256).Hash
        if ($provenance.sourceFingerprint -eq $currentFingerprint -and
            $provenance.exeHash -eq $currentExeHash) {
            $needsBuild = $false
        }
    } catch {
        $needsBuild = $true
    }
}

if (-not $needsBuild) {
    Write-Ok "  Source unchanged since last verified build -- reusing existing release binary."
} else {
    Write-Step "  Source changed (or no verified provenance found) -- building native TARS..."
    Push-Location $WebDir
    try {
        if (-not (Test-Path (Join-Path $WebDir 'node_modules'))) {
            & $npmCmd.Source ci
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
        }
        # The exe may still be locked by a process this script didn't start
        # (e.g. launched manually). Retry once after another stop attempt
        # rather than ever failing the user with a raw file-lock error.
        Stop-StaleNativeProcess -ExePath $exe
        & $npmCmd.Source run tauri build
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "  Native build failed (possible lingering file lock) -- retrying once..."
            Stop-StaleNativeProcess -ExePath $exe
            & $npmCmd.Source run tauri build
            if ($LASTEXITCODE -ne 0) { throw "native build failed with exit code $LASTEXITCODE" }
        }
    } finally {
        Pop-Location
    }

    if (-not (Test-Path $exe)) {
        Write-Error "Source-matched native build completed without producing $exe"
        exit 1
    }
    $builtExeHash = (Get-FileHash -Path $exe -Algorithm SHA256).Hash
    @{
        sourceFingerprint = $currentFingerprint
        exeHash           = $builtExeHash
        builtAt           = (Get-Date).ToString('o')
    } | ConvertTo-Json | Set-Content -Path $provenanceFile -Encoding UTF8
    Write-Ok "  Recorded build provenance."
}

if (Test-Path $exe) {
    $nativeProc = Start-Process -FilePath $exe -WorkingDirectory $WebDir -PassThru
    Write-Ok "  Launched source-matched $exe (PID $($nativeProc.Id))"

    # Verify native window creation and frontend loaded
    $windowVerified = $false
    for ($w = 0; $w -lt 20; $w++) {
        Start-Sleep -Milliseconds 500
        try {
            $procRefreshed = Get-Process -Id $nativeProc.Id -ErrorAction Stop
            if ($procRefreshed.MainWindowHandle -ne 0 -and $procRefreshed.MainWindowTitle -eq 'TARS Ready') {
                $windowVerified = $true
                Write-Ok "  Native webview verified (title: '$($procRefreshed.MainWindowTitle)', handle: $($procRefreshed.MainWindowHandle))."
                break
            }
        } catch {
            # continue waiting
        }
    }
    if (-not $windowVerified) {
        Write-Error "Native process started but the TARS Ready webview marker did not appear."
        exit 1
    }
} else {
    Write-Error "Native release binary was not found at $exe"
    exit 1
}

# ---- 8. Ready ----------------------------------------------------------------
if ($readiness -and $readiness.ready -eq $true) {
    Write-Step "[8/8] TARS READY"
    Write-Host 'Say: Hey TARS' -ForegroundColor Green
} else {
    Write-Warn "[8/8] TARS STARTED (DEGRADED: Some runtime providers are not fully ready)"
}
