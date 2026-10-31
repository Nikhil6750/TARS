@echo off
REM Launches the TARS Market Intelligence DESKTOP application (Tauri 2).
REM Needs the MSVC C++ build tools; the script checks and tells you if missing.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-market-desktop.ps1" %*
endlocal
