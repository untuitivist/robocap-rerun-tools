@echo on
setlocal
chcp 65001 >nul

set "REPO_DIR=%~dp0.."
set "OLD_PID=%~1"

pushd "%REPO_DIR%"
echo ============================================================
echo Robocap Rerun Tools web dependency update and restart
echo Repo: %CD%
echo Old web PID: %OLD_PID%
echo Time: %DATE% %TIME%
echo ============================================================

if not "%OLD_PID%"=="" (
  echo Waiting before closing the old web process...
  timeout /t 2 /nobreak
  echo Closing old web process tree...
  taskkill /PID %OLD_PID% /T /F
)

echo Installing/updating web dependencies...
uv pip install -e ".[web]"
if errorlevel 1 goto failed

echo Restarting web UI...
call "%REPO_DIR%\start_web.bat"
popd
exit /b 0

:failed
echo Update failed with errorlevel %ERRORLEVEL%.
echo Press any key to close this window.
pause >nul
popd
exit /b 1
