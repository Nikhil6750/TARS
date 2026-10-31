@echo off
REM Builds the self-contained Windows installer: backend runtime first, then the
REM Tauri app with that runtime bundled. Needs Python, Node, Rust and MSVC on the
REM build machine. The machine it is installed on needs none of them.
setlocal
python "%~dp0build-backend-runtime.py" %* || exit /b 1
pushd "%~dp0..\apps\web"
call npm run tauri build -- --config src-tauri/tauri.bundle.conf.json
set STATUS=%ERRORLEVEL%
popd
endlocal & exit /b %STATUS%
