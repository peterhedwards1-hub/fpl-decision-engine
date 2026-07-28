@echo off
setlocal
cd /d "%~dp0"

set "SEASON_CODE=2026-27"
set "SEASON_NAME=2026/27"

where py >nul 2>&1
if errorlevel 1 (
  echo Python 3.12 or newer is required.
  echo Install Python from python.org and tick "Add Python to PATH".
  pause
  exit /b 1
)

py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 12))"
if errorlevel 1 (
  echo Python 3.12 or newer is required.
  pause
  exit /b 1
)

set "PYTHONPATH=%CD%\src"
py -3 -m fpl_engine.live.cli ^
  --database data\fpl.sqlite3 ^
  --archive-root data\raw\fpl ^
  --report-root data\reports\fpl ^
  --season-code "%SEASON_CODE%" ^
  --season-name "%SEASON_NAME%" ^
  --open-report

if errorlevel 1 (
  echo.
  echo Collection failed. Review the message above.
  pause
  exit /b 1
)

echo.
echo Collection complete. The verification report should now be open.
pause
