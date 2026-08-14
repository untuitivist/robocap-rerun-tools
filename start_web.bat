@echo on
setlocal
chcp 65001 >nul

set "REPO_DIR=%~dp0"
set "CLI=%REPO_DIR%.venv\Scripts\robocap-rerun.exe"
set "NO_PROXY=127.0.0.1,localhost,%NO_PROXY%,%no_proxy%"
set "no_proxy=%NO_PROXY%"

cd /d "%REPO_DIR%"

echo ============================================================
echo Robocap Rerun Tools web launcher
echo Repo: %CD%
echo Time: %DATE% %TIME%
echo Python:
if exist "%REPO_DIR%.venv\Scripts\python.exe" "%REPO_DIR%.venv\Scripts\python.exe" --version
echo CLI: %CLI%
echo ============================================================

if not exist "%CLI%" (
  echo Local virtual environment was not found.
  echo Run these commands first:
  echo   uv venv .venv --python 3.11
  echo   .venv\Scripts\activate.bat
  echo   uv pip install -e ".[web]"
  exit /b 1
)

"%CLI%" web --open
set "EXIT_CODE=%ERRORLEVEL%"
echo Web process exited with code %EXIT_CODE%.
exit /b %EXIT_CODE%
