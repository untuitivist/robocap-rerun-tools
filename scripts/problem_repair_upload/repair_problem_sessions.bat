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
echo 使用工具内置问题清单，无需选择文件或输入路径。
echo 自动搜索本机所有盘，并发现当前账号能访问的网络共享。
echo 找到有效原件会自动补传，校验远端 SHA-256 后才算解决。
echo 原件会保留；缺数据、配对和量程问题不会自动猜测修补。
echo 请保持窗口打开，结果和日志位于 _artifacts\problem_repair_upload。
uv run python "%~dp0repair_problem_sessions.py" --auto-roots --discover-shares --apply --open-report
goto completed

:arguments
uv run python "%~dp0repair_problem_sessions.py" %*
:completed
set "EXIT_CODE=%ERRORLEVEL%"
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
