@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Monitor.ps1" %*
pause
