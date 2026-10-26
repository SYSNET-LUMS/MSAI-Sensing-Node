@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run Setup.cmd first.
  pause
  exit /b 1
)
if not exist "dist\index.html" (
  echo The built dashboard is missing. Restore dist or rebuild with npm ci and npm run build.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B backend\check_startup.py
if errorlevel 2 exit /b 0
if errorlevel 1 (
  pause
  exit /b 1
)
echo Dashboard: http://127.0.0.1:8000
echo Node receiver: TCP 5443; MSAI_TCP_PORT can override it.
echo For BLE, select BLE in the dashboard and connect the base station COM port.
echo Keep this window open. Ctrl+C stops reception and closes recordings.
set PYTHONDONTWRITEBYTECODE=1
".venv\Scripts\python.exe" -m uvicorn backend.server:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
if errorlevel 1 pause
