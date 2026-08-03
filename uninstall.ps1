$ErrorActionPreference = "SilentlyContinue"

$AppName = "Blender Render Watchdog"
$InstallDir = Join-Path $env:LOCALAPPDATA "BlenderRenderWatchdog"
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk"
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Blender Render Watchdog"
$ResumeStartupFile = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\BlenderRenderWatchdog-Resume.cmd"

Remove-Item -LiteralPath $DesktopShortcut -Force
Remove-Item -LiteralPath $StartMenuDir -Recurse -Force
Remove-Item -LiteralPath $ResumeStartupFile -Force
Remove-Item -LiteralPath $InstallDir -Recurse -Force

Write-Host "$AppName was removed."
