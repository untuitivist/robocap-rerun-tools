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

echo Right-wrist IMU check - read only
echo Help: scripts\README.md (Chinese)
echo Reports and run.log: _artifacts\imu_repair_upload
echo Enter paths without quotes. The report will open when finished.
set "IMU_LOCAL_ROOT=F:\"
set /p "IMU_LOCAL_ROOT=Local source root [F:\, press Enter to use]: "
set "IMU_SHARED_ROOT="
set /p "IMU_SHARED_ROOT=Shared source root (optional, press Enter to skip): "
if defined IMU_SHARED_ROOT goto two_roots
uv run python "%~dp0inspect_right_imu.py" --root "%IMU_LOCAL_ROOT%\." --open-report
goto completed

:two_roots
uv run python "%~dp0inspect_right_imu.py" --root "%IMU_LOCAL_ROOT%\." --root "%IMU_SHARED_ROOT%\." --open-report
goto completed

:arguments
uv run python "%~dp0inspect_right_imu.py" %*

:completed
set "EXIT_CODE=%ERRORLEVEL%"
goto finish

:uv_error
echo uv was not found on PATH. Ask the person who provided this tool to install uv.
set "EXIT_CODE=1"
goto finish

:repo_error
echo Could not open the repository directory. Keep the complete tool folder together.
set "EXIT_CODE=1"
goto exit_script

:finish
popd
:exit_script
echo Right-wrist IMU check exited with code %EXIT_CODE%.
if "%INTERACTIVE%"=="1" pause
exit /b %EXIT_CODE%
