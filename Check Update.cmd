@echo off
chcp 65001 >nul
title Blender Render Watchdog Update
echo Checking Blender Render Watchdog updates...
"%~dp0dist\BlenderRenderWatchdog.exe" --check-update --install-update
echo.
pause
