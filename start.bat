@echo off
REM ============================================================
REM  Options Finder - START
REM  Launches the server and opens it in your browser.
REM  Leave this window open while you use the app.
REM  To stop: press Ctrl+C in this window, or run stop.bat.
REM ============================================================
setlocal
cd /d "%~dp0"
set "PORT=8000"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Not set up yet. Double-click  setup.bat  first.
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo   Options Finder - starting on http://localhost:%PORT%
echo   Your browser will open in a few seconds.
echo   Keep this window open. Press Ctrl+C (or run stop.bat) to quit.
echo ============================================================
echo.

REM Open the browser ~3s after launch, without blocking the server.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://localhost:%PORT%/'"

REM Run the server in the foreground (Ctrl+C stops it).
".venv\Scripts\python.exe" -m uvicorn app.api:app --host 127.0.0.1 --port %PORT%

echo.
echo Server stopped.
pause
