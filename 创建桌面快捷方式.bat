@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Chat Cortech · 创建桌面快捷方式

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = '%~dp0'.TrimEnd('\');" ^
  "$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source;" ^
  "if (-not $pyw) { $c = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\pythonw.exe'; if (Test-Path $c) { $pyw = $c } }" ^
  "if (-not $pyw -and (Test-Path 'C:\Python314\pythonw.exe')) { $pyw = 'C:\Python314\pythonw.exe' }" ^
  "if (-not $pyw) { Write-Host '  未找到 pythonw.exe，请先安装 Python 3.14。'; exit 1 }" ^
  "$ico = Join-Path $root 'frontend\dist\favicon.ico';" ^
  "$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Chat Cortech.lnk';" ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$s = $ws.CreateShortcut($lnk);" ^
  "$s.TargetPath = $pyw;" ^
  "$s.Arguments = '\"' + (Join-Path $root 'CortechChat.py') + '\"';" ^
  "$s.WorkingDirectory = $root;" ^
  "$s.Description = 'Chat Cortech · 社媒舆情运营 SaaS';" ^
  "if (Test-Path $ico) { $s.IconLocation = $ico };" ^
  "$s.Save();" ^
  "Write-Host ('  桌面快捷方式已创建：' + $lnk)"

echo.
pause
