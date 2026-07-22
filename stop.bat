@echo off
REM ============================================================
REM  Options Finder - STOP
REM  Force-stops the server if Ctrl+C in the start window isn't
REM  available (e.g. you closed it or it got stuck).
REM ============================================================
setlocal
set "PORT=8000"

echo Stopping anything listening on port %PORT% ...
set "FOUND="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  echo   killing process %%P
  taskkill /F /PID %%P >nul 2>nul
  set "FOUND=1"
)

if not defined FOUND (
  echo Nothing was running on port %PORT%.
) else (
  echo Stopped.
)
echo.
pause
