# Robocap Rerun Tools 中文说明

[English documentation](README.md)

这个工具用于检查并对齐 Robocap、NOKOV 动捕、第三人称视频和可选 robowrist 数据，然后生成
同步的 Rerun `.rrd`。时间对齐导出以 `capture_time` 为主时间轴；帧对齐导出以整数 `frame`
为主时间轴。

## 功能

- 检查视频、动捕、MAG、IMU 和 robowrist 的 FPS、帧数/样本数、相邻时间戳间隔及推算漏帧。
- 生成单文件 `timestamp_anomaly_detail_table.html`，数据、样式和 JavaScript 全部内嵌，可离线分享。
- 导出 time/frame 两种 RRD，支持自动整数 FPS 比例、有符号 Robocap 帧 offset、漏帧插值和帧范围。
- 同时读取勾选的 BVH/TRC/CSV/XRS，以及同一 3D 空间中的多个人体和刚体，不创建空白占位视图。
- 可选择第三人称视频、Robocap 视频/传感器、robowrist、MAG 和 IMU 是否进入 RRD。
- 使用压缩视频打包 session，并准备、上传约定的 ModelScope 数据集结构。
- 直接从 Web 打开检查 HTML 和生成的 RRD。
- 递归扫描一个数据集合根目录，并从可搜索下拉框选择识别到的 Session。
- 按 `PXX` 汇总录制时长；每个 Segment 只计算一个 Robocap 参考视频，并可先补做缺失的检查报告。

## 适用数据

当前主要面向类似下面的 session：

```text
Z:\DATASETS\Frodobots\nokov\20260707_083023_session48
```

常见内容包括：

- Robocap 多视角视频：left/right、eye、front、wrist。
- Robocap 传感器文件。
- 标准 `mocap/` 子目录，也兼容旧 `test*/` 或其他 GT 子目录，其中包含第三人称视频和
  BVH/TRC/CSV/XRS 动捕导出。
- 可选 `robowrist_<device_id>_<side>/` 目录。
- 可选 MANO 模型，仅用于 CLI 高级手部 mesh 重定向。

如果某些视频或数据缺失，导出脚本会跳过对应视图，不会因为少一个流就整体失败，也不会创建无数据的文字占位窗口。MAG 和 Robocap IMU 都不存在时，整行传感器视图会被省略。

## Windows 快速开始

运行工具只需先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)；Git 仅用于克隆和
更新代码。使用 HTTPS 克隆，不需要 SSH key：

```bat
git clone https://github.com/untuitivist/robocap-rerun-tools.git
cd robocap-rerun-tools
start_web.bat
```

`start_web.bat` 会自动执行 `uv sync --extra web`：创建 `.venv`，按需下载兼容的 Python 3.11+，
安装全部 Python 依赖，并打开 `http://127.0.0.1:7860`。首次同步还会下载约 87 MiB、同时包含
FFmpeg 和 FFprobe 的 `ffmpeg-binaries-compat` wheel。因此不需要单独安装 Python/FFmpeg，
也不需要管理员权限、激活虚拟环境或修改系统 `PATH`。

工具默认优先使用 uv 锁定的 FFmpeg/FFprobe，保证不同电脑处理一致；仅当当前平台没有可用 wheel 时，
才回退到系统中的完整 FFmpeg/FFprobe。目前 wheel 支持 Windows x64、Linux x64 和 macOS universal。

## Linux 与 macOS 快速开始

使用 HTTPS 克隆后运行 POSIX 启动脚本：

```sh
git clone https://github.com/untuitivist/robocap-rerun-tools.git
cd robocap-rerun-tools
./start_web.sh
```

`start_web.sh` 与 Windows 启动器一样会执行 `uv sync --extra web`，随后使用
`.venv/bin/robocap-rerun` 启动 Web，并保持前台运行以打印日志。只有确认环境已经同步时才使用
`ROBOCAP_SKIP_SYNC=1 ./start_web.sh`。通过 Git clone 会保留可执行权限；如果使用不保留权限的源码
压缩包，首次运行前执行一次 `chmod +x start_web.sh`。

## CLI 与开发

只使用 CLI：

```bat
uv sync
uv run robocap-rerun --help
```

不经过启动脚本打开 Web：

```bat
uv run --extra web robocap-rerun web --open
```

开发和测试：

```bat
uv sync --extra web --extra dev
uv run python -m pytest -q
```

下文命令为了简洁直接写 `robocap-rerun`。可以先执行一次
`.venv\Scripts\activate.bat`，也可以在每条命令前加 `uv run`。

## Web 界面

之后每次启动直接运行：

```bat
start_web.bat
```

页面提供：

- 中文/英文切换
- 检查帧率、漏帧和异常时间戳，并快捷打开独立 HTML 报告
- 按 `PXX` 统计未检查/差帧时长、总时长、Session 数和逐 Session 帧数异常
- 默认压缩视频的数据打包
- time/frame RRD 导出、offset 检查和 offset sweep
- RRD Web Viewer
- ModelScope 数据准备、上传和 Token 配置
- 环境、依赖和 Git 更新检查
- 内置中文文档

页面顶部先填写包含多条录制数据的数据集根目录，再点击“扫描 Session”。扫描器兼容 Session
直接位于根目录下，以及 `EgoMotionActions/<批次>/<动作>/<session_id>` 这类嵌套结构；下拉框使用相对路径
区分同名 Session。Session 的判定标准是其根目录直接存在至少一个 `robocap_*` 源文件。
扫描会排除分析结果、生成产物、ModelScope 暂存数据、标定目录、虚拟环境和构建目录。数据集根目录
和最近选择的 Session 会在 Web 重启后恢复；切换 Session 时会清空上一条 Session 派生出的 GT、
检查报告、RRD、第三人称视频和 Offset 参考文件选择，避免跨 Session 误用。

两个 Offset 输入框旁都有“设为默认值”按钮。点击后会把当前整数 Robocap 视频帧偏移量同步到“导出 RRD”和
“Offset”两个页面，并在 Web UI 重启后继续使用。Windows 配置保存在
`%LOCALAPPDATA%\robocap-rerun-tools\web_settings.json`。

在“导出 RRD / Export”页先点“扫描文件”，网页会列出 GT/NOKOV 目录下的
`.bvh`、`.trc`、`.csv`、`.xrs` 文件。你可以勾选哪些文件进入 RRD，也可以选择是否包含第三人称视频、
是否包含 robowrist、MAG 和 IMU 数据。Web 导出固定记录骨骼和刚体，不执行模型重定向；高级重定向参数
仍可通过 CLI 使用。扫描时还会检测标准 robowrist 视频与传感器数据；没有检测到时，robowrist 复选框会
自动取消并禁用，导出文件名使用 `rw0`，不会再用 `rw1` 表示不存在的数据。

“环境 / Environment”页会检查 Python、Python 包、ffmpeg/ffprobe 和 Git 仓库状态，并显示分支、
commit、HTTPS origin、upstream、本地改动及 ahead/behind 数量。“检查代码更新”会 fetch `origin`；
fetch 失败时会明确显示远端状态未知，不再使用可能过期的 `origin/master` 得出“已是最新”。未跟踪
文件会作为本地工作区状态单独列出，本身不代表本地 commit 与 GitHub 不同。“更新代码并重启”要求
工作区干净，并执行 `git pull --ff-only`。代码更新和依赖更新都会在独立的
`cmd` 窗口运行，通过预检后才关闭 Web，打印完整日志，执行 `uv sync --extra web`，最后通过
`start_web.bat` 重启。程序不会自动 stash，也不会覆盖本地改动。

Web“检查”只生成 `timestamp_anomaly_detail_table.html`，数据、样式与 JavaScript 全部内嵌，可离线
打开并直接分享。“检查报告 / Reports”页可以扫描报告并用默认浏览器打开，顶部输出框只打印生成路径。

“统计 / Statistics”页会扫描根目录下识别到的全部 Session，并从 Session 路径或直属 `mocap*`
目录中的唯一 `PXX` 归类。每个 Segment 只选择一个 Robocap 参考视频计算时长，不会把多摄像头重复
相加。默认会按选择的 8/4 倍比例串行补做缺失检查，再输出每个动作的未检查/差帧时长、总时长、
Session 数、`{Session: Session 时长}` 和逐 Session 异常列表。异常列表区分正常、mocap 多帧/少帧、
第三人称多帧/少帧，并合并同一 Session 多个 Segment 的类别。未检查/差帧时长只包含缺失或无法读取
报告，以及帧数不满足 `n:ratio*(n+1):(n+1)` 的 Segment；时间戳 diff、推算丢帧、缺失时间戳和
frame_index 等其他问题均不计入此时长。

同一页可批量准备并上传 clean Session。点击时会重新筛选，必要时先补报告；只有全部 Segment 都满足
上述帧数关系的 Session 才会进入上传。批量流程固定使用压缩的完整 Session，默认选择
BVH/CSV/TRC/MP4 并排除路径含 `unnamed` 的文件，不包含 RRD，目标仓库读取 `.env`。无法唯一识别
`PXX`，且不在明确的 `EgoMotionActions/<动作>/...` 结构下的 Session 会跳过。全部合格 Session 共用
同一个上传日期目录。

“查看 Rerun / Viewer”页可以扫描当前 session 下生成的 `.rrd` 文件，选择其中一个用 Rerun Web Viewer
打开。扫描后默认选择修改时间最新的 RRD，不再默认打开按文件名排序的旧文件。Viewer 会在独立 `cmd`
窗口里运行，方便查看 Rerun 自己的日志。
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

## 上传 ModelScope 数据集

发布时使用可直接访问的文件，不只上传 ZIP。准备后的目录固定为：

```text
<dataset_root>/
  README.md                              # 必需：ModelScope Dataset Card
  metadata.jsonl                         # 必需：每个 session 一行
  raw_calibration/                       # 必需：由独立标定流程维护
    <device_id>/                         # 通过明确的 device ID 查找
  EgoMotionActions/                      # 工具生成的动作数据
    <YYYYMMDD>/                          # 新上传按上传电脑本地日期归档
      <primitive_id>/                    # P01-P29 惯例或自定义动作名称
        <session_id>/
          robocap_<segment>_video_*.mp4       # 必需：六路第一人称相机
          robocap_<segment>_imu_*.db          # 必需：Robocap IMU
          robocap_<segment>_mag_*.db          # 必需：Robocap MAG
          mocap/
            *.{bvh,trc,csv,xrs,c3d,...}       # [可选格式]：至少存在一种
                                              # 每种已选格式包含全部人体和刚体
            *.mp4                             # 必需：一个或多个第三人称视频
          robowrist_<device_id>_<side>/       # 必需：左右视频、IMU、MAG
          rerun/<segment>/inspection/*.rrd    # [可选]：仅勾选后包含
          manifest.json                       # 自动生成；包含 device ID
          timestamp_anomaly_detail_table.html # 自动生成且必需
    Demo/                                  # 旧结构迁移来的示例；新上传不写入
      <primitive_id>/
        <session_id>/                      # 内容与上面的 session 相同
```

采集数据与动捕内容本身必需。只有具体采用哪些 NOKOV 导出格式和是否包含 RRD 是可选项；动捕格式
至少存在一种。Web 的 ModelScope 页会扫描 Session 直属的唯一 `mocap*` 目录，并像选择 RRD 一样
逐文件显示复选框。扫描后默认只勾选 BVH、CSV、TRC 与 MP4；相对路径中含 `unnamed` 的文件不区分
大小写，默认不勾选。其他文件仍可手动勾选；未勾选的杂项不会进入暂存，且至少保留一个 Mocap 文件。

选择 Session 时，ModelScope 页会从直属 `mocap*` 目录名中的独立 `PXX` 片段自动建议动作基元。例如
`mocap-P03-St-user` 会建议 `P03`。这只是默认值；下拉框允许任意安全的单级目录名，手动输入具有最终
优先级。没有匹配或存在冲突匹配时保留当前值。

原始标定数据位于每个动作 session 之外，统一放在
`<dataset_root>/raw_calibration/<device_id>/`。Session 准备命令不会复制源 session 内的本地
`raw_calibration/`，根级标定数据由独立流程维护。Session 的 `manifest.json` 与 `metadata.jsonl`
只通过 `device_ids: {main, left, right}` 引用它；本地路径、API 响应和临时签名 URL 永远不会进入发布包。

可以复制 `.env.example` 为 `.env`，也可以在 Web 的“ModelScope”页保存 Token：

```dotenv
MODELSCOPE_API_TOKEN=
MODELSCOPE_ENDPOINT=https://modelscope.cn
MODELSCOPE_REPO_ID=owner/egomocap
```

`.env` 已被 Git 忽略，也会被所有数据打包流程排除。Token 不会进入命令行参数或日志。检查身份：

```bat
robocap-rerun modelscope-auth
```

先准备一套 session。数据集根目录自动使用该 Session 同级的 `_modelscope_dataset`。下面的命令会
重新生成检查 HTML，默认压缩视频，去掉待上传 HTML 中的本机绝对路径，并更新数据集根目录的
`metadata.jsonl`。准备结果先写入仅供本地使用的 `_prepared/<primitive_id>/<session_id>/`：

```bat
robocap-rerun modelscope-stage Z:\DATASETS\Frodobots\nokov\20260803_081935_session39 --primitive-id P01 --segment segment1 --refresh-inspection
```

CLI 可重复使用 `--mocap-file`，参数可以是 Session 相对路径或绝对路径；不传该参数时，为兼容旧用法，
会暂存全部可打包 Mocap 文件。重新准备同一 Session 会按本次选择同步目标 `mocap/`，此前勾选但本次
取消的文件不会残留。RRD 使用相同的逐文件逻辑，但允许一个都不选：

```bat
robocap-rerun modelscope-stage Z:\DATASETS\Frodobots\nokov\20260803_081935_session39 --primitive-id P01 --segment segment1 --mocap-file mocap\motion.trc --mocap-file mocap\third_person.mp4 --rrd-file _artifacts\segment1\inspection\frame.rrd
```

再上传 `metadata.jsonl` 引用的全部已准备 session。可恢复上传缓存会跳过未变化的文件：

```bat
robocap-rerun modelscope-upload Z:\DATASETS\Frodobots\nokov\_modelscope_dataset
```

上传开始时，所有待上传 Session 按上传电脑的本地日期归档到
`EgoMotionActions/<YYYYMMDD>/<primitive_id>/<session_id>/`，精确 ISO 开始时间保留在
`upload_batch_created_at`。工具会在传输前原子更新 `manifest.json` 与 `metadata.jsonl`；传输失败后
重试会复用原日期。旧 `YYYYMMDD_HHMMSS` 路径仍可读取但不再生成。`_prepared/` 不会上传。
`EgoMotionActions/Demo/` 只存放从旧版无批次结构迁移的示例。
提交新索引前，工具会下载并合并远端 `metadata.jsonl`，保留无关批次与 Demo 行；本地相同
`(primitive_id, session_id)` 的行覆盖远端旧行。只有 Session 文件传输成功后才提交合并索引。

上传默认使用 `.env` 中的 `MODELSCOPE_REPO_ID`；只有临时覆盖时才传
`--repo-id owner/another-dataset`。仅 CLI 在明确需要时支持
`--create-if-missing --visibility private`。上传使用官方 `modelscope-hub` 和可恢复缓存。
Web 固定使用压缩视频，并要求目标仓库已经存在；不提供原始视频、编码参数、仓库创建、可见性或
license 控件。

先检查一个 session 的视频帧率、文件帧数和异常帧间隔：

```bat
robocap-rerun inspect Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1
```

检查范围包含 Robocap/robowrist 视频、第三人称视频、GT 运动文件，以及 IMU/MAG SQLite 数据库中
带时间戳的数据表。ACC、GYRO、MAG 会作为独立数据流分别报告。视频 `fps` 使用 ffprobe 给出的全程
平均值；中位/最小/最大帧间隔和异常数量使用真实逐帧时间戳计算。MP4 存在数值型 `comment` 捕获时间时，
起终时间会落在该 capture-time 轴上；缺少该 metadata 时，报告会明确标记为相对媒体时间。
检查页或 CLI 的 `--mocap-ratio` 可以手动选择动捕比例。默认 `8` 按动捕 240 FPS 检查，期望帧数为
`8*(n+1)`；选择 `4` 时按动捕 120 FPS 检查，期望帧数为 `4*(n+1)`。视频始终按 30 FPS，第三人称
视频始终为 `n+1`。所以动捕整数毫秒时间戳的正常 diff 在比例 8 时为 4/5 ms，在比例 4 时为
8/9 ms。diff 只计算时间戳都有效的相邻数据行；缺失行会列为异常，但绝不跨过缺失行相减：

```bat
robocap-rerun inspect Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mocap-ratio 4
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

需要在对齐前补齐 NOKOV/GT 短时丢帧时，增加：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --mode frame --ratio 8 --interpolate-dropped-frames --use-proxy
```

插值固定按 NOKOV 240 FPS 处理。整数毫秒时间戳中正常的 4/5 ms 抖动保持不变；约 8 ms 的
相邻有效时间戳缺口补 1 个线性姿态，约 12 ms 补 2 个，依此类推。超过 1 秒的缺口视为采集中断，
不会跨越插值。该选项只补 GT 轨迹，不伪造视频帧或传感器样本。同一导出对象的 BVH/CSV/XRS
在存在同名 TRC 时统一使用 TRC 采样时钟，即使界面中没有勾选展示该 TRC；这是因为 CSV 的
追踪无效占位行可能写入零时间戳。
在 Rerun 3D 视图中，插值产生的骨骼/刚体帧会显示为纯红色，并持续显示 0-based GT 源帧号；
帧对齐模式还会同时显示 `frame` timeline 帧号。进入下一原始帧后恢复配置的点线颜色并清除标签。

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

- 第一行：Robocap 三组视频列，分别是 left/right、left_eye/right_eye、left_front/right_front；当勾选并检测到 robowrist 时，额外加入 left_wrist_down/right_wrist_down 视频列。
- 中间传感器区域：整体是一个单列多行 Grid；内部第 1 行是完整的 Robocap sensors，检测到 wrist 数据时，第 2、3 行分别是左、右 wrist MAG/IMU，不存在的行直接省略。Robocap sensors 内部的 `middle_mag` 跨两行，左手 `acc/gyro` 同一行，右手 `acc/gyro` 同一行。
- 第三行：实际存在的 BVH、TRC、CSV、XRS 骨骼视图从左到右并列，mesh 与第三人称视频位于旁边。不会为空缺格式创建占位窗口。

NOKOV 导出的坐标默认按毫米读取，并用 `0.001` 转换为 Rerun 中的米。脚本还会读取文件头的
`BoneAxis`：`BoneAxis=Z` 使用 Y-up，`BoneAxis=Y` 使用 Z-up；无法识别时才使用默认坐标系。
同一个 CSV/XRS 内的多个刚体会保留在同一个 3D 空间中，不会分别归一化或移动到不同原点。

使用 `--no-mag` 或 `--no-imu` 可以让对应传感器既不写入 RRD，也不出现在布局中。Web 导出页提供相同的勾选项。wrist 数据先按标准 robowrist 目录发现，再递归后备匹配对应 segment 文件，因此打包时多套一层目录也不会漏掉左右 wrist MAG 数据库。

## Offset 检查

生成一个 offset 映射表：

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --offset 5
```

当一个 `mocap` 目录里有多个 BVH/TRC 时，建议明确指定参考源：

```bat
robocap-rerun inspect-offset Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --segment segment1 --ratio auto --offset 0 --nokov-source Z:\DATASETS\Frodobots\nokov\20260707_083023_session48\mocap\test2-hand.bvh
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
- `--ratio auto`：实时扫描当前 session，分别计算所有有效 GT 运动数据 FPS 与 Robocap 视频 FPS
  的均值，各自取最近的 10 倍数，再计算 `GT 取整 FPS / Robocap 取整 FPS`，并将这个商再次
  四舍五入为最接近的正整数，作为实际 auto ratio。该过程不读取或生成检查报告。
- `--ratio 8`：需要固定比例时显式覆盖自动值。
- `--offset 5`：以 Robocap 视频为基准的有符号视频帧数。正值表示 NOKOV/GT 相对 Robocap
  视频前移、提前出现，同一视频帧会取更靠后的 GT 帧；负值表示 NOKOV/GT 相对 Robocap
  视频后移、延后出现，同一视频帧会取更靠前的 GT 帧。程序先转换为
  `GT 帧 offset = round(Robocap 帧 offset * ratio)`，然后原样使用源脚本的对齐公式。
  ratio 为 8 时，`5` 会转换为 GT offset `40`，`-5` 会把 GT 第 0 帧放到 Robocap 视频第 5 帧。

Rerun 中显示的统一帧号为：视频第 `N` 帧位于 `round(N * ratio)`；GT 源帧 `K` 位于
`K - GT 帧 offset`。因此 ratio 8、offset 5 时，视频第 100 帧和 GT 第 840 帧都位于
统一时间轴的 `frame=800`。

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

如果通过 CLI 只导出骨骼而不要 MANO mesh，传入 `--retarget-model none`。此时 Rerun
不会创建 mesh 窗口，也不会增加无数据的文字占位窗口。Web 导出会自动使用该模式，且不显示模型控件：

```bat
robocap-rerun export Z:\DATASETS\Frodobots\nokov\20260707_083023_session48 --retarget-model none
```

## 输出位置

Web 发起的 RRD 导出、检查、打包、Offset 与环境命令都不设置进程超时，会一直等待命令自行完成。

默认输出在：

```text
<session>/_artifacts/<segment>/inspection/
```

常见文件：

- `*_time_aligned_fall_interp0_rt-none_raw_bp-default_data-..._cfg-....rrd`
- `*_frame_aligned_r8_o5_ref-left_f100-500_interp1_rt-none_p540_bp-display_data-..._cfg-....rrd`
- `time_alignment_report.tsv`
- `timestamp_anomaly_detail_table.html`
- `video_to_nokov_frame_alignment.tsv`
- `offset_inspection.md`

RRD 文件名会纳入导出参数，避免不同配置互相覆盖。可读部分包含帧对齐 ratio（`r`）、Robocap
帧 offset（`o`）、参考视频（`ref`）、帧区间（`f`，全量为 `fall`）、插值开关（`interp`）、重定向模型（`rt`）、
原始/代理视频、布局（`bp`）和数据流开关。末尾稳定的 `cfg-<10 位十六进制>` 指纹还会覆盖
精确压缩参数、传感器点数上限、时间裁剪/对齐开关、坐标缩放、GT 输入、MANO 目录等其余会影响
内容的参数。即使显式指定 `--save`，也会追加同样的参数后缀；如果路径已经包含同一后缀则不会重复。

## 更新已有 Clone

拉取代码后直接运行启动器；启动器会自动同步发生变化的依赖：

```bat
git pull --ff-only
start_web.bat
```

Linux 或 macOS 使用：

```sh
git pull --ff-only
./start_web.sh
```

Web“环境 / Environment”页提供相同的干净工作区更新流程，不会自动 stash 或覆盖本地修改。

开发环境需要额外同步测试工具：

```bat
uv sync --extra web --extra dev
```

如果你在那台电脑也改了代码，流程是：

```bat
git status
git add <你改过的文件>
git commit -m "说明这次改了什么"
git pull --rebase
git push
```

注意：

- `git pull` 是把 GitHub 上的新版本拉到本地。
- `git push` 是把你本地提交过的修改上传到 GitHub。
- 没有 `git commit` 的修改不会被 `git push` 上传。
- 如果只是使用工具，不改代码，通常只需要 `git pull`。

## 迁移旧动捕目录

旧数据若仍使用原来的 session 动捕子目录名，先预览，再正式执行：

```bat
uv run python scripts\migrate_mocap_layout.py Z:\DATASETS\Frodobots\nokov --rewrite-zip
uv run python scripts\migrate_mocap_layout.py Z:\DATASETS\Frodobots\nokov --rewrite-zip --apply
```

该命令会重命名 session 子目录，更新 `_analysis`、`_artifacts`、`_modelscope_dataset` 中生成
报告和 manifest 的路径引用，并原位更新顶层 ZIP 的等长路径字段，不重新压缩数据。RAR 需要使用
RAR 归档工具单独重命名，并在修改后运行归档完整性测试。

## 仓库中不要提交的数据

不要提交以下内容：

- `.venv/`
- `_artifacts/`
- `.rrd`
- 视频文件
- 原始数据 CSV/TRC/BVH
- MANO pickle 模型

这些已经在 `.gitignore` 里忽略。仓库应该只放代码和文档。
