@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Chat Cortech

echo.
echo   Chat Cortech · 正在启动桌面客户端...
echo.

rem 优先用 pythonw.exe（无控制台窗口）；找不到则退回控制台模式
set "PYW="
for %%P in (pythonw.exe) do if not defined PYW set "PYW=%%~$PATH:P"
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"
if not defined PYW if exist "C:\Python314\pythonw.exe" set "PYW=C:\Python314\pythonw.exe"

if defined PYW (
    start "" "%PYW%" "%~dp0CortechChat.py"
    exit /b 0
)

where python.exe >nul 2>nul
if errorlevel 1 (
    echo   [错误] 未检测到 Python，请先安装 Python 3.14 并勾选 "Add to PATH"。
    echo          下载地址：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo   未找到 pythonw.exe，改用控制台模式启动（关闭本窗口即退出）。
echo.
python "%~dp0CortechChat.py"
pause
