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
if "%INTERACTIVE%"=="0" goto arguments

echo 问题 Session 搜索、检查、补传
echo 会搜索本机所有本地盘和映射盘。未映射的共享目录可在下面补充。
echo 找到有效原件会自动补传，校验远端 SHA-256 后才算解决。
echo 原件会保留；缺数据、配对和量程问题不会自动猜测修补。
echo 请保持窗口打开，结果和日志位于 _artifacts\problem_repair_upload。
echo 路径直接粘贴，不用添加引号。
set "PROBLEM_REPORT=%~dp0problematic_sessions.md"
set /p "PROBLEM_REPORT=问题 Markdown 路径 [回车使用本 BAT 旁的 problematic_sessions.md]: "
if not exist "%PROBLEM_REPORT%" goto report_error
set "PROBLEM_SHARE="
set /p "PROBLEM_SHARE=额外共享目录 [例如 \\SERVER\DATA，无则回车]: "
if defined PROBLEM_SHARE goto with_share
uv run python "%~dp0repair_problem_sessions.py" --report "%PROBLEM_REPORT%" --auto-roots --apply --open-report
goto completed

:with_share
uv run python "%~dp0repair_problem_sessions.py" --report "%PROBLEM_REPORT%" --auto-roots --root "%PROBLEM_SHARE%\." --apply --open-report
goto completed

:arguments
uv run python "%~dp0repair_problem_sessions.py" %*
:completed
set "EXIT_CODE=%ERRORLEVEL%"
goto finish
:report_error
echo 找不到问题 Markdown：%PROBLEM_REPORT%
set "EXIT_CODE=2"
goto finish
:uv_error
echo 未找到 uv，请让提供工具的人先安装 uv。
set "EXIT_CODE=2"
goto finish
:repo_error
echo 无法进入工具根目录，请保留完整工具文件夹。
set "EXIT_CODE=2"
goto exit_script
:finish
popd
:exit_script
echo 已结束，退出码 %EXIT_CODE%。0=全部解决，1=还有待处理或搜索不完整，2=配置问题。
if "%INTERACTIVE%"=="1" pause
exit /b %EXIT_CODE%
