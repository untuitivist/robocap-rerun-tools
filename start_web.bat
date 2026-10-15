@echo on
setlocal
chcp 65001 >nul

set "REPO_DIR=%~dp0"
set "CLI=%REPO_DIR%.venv\Scripts\robocap-rerun.exe"
set "NO_PROXY=127.0.0.1,localhost,%NO_PROXY%,%no_proxy%"
set "no_proxy=%NO_PROXY%"

cd /d "%REPO_DIR%"

if "%ROBOCAP_LAUNCHER_UPDATED%"=="1" goto updated
if "%ROBOCAP_SKIP_UPDATE%"=="1" goto updated
where git >nul 2>&1
if errorlevel 1 (
  echo WARNING: Git was not found. Skipping update and starting with local code.
  goto updated
)
if not exist "%REPO_DIR%.venv\Scripts\python.exe" (
  echo WARNING: Local Python is not ready. Skipping update for initial setup.
  goto updated
)
"%REPO_DIR%.venv\Scripts\python.exe" "%REPO_DIR%scripts\startup_update.py"
if errorlevel 1 (
  echo WARNING: Git update failed. Skipping update and starting with local code.
  goto updated
)
rem Reload the launcher in case the pull updated this file.
set "ROBOCAP_LAUNCHER_UPDATED=1"
call "%~f0"
exit /b %ERRORLEVEL%

:updated
set "ROBOCAP_LAUNCHER_UPDATED="

where uv >nul 2>&1
if not errorlevel 1 goto uv_ready
rem Also recognize an existing install not yet visible in this CMD's PATH.
set "PATH=%USERPROFILE%\.local\bin;%UV_INSTALL_DIR%;%PATH%"
where uv >nul 2>&1
if not errorlevel 1 goto uv_ready
echo uv was not found. Installing uv automatically...
powershell -NoProfile -ExecutionPolicy ByPass -File "%REPO_DIR%scripts\install_uv.ps1"
if errorlevel 1 (
  echo ERROR: uv could not be installed. Enable your VPN/proxy and retry.
  exit /b 1
)
where uv >nul 2>&1
if errorlevel 1 (
  echo ERROR: uv is still not on PATH. Open a new CMD window and run start_web.bat again.
  exit /b 1
)

:uv_ready
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
  uv sync --python 3.12 --extra web
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
