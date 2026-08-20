@echo on
setlocal
chcp 65001 >nul

set "OLD_PID=%~1"
set "UPDATE_MODE=%~2"
set "REPO_DIR=%~3"

if "%UPDATE_MODE%"=="" set "UPDATE_MODE=dependencies"
if "%REPO_DIR%"=="" (
  echo Repository directory argument is required.
  pause
  exit /b 2
)

pushd "%REPO_DIR%"
echo ============================================================
echo Robocap Rerun Tools update and restart
echo Repo: %CD%
echo Old web PID: %OLD_PID%
echo Update mode: %UPDATE_MODE%
echo Time: %DATE% %TIME%
echo ============================================================

if /i "%UPDATE_MODE%"=="code" (
  echo Checking Git working tree before stopping the web process...
  git status --porcelain >nul 2>&1
  if errorlevel 1 goto git_failed
  for /f "delims=" %%A in ('git status --porcelain') do goto dirty_worktree
  echo Fetching origin before stopping the web process...
  git fetch --prune origin
  if errorlevel 1 goto git_failed
)

if not "%OLD_PID%"=="" (
  echo Waiting before closing the old web process...
  timeout /t 2 /nobreak
  echo Closing old web process tree...
  taskkill /PID %OLD_PID% /T /F
)

if /i "%UPDATE_MODE%"=="code" (
  echo Pulling code with fast-forward-only policy...
  git pull --ff-only
  if errorlevel 1 goto failed_and_restart
)

echo Installing/updating web dependencies...
uv sync --extra web
if errorlevel 1 goto failed_and_restart

echo Restarting web UI...
set "ROBOCAP_SKIP_SYNC=1"
call "%REPO_DIR%\start_web.bat"
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

:dirty_worktree
echo Working tree is not clean. Commit or stash local changes before updating code.
echo No process was stopped and no files were changed.
git status --short --branch
echo Press any key to close this window.
pause >nul
popd
exit /b 3

:git_failed
echo Git preflight failed with errorlevel %ERRORLEVEL%.
echo No process was stopped and no files were changed.
echo Press any key to close this window.
pause >nul
popd
exit /b 4

:failed_and_restart
echo Update failed with errorlevel %ERRORLEVEL%.
echo Restarting the existing working tree so the web UI remains available...
set "ROBOCAP_SKIP_SYNC=1"
call "%REPO_DIR%\start_web.bat"
popd
exit /b 1
