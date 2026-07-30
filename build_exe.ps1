$ErrorActionPreference = "Stop"

$PackageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Source = Join-Path $PackageDir "blender_render_watchdog.py"
$BuildDir = Join-Path $PackageDir "build"
$DistDir = Join-Path $PackageDir "dist"
$ExePath = Join-Path $DistDir "BlenderRenderWatchdog.exe"

if (-not (Test-Path -LiteralPath $Source)) {
    throw "Source file was not found: $Source"
}

$pyinstaller = Get-Command pyinstaller.exe -ErrorAction SilentlyContinue
if (-not $pyinstaller) {
    Write-Host "PyInstaller was not found. Installing it with pip..."
    python -m pip install pyinstaller
}

if (Test-Path -LiteralPath $BuildDir) {
    Remove-Item -LiteralPath $BuildDir -Recurse -Force
}
if (Test-Path -LiteralPath $DistDir) {
    Remove-Item -LiteralPath $DistDir -Recurse -Force
}

pyinstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onefile `
    --name "BlenderRenderWatchdog" `
    --distpath "$DistDir" `
    --workpath "$BuildDir" `
    "$Source"

if (-not (Test-Path -LiteralPath $ExePath)) {
    throw "Build finished, but exe was not found: $ExePath"
}

Write-Host ""
Write-Host "EXE created:"
Write-Host $ExePath
