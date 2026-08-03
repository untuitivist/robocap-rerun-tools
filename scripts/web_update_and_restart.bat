@echo on
setlocal
chcp 65001 >nul

set "REPO_DIR=%~dp0.."
set "OLD_PID=%~1"
set "MODE=%~2"

pushd "%REPO_DIR%"
echo ============================================================
echo Robocap Rerun Tools web update and restart
echo Repo: %CD%
echo Old web PID: %OLD_PID%
echo Mode: %MODE%
echo Time: %DATE% %TIME%
echo ============================================================

if not "%OLD_PID%"=="" (
  echo Waiting before closing the old web process...
  timeout /t 2 /nobreak
  echo Closing old web process tree...
  taskkill /PID %OLD_PID% /T /F
)

if /I "%MODE%"=="pull" (
  echo Checking working tree before pull...
  git status --short
  for /f "delims=" %%A in ('git status --short') do set "DIRTY=1"
  if defined DIRTY (
    echo Working tree is not clean. Commit or stash changes before web update.
    echo Update aborted. Press any key to close this window.
    pause >nul
    exit /b 1
  )

  echo Fetching remote changes...
  git fetch --prune
  if errorlevel 1 goto failed

  echo Pulling latest code...
  git pull --ff-only
  if errorlevel 1 goto failed
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
