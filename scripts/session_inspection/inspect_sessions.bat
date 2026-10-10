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

echo Full Session check - structure, contents, frame counts and timestamps
echo Videos are fully decoded. Please leave this window open until finished.
echo Help: scripts\session_inspection\README.md (Chinese)
echo Mocap rules: Body0=TRC/BVH/CSV, Tracker0=TRC/CSV; HH needs 2 or more bodies.
echo Extra bodies and props: scripts\session_inspection\mocap_requirements.csv
echo Reports and run.log: _artifacts\session_inspection
echo Enter paths without quotes.
set "SESSION_LOCAL_ROOT=F:\"
set /p "SESSION_LOCAL_ROOT=Local source root [F:\, press Enter to use]: "
set "SESSION_SHARED_ROOT="
set /p "SESSION_SHARED_ROOT=Shared source root (optional, press Enter to skip): "
set "SESSION_RATIO=8"
set /p "SESSION_RATIO=Mocap rate: 8=240 FPS, 4=120 FPS [8]: "
if not "%SESSION_RATIO%"=="8" if not "%SESSION_RATIO%"=="4" goto ratio_error
if defined SESSION_SHARED_ROOT goto two_roots
uv run python "%~dp0inspect_sessions.py" --root "%SESSION_LOCAL_ROOT%\." --mocap-ratio %SESSION_RATIO% --open-report
goto completed

:two_roots
uv run python "%~dp0inspect_sessions.py" --root "%SESSION_LOCAL_ROOT%\." --root "%SESSION_SHARED_ROOT%\." --mocap-ratio %SESSION_RATIO% --open-report
goto completed

:arguments
uv run python "%~dp0inspect_sessions.py" %*

:completed
set "EXIT_CODE=%ERRORLEVEL%"
goto finish
:uv_error
echo uv was not found. Ask the person who provided this tool to install uv.
set "EXIT_CODE=2"
goto finish
:ratio_error
echo Mocap rate must be 8 for 240 FPS or 4 for 120 FPS. Please run again.
set "EXIT_CODE=2"
goto finish
:repo_error
echo Could not open the repository. Keep the complete tool folder together.
set "EXIT_CODE=2"
goto exit_script
:finish
popd
:exit_script
echo Session inspection exited with code %EXIT_CODE%.
echo 0 = file checks passed; 1 = issues to review; 2 = setup or parameter error.
if "%INTERACTIVE%"=="1" pause
exit /b %EXIT_CODE%
