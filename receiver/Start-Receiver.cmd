@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run Setup-Receiver.cmd first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe receiver.py serve --bind 0.0.0.0 --port 5443
pause
