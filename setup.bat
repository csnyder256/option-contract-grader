@echo off
REM ============================================================
REM  Options Finder - one-time SETUP
REM  Creates an isolated Python environment (.venv) and installs
REM  the dependencies. Double-click this once before start.bat.
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Options Finder - Setup
echo ============================================================
echo.

REM --- Find a Python launcher ---------------------------------
set "PYLAUNCH="
where py >nul 2>nul && set "PYLAUNCH=py -3"
if not defined PYLAUNCH (
  where python >nul 2>nul && set "PYLAUNCH=python"
)
if not defined PYLAUNCH (
  echo [ERROR] Python was not found.
  echo Install Python 3.10+ from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during install, then re-run setup.
  echo.
  pause
  exit /b 1
)
echo Using Python: %PYLAUNCH%
echo.

REM --- Create the virtual environment -------------------------
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment in .venv ...
  %PYLAUNCH% -m venv .venv
) else (
  echo Virtual environment already exists - reusing it.
)
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Could not create the virtual environment.
  echo.
  pause
  exit /b 1
)

REM --- Install dependencies -----------------------------------
echo.
echo Installing dependencies, this can take a minute ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] Dependency installation failed. Check your internet connection.
  echo.
  pause
  exit /b 1
)

REM --- Create .env from the template if missing ---------------
if not exist ".env" if exist ".env.example" copy /y ".env.example" ".env" >nul
echo.
echo Config ready - using the FREE CBOE data source by default, no account needed.

echo.
echo ============================================================
echo   Setup complete!  Next: double-click  start.bat
echo ============================================================
echo.
pause
