# Pack Cortech Chat Xiaohongshu helper for Windows (zip root = extension files).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $root "cortech-xhs-assist"
$out = Join-Path $root "cortech-xhs-assist.zip"
if (-not (Test-Path -LiteralPath (Join-Path $src "manifest.json"))) {
    Write-Error "Missing $src"
}
if (Test-Path -LiteralPath $out) {
    Remove-Item -LiteralPath $out -Force
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($src, $out)
Write-Output "Wrote $out"
