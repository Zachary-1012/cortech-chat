@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Chat Cortech · 停止

echo.
echo   Chat Cortech · 正在停止后台服务...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*monitor.app*' }; if ($procs) { $procs | ForEach-Object { Write-Host ('  结束进程 PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Host '  没有正在运行的服务' }"

echo.
echo   已停止。
echo.
pause
