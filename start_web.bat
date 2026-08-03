@echo off
setlocal

set "REPO_DIR=%~dp0"
set "CLI=%REPO_DIR%.venv\Scripts\robocap-rerun.exe"

if not exist "%CLI%" (
  echo Local virtual environment was not found.
  echo Run these commands first:
  echo   uv venv .venv --python 3.11
  echo   .venv\Scripts\activate.bat
  echo   uv pip install -e ".[web]"
  exit /b 1
)

"%CLI%" web --open

