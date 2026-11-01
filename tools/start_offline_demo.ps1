<# Opens the packaged desktop over genuine, local recorded history. No build or import. #>
[CmdletBinding()]
param(
    [string]$AppDirectory = $PSScriptRoot,
    [string]$DataRoot = '',
    [int]$Port = 8775,
    [switch]$Quit
)
$ErrorActionPreference = 'Stop'
$appRoot = (Resolve-Path -LiteralPath $AppDirectory).Path
$exe = Join-Path $appRoot 'tars-companion.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "Packaged desktop missing: $exe" }
$running = @(Get-Process -Name 'tars-companion' -ErrorAction SilentlyContinue)
if ($running | Where-Object { $_.Path -ne $exe }) {
    throw 'Another TARS copy is running. Quit that copy before using this demo.'
}
if ($Quit) { & $exe --quit; exit $LASTEXITCODE }
if ($running) { & $exe --market; exit $LASTEXITCODE }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Demo port $Port is occupied; no existing backend will be adopted."
}
if (-not $DataRoot) { $DataRoot = Join-Path $appRoot 'data' }
$dataPath = (Resolve-Path -LiteralPath $DataRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $dataPath 'market\xray_store\manifest.json'))) {
    throw 'The demo requires its recorded candle-store copy. It will not fabricate or import data.'
}
if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'tars\python\python.exe'))) {
    throw 'The packaged Python runtime is missing.'
}
# A dedicated loopback port and disabled external adapters keep this demo
# separate from the normal application and truthful about historical data.
Remove-Item Env:\TARS_BACKEND_DIR -ErrorAction SilentlyContinue
$env:TARS_BACKEND_URL = "http://127.0.0.1:$Port"
$env:BACKEND_PORT = "$Port"
$env:TARS_DATA_ROOT = $dataPath
$env:MONITORS_ENABLED = 'false'
$env:TARS_DISABLE_MONITORS = '1'
$env:MT5_ENABLED = 'false'
$env:CALENDAR_ENABLED = 'false'
$env:NEWS_ENABLED = 'false'
$env:SETUP_WATCH_AGENT_ENABLED = 'false'
$env:USE_MOCK_TRADING_EVENTS = 'false'
$env:ASSISTANT_PROVIDER = 'mock'
$env:STT_PROVIDER = 'mock'
$env:TTS_PROVIDER = 'mock'
$env:WAKE_WORD_PROVIDER = 'mock'
Write-Host 'TARS offline demo: RECORDED HISTORY / REPLAY. Trading execution DISABLED.'
Start-Process -FilePath $exe -ArgumentList '--market' -WorkingDirectory $appRoot
Write-Host "Quit: powershell -File `"$PSCommandPath`" -AppDirectory `"$appRoot`" -Quit"
