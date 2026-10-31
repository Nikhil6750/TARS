@echo off
REM One-command launcher for the TARS Market X-Ray dashboard.
REM Builds the candle store if needed, starts the backend and the web server,
REM then opens the dashboard. No API key required; trading stays disabled.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-market-xray.ps1" %*
endlocal
