@echo off
chcp 65001 >nul
title RenderW.dog Update
echo Checking RenderW.dog updates...
"%~dp0dist\RenderW.dog.exe" --check-update --install-update
echo.
pause
