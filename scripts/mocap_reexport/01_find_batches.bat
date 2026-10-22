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
echo 查找之前动捕问题涉及的 NOKOV 批次，只生成报告，不修改数据。
echo 自动搜索本机盘和可访问共享，不限 F 盘；无需选择 Markdown 或填写路径。
echo 扫描期间请保持窗口打开。报告内可复制每个批次路径或全部路径。
uv run python -m robocap_rerun_tools.mocap_reexport report --open-report %*
set "EXIT_CODE=%ERRORLEVEL%"
goto finish
:uv_error
echo 未找到 uv，请让提供工具的人先安装 uv。
set "EXIT_CODE=2"
goto finish
:repo_error
echo 无法进入工具目录，请保留完整工具文件夹。
set "EXIT_CODE=2"
goto exit_script
:finish
popd
:exit_script
echo 已结束。退出码 %EXIT_CODE%：0=匹配完整，1=有未找到/重复/搜索失败，2=运行失败。
if "%INTERACTIVE%"=="1" pause
exit /b %EXIT_CODE%
