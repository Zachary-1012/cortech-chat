@echo off
cd /d "%~dp0"
title Cortech Chat
setlocal

rem Local SaaS for Cortech Chat. Sets CORTECH_SAAS=1 and a data directory,
rem then runs the web server. No desktop window. Does not install packages.
set "CORTECH_SAAS=1"
set "CORTECH_DATA_DIR=%~dp0data"
set "CORTECH_HOST=127.0.0.1"
set "CORTECH_PORT=8000"
if exist "%~dp0libs\fastapi" set "PYTHONPATH=%~dp0libs;%PYTHONPATH%"

where python.exe >nul 2>nul
if errorlevel 1 (
    echo [error] Python was not found on PATH.
    echo Install Python 3 and tick Add to PATH: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo.
echo   Cortech Chat SaaS
echo   Open http://127.0.0.1:8000
echo   Data directory: %CORTECH_DATA_DIR%
echo   CORTECH_SAAS=1
echo   Close this window to stop the server.
echo.

rem Optional: install Playwright Chromium when the browser bundle is missing.
rem Failure does not stop the server. QR login can still use Edge or Chrome.
python -c "import os,sys; from pathlib import Path; roots=[Path(os.environ.get('LOCALAPPDATA',''))/'ms-playwright', Path.home()/'.cache'/'ms-playwright']; sys.exit(0 if any(p.is_dir() and any(p.iterdir()) for p in roots) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Playwright Chromium is missing. Trying a one-time install. You can ignore a failure.
    python -m playwright install chromium
    if errorlevel 1 echo [warn] Playwright Chromium was not installed. QR login can use Edge or Chrome instead.
)

python -m monitor.app
if errorlevel 1 (
    echo.
    echo [error] The server stopped with an error.
    echo If libs\ already exists this launcher does not install packages.
    echo Use start-cortech.bat once when libs\ is missing, or install requirements yourself.
    pause
    exit /b 1
)
endlocal
