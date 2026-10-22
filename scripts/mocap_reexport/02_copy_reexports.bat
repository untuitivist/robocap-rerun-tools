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
echo 请先在 NOKOV 中完成重导出并关闭导出任务。
echo 自动重新搜索磁盘，将唯一匹配且检查通过的导出复制回对应 Session。
echo 源文件保留；旧动捕目录保留在 Session 同级的 _artifacts 下，不混入数据。
echo 重复、缺文件、无效内容会跳过并记录。不会自动上传远端。
uv run python -m robocap_rerun_tools.mocap_reexport copy --open-report %*
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
echo 已结束。退出码 %EXIT_CODE%：0=全部复制通过，1=还有待处理，2=运行失败。
if "%INTERACTIVE%"=="1" pause
exit /b %EXIT_CODE%
