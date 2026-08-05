# Robocap Rerun Tools 中文说明

这个仓库用于检查 Robocap/NOKOV session 的帧率、时间线和 offset，并生成 Rerun `.rrd` 文件。核心目标是把 Robocap 视频、传感器数据、NOKOV BVH/TRC/CSV/XRS 骨骼数据、第三人称视频和可选 MANO 手部 mesh 放到同一个 `capture_time` 时间线上。

## 适用数据

当前主要面向类似下面的 session：

```text
Z:\DATASETS\Frodobots\nokov\20260707_083023_session48
```

常见内容包括：

- Robocap 多视角视频：left/right、eye、front、wrist。
- Robocap sensor CSV。
- `test*`、`nokov` 或其他 GT 子目录里的 NOKOV 数据：第三人称视频、BVH、TRC、CSV、XRS、手部轨迹。
- 可选 MANO 模型：`MANO_LEFT.pkl`、`MANO_RIGHT.pkl`。

如果某些视频或数据缺失，导出脚本会跳过对应视图，不会因为少一个流就整体失败，也不会创建无数据的文字占位窗口。MAG 和 Robocap IMU 都不存在时，整行传感器视图会被省略。

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

或者直接运行：

```bat
start_web.bat
```

默认地址：

```text
http://127.0.0.1:7860
```

网页里可以做：

- 中文/英文切换
- 在“文档 / Docs”页直接看中文说明
- inspect 帧率和异常帧间隔
- package-data 打包数据，默认压缩视频
- export time/frame RRD
- inspect-offset 和 sweep-offset

两个 Offset 输入框旁都有“设为默认值”按钮。点击后会把当前整数 Robocap 视频帧偏移量同步到“导出 RRD”和
“Offset”两个页面，并在 Web UI 重启后继续使用。Windows 配置保存在
`%LOCALAPPDATA%\robocap-rerun-tools\web_settings.json`。

在“导出 RRD / Export”页先点“扫描文件”，网页会列出 GT/NOKOV 目录下的
`.bvh`、`.trc`、`.csv`、`.xrs` 文件。你可以勾选哪些文件进入 RRD，也可以选择是否包含第三人称视频、
是否包含 robowrist 数据，以及选择重定向模型。当前真正实现的是 MANO；SMPL/SMPLH 先作为显式选项保留，
导出时会在记录说明里写明尚未实现，不会假装已经生成对应 mesh。

“环境 / Environment”页可以检查 Python、Python 包、ffmpeg/ffprobe、git 分支与本地/远端版本，
也可以运行 `uv pip install -e ".[web]"` 安装或更新依赖；依赖更新会打开一个新的 `cmd` 窗口，
先关闭当前 web 进程，再打印更新日志，最后通过 `start_web.bat` 自动重启。源代码更新请使用
下面的常规 Git 流程。

“查看 Rerun / Viewer”页可以扫描当前 session 下生成的 `.rrd` 文件，选择其中一个用 Rerun Web Viewer
打开。Viewer 会在独立 `cmd` 窗口里运行，方便查看 Rerun 自己的日志。
端口默认填 `0`，表示自动选择。指定端口如果被 Windows 保留或已被其他程序占用，网页会自动改用
可绑定的本机端口并回填实际端口。Rerun 会在默认浏览器中自动打开已连接 recording 的完整地址；
不要单独打开 HTTP 根地址，根地址只会显示 Rerun 欢迎页。

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

生成帧对齐版 RRD。主比例默认使用 `auto`：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --offset 0 --use-proxy
```

生成固定 8 倍、offset 5 的帧对齐版；它等价于旧定义中的 GT offset 40：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 5 --use-proxy
```

只导出 Robocap 参考视频第 100 到第 500 帧。帧号从 0 开始，并且首尾都包含：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 5 --robocap-start-frame 100 --robocap-end-frame 500 --use-proxy
```

起始帧与结束帧必须同时填写。导出器会先把参考视频的帧区间转换为一个统一的 `capture_time`
时间窗，用它同时裁剪所有 Robocap 视频、传感器、NOKOV 轨迹和第三人称视频，再与原有的公共数据
时间窗求交。在 Web 页面先勾选“限制 Robocap 帧范围”再填写两个帧号；不勾选时保持全量导出。

帧对齐版 RRD 的主时间轴是整数 `frame`，统一采用 GT/NOKOV 帧尺度。Robocap 视频第 `N` 帧
写在 `frame = round(N * ratio)`，GT 源数据第 `K` 帧写在
`frame = K - round(offset * ratio)`；其余视频和传感器根据参考视频时间戳映射到同一条帧轴。
`capture_time` 仍然保留用于复核，但在 frame 模式下不再是默认时间轴。

生成展示版布局：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --offset 5 --use-proxy --display
```

展示版布局会保留：

- 第一行：Robocap 三组视频列，分别是 left/right、left_eye/right_eye、left_front/right_front。
- 第二行：Robocap sensors；`middle_mag` 跨两行，左手 `acc/gyro` 同一行，右手 `acc/gyro` 同一行。
- 第三行：实际存在的 BVH、TRC、CSV、XRS 分别进入骨骼 tabs 和 mesh tabs，旁边显示第三人称视频。不会为空缺格式创建占位 tab。

NOKOV 导出的坐标默认按毫米读取，并用 `0.001` 转换为 Rerun 中的米。脚本还会读取文件头的
`BoneAxis`：`BoneAxis=Z` 使用 Y-up，`BoneAxis=Y` 使用 Z-up；无法识别时才使用默认坐标系。
同一个 CSV/XRS 内的多个刚体会保留在同一个 3D 空间中，不会分别归一化或移动到不同原点。

使用 `--no-mag` 或 `--no-imu` 可以让对应传感器既不写入 RRD，也不出现在布局中。Web 导出页提供相同的勾选项。

## Offset 检查

生成一个 offset 映射表：

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --offset 5
```

当一个 `test*` 目录里有多个 BVH/TRC 时，建议明确指定参考源：

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio auto --offset 0 --nokov-source Z:\DATASETS\Frodobots\nokov\20260707_083023_session48\test1\test2-hand.bvh
```

扫一段候选 offset：

```bat
robocap-rerun sweep-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio 8 --offset-min -10 --offset-max 10
```

帧对齐公式是：

```text
GT 帧 offset = round(Robocap 帧 offset * main_ratio)
video frame N -> NOKOV frame round(N * main_ratio) + GT 帧 offset
```

其中：

- 不写 `--ratio`：默认使用 `auto`。
- `--ratio auto`：读取 `_artifacts/<segment>/inspection/frame_rate_report.md` 表格，分别计算所有
  有效 GT 运动数据 FPS 与 Robocap 视频 FPS 的均值，各自取最近的 10 倍数，再计算
  `GT 取整 FPS / Robocap 取整 FPS`，并将这个商再次四舍五入为最接近的正整数，作为实际
  auto ratio。报告不存在或无法计算时会先自动生成检查报告。
- `--ratio 8`：需要固定比例时显式覆盖自动值。
- `--offset 5`：以 Robocap 视频为基准的有符号视频帧数。正值表示 NOKOV/GT 相对 Robocap
  视频前移、提前出现，同一视频帧会取更靠后的 GT 帧；负值表示 NOKOV/GT 相对 Robocap
  视频后移、延后出现，同一视频帧会取更靠前的 GT 帧。程序先转换为
  `GT 帧 offset = round(Robocap 帧 offset * ratio)`，然后原样使用源脚本的对齐公式。
  ratio 为 8 时，`5` 会转换为 GT offset `40`，`-5` 会把 GT 第 0 帧放到 Robocap 视频第 5 帧。

Rerun 中显示的统一帧号为：视频第 `N` 帧位于 `round(N * ratio)`；GT 源帧 `K` 位于
`K - GT 帧 offset`。因此 ratio 8、offset 5 时，视频第 100 帧和 GT 第 840 帧都位于
统一时间轴的 `frame=800`。

`frame_rate_report.md` 会写出 GT/Robocap 样本数、原始均值、10 倍数取整值、最终取整前的商和
实际使用的整数 ratio，便于复核。
这取代了旧版“Offset 直接使用 GT 帧数”的定义。

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

MANO 重定向沿用示例脚本的命名约定：BVH 的 `Finger0/1/2` 与 TRC/CSV/XRS 的
`Finger1/2/3` 分别对应 MANO 的 MCP/PIP/DIP。MANO 模板先统一归一化，再按每帧手腕原点、
掌部朝向和手部尺度建立初始姿态，用实际关节点覆盖对应链并执行线性蒙皮。因此每一帧都会产生
随骨骼姿态变化的 mesh，而不是只把静态模板放到手腕位置。

如果只想导出骨骼，不要 MANO mesh，在“重定向模型”中选择 `none`。此时 Rerun
不会创建 mesh 窗口，也不会增加无数据的文字占位窗口：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --retarget-model none
```

## 输出位置

Web 发起的 RRD 导出、检查、打包、Offset、环境与 Git 命令都不设置进程超时，会一直等待命令自行完成。

默认输出在：

```text
<session>/_artifacts/<segment>/inspection/
```

常见文件：

- `*_time_aligned_fall_rt-none_raw_bp-default_data-..._cfg-....rrd`
- `*_frame_aligned_r8_o5_ref-left_f100-500_rt-none_p540_bp-display_data-..._cfg-....rrd`
- `time_alignment_report.tsv`
- `frame_rate_report.md`
- `frame_rate_report.tsv`
- `video_to_nokov_frame_alignment.tsv`
- `offset_inspection.md`

RRD 文件名会纳入导出参数，避免不同配置互相覆盖。可读部分包含帧对齐 ratio（`r`）、Robocap
帧 offset（`o`）、参考视频（`ref`）、帧区间（`f`，全量为 `fall`）、重定向模型（`rt`）、
原始/代理视频、布局（`bp`）和数据流开关。末尾稳定的 `cfg-<10 位十六进制>` 指纹还会覆盖
精确压缩参数、传感器点数上限、时间裁剪/对齐开关、坐标缩放、GT 输入、MANO 目录等其余会影响
内容的参数。即使显式指定 `--save`，也会追加同样的参数后缀；如果路径已经包含同一后缀则不会重复。

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
