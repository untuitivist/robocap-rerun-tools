@echo off
setlocal DisableDelayedExpansion
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "INTERACTIVE=0"
if "%~1"=="" set "INTERACTIVE=1"
pushd "%~dp0..\.."
if errorlevel 1 goto repo_error
where uv >nul 2>&1
if errorlevel 1 goto uv_error
if "%INTERACTIVE%"=="0" goto arguments

echo Repair uploads from a corruption CSV
echo Paths may be local, mapped drives, or UNC shares.
echo Enter paths without surrounding quotes. Logs are saved under _artifacts\repair_upload.
set "REPORT_CSV=%~dp0corrupt_sessions_20260924.csv"
echo Default report: corrupt_sessions_20260924.csv bundled with this launcher.
set /p "REPORT_CSV=Corruption CSV path [Enter uses bundled report]: "
if not exist "%REPORT_CSV%" goto csv_error
set "LOCAL_ROOT=F:\"
set /p "LOCAL_ROOT=Local search root [F:\]: "
set "SHARED_ROOT="
set /p "SHARED_ROOT=Shared search root (optional, e.g. \\SERVER\F): "
set "REPAIR_MODE="
set /p "REPAIR_MODE=Type UPLOAD to validate and overwrite reported files; Enter previews only: "
set "APPLY_ARG="
if /i "%REPAIR_MODE%"=="UPLOAD" set "APPLY_ARG=--apply"
if defined SHARED_ROOT goto two_roots
uv run python "%~dp0reupload_corrupt_sessions.py" --csv "%REPORT_CSV%" --root "%LOCAL_ROOT%\." %APPLY_ARG%
goto completed

:two_roots
uv run python "%~dp0reupload_corrupt_sessions.py" --csv "%REPORT_CSV%" --root "%LOCAL_ROOT%\." --root "%SHARED_ROOT%\." %APPLY_ARG%
goto completed

:arguments
uv run python "%~dp0reupload_corrupt_sessions.py" %*

:completed
set "EXIT_CODE=%ERRORLEVEL%"
goto finish

:csv_error
echo Corruption CSV file not found: "%REPORT_CSV%"
set "EXIT_CODE=1"
goto finish

:uv_error
echo uv was not found on PATH. Install uv before running this launcher.
set "EXIT_CODE=1"
goto finish

:repo_error
echo Could not open the repository directory.
set "EXIT_CODE=1"
goto exit_script

:finish
popd
:exit_script
echo Repair upload exited with code %EXIT_CODE%.
if "%INTERACTIVE%"=="1" pause
exit /b %EXIT_CODE%
