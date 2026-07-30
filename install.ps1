$ErrorActionPreference = "Stop"

$AppName = "Blender Render Watchdog"
$PackageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $env:LOCALAPPDATA "BlenderRenderWatchdog"
$DesktopDir = [Environment]::GetFolderPath("Desktop")
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Blender Render Watchdog"
$AppFile = Join-Path $InstallDir "blender_render_watchdog.py"
$SourceFiles = @(
    "blender_render_watchdog.py",
    "auto_fix.py",
    "glass_ui.py",
    "localization.py",
    "mobile_dashboard.py",
    "network_render.py",
    "render_analytics.py",
    "render_queue.py",
    "render_sandbox.py",
    "video_tools.py"
)
$ShortcutPath = Join-Path $DesktopDir "$AppName.lnk"
$StartMenuShortcut = Join-Path $StartMenuDir "$AppName.lnk"
$UninstallShortcut = Join-Path $StartMenuDir "Uninstall $AppName.lnk"
$PythonDownloadUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"

function Find-Python {
    $commands = @(
        @{ File = "py.exe"; Args = @("-3", "-c", "import sys; print(sys.executable)") },
        @{ File = "python.exe"; Args = @("-c", "import sys; print(sys.executable)") }
    )

    foreach ($command in $commands) {
        try {
            $result = & $command.File @($command.Args) 2>$null
            if ($LASTEXITCODE -eq 0 -and $result) {
                $path = $result | Select-Object -First 1
                if (Test-Path -LiteralPath $path) {
                    return $path
                }
            }
        } catch {
        }
    }

    $localPythonRoot = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path -LiteralPath $localPythonRoot) {
        $localPython = Get-ChildItem -LiteralPath $localPythonRoot -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($localPython) {
            return $localPython.FullName
        }
    }

    return $null
}

function Install-Python {
    Write-Host "Python was not found. Installing Python 3..."

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "Trying winget install..."
        & winget.exe install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
        $pythonAfterWinget = Find-Python
        if ($pythonAfterWinget) {
            return $pythonAfterWinget
        }
    }

    Write-Host "Downloading Python installer..."
    $installer = Join-Path $env:TEMP "python-3-watchdog-installer.exe"
    Invoke-WebRequest -Uri $PythonDownloadUrl -OutFile $installer -UseBasicParsing

    Write-Host "Installing Python for current user..."
    $arguments = "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1"
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Python installer failed with exit code $($process.ExitCode)."
    }

    $pythonAfterInstall = Find-Python
    if (-not $pythonAfterInstall) {
        throw "Python was installed, but python.exe was not found. Restart Windows or install Python manually."
    }

    return $pythonAfterInstall
}

function Get-GuiPythonPath($PythonPath) {
    $pythonw = Join-Path (Split-Path -Parent $PythonPath) "pythonw.exe"
    if (Test-Path -LiteralPath $pythonw) {
        return $pythonw
    }

    return $PythonPath
}

function New-Shortcut($Path, $TargetPath, $Arguments, $WorkingDirectory, $Description) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $TargetPath
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.Description = $Description
    $shortcut.IconLocation = "C:\Windows\System32\shell32.dll,167"
    $shortcut.Save()
}

foreach ($sourceFile in $SourceFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $PackageDir $sourceFile))) {
        throw "Application source file was not found: $sourceFile"
    }
}

$python = Find-Python
if (-not $python) {
    $python = Install-Python
}

$guiPython = Get-GuiPythonPath $python

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $StartMenuDir | Out-Null

foreach ($sourceFile in $SourceFiles) {
    Copy-Item -LiteralPath (Join-Path $PackageDir $sourceFile) -Destination (Join-Path $InstallDir $sourceFile) -Force
}
Copy-Item -LiteralPath (Join-Path $PackageDir "uninstall.ps1") -Destination (Join-Path $InstallDir "uninstall.ps1") -Force

$launcher = @"
@echo off
cd /d "%~dp0"
"$python" "%~dp0blender_render_watchdog.py"
echo.
echo Press any key to close this window.
pause >nul
"@
$launcher | Set-Content -LiteralPath (Join-Path $InstallDir "debug_start.bat") -Encoding ASCII

New-Shortcut `
    -Path $ShortcutPath `
    -TargetPath $guiPython `
    -Arguments "`"$AppFile`"" `
    -WorkingDirectory $InstallDir `
    -Description "Start Blender Render Watchdog"

New-Shortcut `
    -Path $StartMenuShortcut `
    -TargetPath $guiPython `
    -Arguments "`"$AppFile`"" `
    -WorkingDirectory $InstallDir `
    -Description "Start Blender Render Watchdog"

New-Shortcut `
    -Path $UninstallShortcut `
    -TargetPath "powershell.exe" `
    -Arguments "-NoProfile -ExecutionPolicy Bypass -File `"$InstallDir\uninstall.ps1`"" `
    -WorkingDirectory $InstallDir `
    -Description "Uninstall Blender Render Watchdog"

Write-Host ""
Write-Host "$AppName installed successfully."
Write-Host "Install folder: $InstallDir"
Write-Host "Desktop shortcut: $ShortcutPath"
