@echo on
setlocal
chcp 65001 >nul

set "REPO_DIR=%~dp0"
set "CLI=%REPO_DIR%.venv\Scripts\robocap-rerun.exe"
set "NO_PROXY=127.0.0.1,localhost,%NO_PROXY%,%no_proxy%"
set "no_proxy=%NO_PROXY%"

cd /d "%REPO_DIR%"

where uv >nul 2>&1
if errorlevel 1 (
  echo uv was not found on PATH.
  echo Install uv first, then run start_web.bat again.
  exit /b 1
)

echo ============================================================
echo Robocap Rerun Tools web launcher
echo Repo: %CD%
echo Time: %DATE% %TIME%
echo uv:
uv --version
echo Python:
if exist "%REPO_DIR%.venv\Scripts\python.exe" "%REPO_DIR%.venv\Scripts\python.exe" --version
echo CLI: %CLI%
echo ============================================================

if /i not "%ROBOCAP_SKIP_SYNC%"=="1" (
  echo Synchronizing Python, Web, FFmpeg, and FFprobe dependencies with uv...
  uv sync --extra web
  if errorlevel 1 (
    echo uv sync failed.
    exit /b 1
  )
)

if not exist "%CLI%" (
  echo uv sync completed, but the CLI was not created: %CLI%
  exit /b 1
)

"%CLI%" web --open
set "EXIT_CODE=%ERRORLEVEL%"
echo Web process exited with code %EXIT_CODE%.
exit /b %EXIT_CODE%
