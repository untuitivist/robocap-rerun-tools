@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "INTERACTIVE=0"
if "%~1"=="" set "INTERACTIVE=1"
pushd "%~dp0..\.."
if errorlevel 1 goto repo_error
where uv >nul 2>&1
if errorlevel 1 goto uv_error
echo Starting NOKOV batch search. No source files will be changed.
echo Please keep this window open. Progress and errors are logged.
uv run python -m robocap_rerun_tools.mocap_reexport report --open-report %*
set "EXIT_CODE=%ERRORLEVEL%"
goto finish
:uv_error
echo uv was not found. Please install uv before running this tool.
set "EXIT_CODE=2"
goto finish
:repo_error
echo Cannot enter repository. Keep the complete tool folder.
set "EXIT_CODE=2"
goto exit_script
:finish
popd
:exit_script
echo Exit code %EXIT_CODE%: 0=complete, 1=needs review, 2=failed.
if "%INTERACTIVE%"=="1" pause
exit /b %EXIT_CODE%
