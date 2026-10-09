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

echo Object Mocap file inventory - read only
echo CSV and HTML reports plus run.log are saved under _artifacts\obj_repair_upload.
echo Source roots can be local, mapped drives or UNC shares. Enter paths without quotes.
set "OBJECT_LOCAL_ROOT=F:\"
set /p "OBJECT_LOCAL_ROOT=Local source root [F:\]: "
set "OBJECT_SHARED_ROOT="
set /p "OBJECT_SHARED_ROOT=Shared source root (optional, e.g. \\SERVER\F): "
if defined OBJECT_SHARED_ROOT goto two_roots
uv run python "%~dp0inspect_object_mocap.py" --root "%OBJECT_LOCAL_ROOT%\."
goto completed

:two_roots
uv run python "%~dp0inspect_object_mocap.py" --root "%OBJECT_LOCAL_ROOT%\." --root "%OBJECT_SHARED_ROOT%\."
goto completed

:arguments
uv run python "%~dp0inspect_object_mocap.py" %*

:completed
set "EXIT_CODE=%ERRORLEVEL%"
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
echo Object inventory exited with code %EXIT_CODE%.
if "%INTERACTIVE%"=="1" pause
exit /b %EXIT_CODE%
