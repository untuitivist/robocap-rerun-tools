@echo on
setlocal
chcp 65001 >nul

set "RRD_PATH=%~1"
set "WEB_PORT=%~2"
set "PYTHON_EXE=%~3"

if "%WEB_PORT%"=="" set "WEB_PORT=0"
if "%PYTHON_EXE%"=="" set "PYTHON_EXE=python"

echo ============================================================
echo Rerun web viewer
echo RRD: %RRD_PATH%
echo Port: %WEB_PORT%
echo Python: %PYTHON_EXE%
echo Time: %DATE% %TIME%
echo ============================================================

if not exist "%RRD_PATH%" (
  echo RRD file does not exist: %RRD_PATH%
  pause
  exit /b 1
)

"%PYTHON_EXE%" -m rerun "%RRD_PATH%" --web-viewer --bind 127.0.0.1 --port auto --web-viewer-port "%WEB_PORT%"
set "EXIT_CODE=%ERRORLEVEL%"
echo Rerun web viewer exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
