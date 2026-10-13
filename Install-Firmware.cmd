@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Flash.ps1" %*
pause
