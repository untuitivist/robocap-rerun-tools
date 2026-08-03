# Robocap Rerun Tools 中文说明

这个仓库用于检查 Robocap/NOKOV session 的帧率、时间线和 offset，并生成 Rerun `.rrd` 文件。核心目标是把 Robocap 视频、传感器数据、NOKOV BVH/TRC/CSV 骨骼数据、第三人称视频和可选 MANO 手部 mesh 放到同一个 `capture_time` 时间线上。

## 适用数据

当前主要面向类似下面的 session：

```text
Z:\DATASETS\Frodobots\nokov\20260707_083023_session48
```

常见内容包括：

- Robocap 多视角视频：left/right、eye、front、wrist。
- Robocap sensor CSV。
- `test*` 子目录里的 NOKOV 数据：第三人称视频、BVH、TRC、CSV、手部轨迹。
- 可选 MANO 模型：`MANO_LEFT.pkl`、`MANO_RIGHT.pkl`。

如果某些视频或数据缺失，导出脚本会尽量用文字占位，不应该因为少一个流就整体失败。

## 第一次 Clone

如果只是下载代码，建议用 HTTPS，不需要 SSH key：

```bat
git clone https://github.com/frodobots-org/robocap-rerun-tools.git
cd robocap-rerun-tools
```

如果组织仓库是 private，clone 时需要 GitHub 账号权限。推荐安装 GitHub CLI 后用浏览器登录：

```bat
winget install GitHub.cli
gh auth login
```

登录时选择：

```text
GitHub.com
HTTPS
Login with a web browser
```

不要把 token 写进 clone URL，也不要把个人 token 发给别人。

## 安装环境

需要先准备：

- `uv`
- Python 3.11
- `ffmpeg` 和 `ffprobe`

在仓库目录里创建虚拟环境：

```bat
uv venv .venv --python 3.11
.venv\Scripts\activate.bat
uv pip install -e .
```

如果要跑测试或开发：

```bat
uv pip install -e ".[dev]"
```

如果要使用本地网页：

```bat
uv pip install -e ".[web]"
```

检查 CLI 是否可用：

```bat
robocap-rerun --help
```

## 常用命令

启动本地 Web UI：

```bat
robocap-rerun web --open
```

默认地址：

```text
http://127.0.0.1:7860
```

网页里可以做：

- inspect 帧率和异常帧间隔
- package-data 打包数据，默认压缩视频
- export time/frame RRD
- inspect-offset 和 sweep-offset

打包一个 session 给别人用。默认会压缩视频，不会把 `.venv`、`_artifacts`、RRD、MANO 模型一起打进去：

```bat
robocap-rerun package-data Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1
```

默认输出：

```text
<session>/_artifacts/packages/<session>_<segment>_data_package.zip
```

如果想指定输出位置：

```bat
robocap-rerun package-data Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --output D:\share\session48_segment1.zip
```

如果确实要保留原始视频，不做压缩：

```bat
robocap-rerun package-data Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --raw-video
```

也可以用 bat 脚本：

```bat
scripts\export_data_package.bat Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 D:\share\session48_segment1.zip segment1
```

先检查一个 session 的视频帧率、文件帧数和异常帧间隔：

```bat
robocap-rerun inspect Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1
```

生成时间对齐版 RRD：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode time --use-proxy
```

生成帧对齐版 RRD，自动使用 `NOKOV FPS / video FPS` 作为主比例：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio auto --offset 0 --use-proxy
```

生成固定 8 倍、offset 40 的帧对齐版：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 40 --use-proxy
```

生成展示版布局：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 40 --use-proxy --display
```

展示版布局会保留：

- 第一行：Robocap 三组视频列，分别是 left/right、left_eye/right_eye、left_front/right_front。
- 第二行：Robocap sensors。
- 第三行：BVH、CSV、TRC 的骨骼和 mesh tabs。

## Offset 检查

生成一个 offset 映射表：

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio 8 --offset 40
```

当一个 `test*` 目录里有多个 BVH/TRC 时，建议明确指定参考源：

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio auto --offset 0 --nokov-source Z:\DATASETS\Frodobots\nokov\20260707_083023_session48\test1\test2-hand.bvh
```

扫一段候选 offset：

```bat
robocap-rerun sweep-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio 8 --offset-min 35 --offset-max 45
```

帧对齐公式是：

```text
video frame N -> NOKOV frame round(N * main_ratio) + offset
```

其中：

- `--ratio 8`：使用固定 8 倍关系。
- `--ratio auto`：使用检测到的 `NOKOV FPS / video FPS`。
- `--offset 40`：在 NOKOV 帧号上额外加 40 帧，不是直接加 40 个视频帧。

## MANO Mesh

默认 MANO 路径：

```text
Z:\MODELS\hand_models\mano\MANO_LEFT.pkl
Z:\MODELS\hand_models\mano\MANO_RIGHT.pkl
```

如果模型在别的位置：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --mano-model-dir Z:\MODELS\hand_models\mano
```

如果只想导出骨骼，不要 MANO mesh：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --no-mano-mesh
```

## 输出位置

默认输出在：

```text
<session>/_artifacts/<segment>/inspection/
```

常见文件：

- `*_time_aligned.rrd`
- `*_frame_aligned.rrd`
- `time_alignment_report.tsv`
- `frame_rate_report.md`
- `frame_rate_report.tsv`
- `video_to_nokov_frame_alignment.tsv`
- `offset_inspection.md`

## 已经 Clone 过，后续怎么更新

如果你只是在另一台电脑使用别人推上来的新版本：

```bat
cd robocap-rerun-tools
git pull
uv pip install -e .
```

如果要用网页：

```bat
uv pip install -e ".[web]"
robocap-rerun web --open
```

如果依赖有变化，也可以重新同步开发依赖：

```bat
uv pip install -e ".[dev]"
```

如果你在那台电脑也改了代码，流程是：

```bat
git status
git pull
git add <你改过的文件>
git commit -m "说明这次改了什么"
git push
```

注意：

- `git pull` 是把 GitHub 上的新版本拉到本地。
- `git push` 是把你本地提交过的修改上传到 GitHub。
- 没有 `git commit` 的修改不会被 `git push` 上传。
- 如果只是使用工具，不改代码，通常只需要 `git pull`。

## 不要提交的数据

不要提交以下内容：

- `.venv/`
- `_artifacts/`
- `.rrd`
- 视频文件
- 原始数据 CSV/TRC/BVH
- MANO pickle 模型

这些已经在 `.gitignore` 里忽略。仓库应该只放代码和文档。
