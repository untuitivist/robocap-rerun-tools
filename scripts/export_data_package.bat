@echo off
setlocal

if "%~1"=="" (
  echo Usage: scripts\export_data_package.bat SESSION_DIR [OUTPUT_ZIP] [SEGMENT]
  exit /b 2
)

set "SCRIPT_DIR=%~dp0"
set "REPO_DIR=%SCRIPT_DIR%.."
set "CLI=%REPO_DIR%\.venv\Scripts\robocap-rerun.exe"
if not exist "%CLI%" set "CLI=robocap-rerun"

set "SESSION_DIR=%~1"
set "OUTPUT_ZIP=%~2"
set "SEGMENT=%~3"

if "%OUTPUT_ZIP%"=="" (
  if "%SEGMENT%"=="" (
    "%CLI%" package-data "%SESSION_DIR%"
  ) else (
    "%CLI%" package-data "%SESSION_DIR%" --segment "%SEGMENT%"
  )
) else (
  if "%SEGMENT%"=="" (
    "%CLI%" package-data "%SESSION_DIR%" --output "%OUTPUT_ZIP%"
  ) else (
    "%CLI%" package-data "%SESSION_DIR%" --output "%OUTPUT_ZIP%" --segment "%SEGMENT%"
  )
)
