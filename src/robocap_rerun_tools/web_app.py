from __future__ import annotations

import argparse
import importlib.metadata
import inspect
import json
import math
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from robocap_rerun_tools import MEDIA_TOOLS
from robocap_rerun_tools.frame_comparison import (
    FrameComparisonError,
    discover_video_files,
    iter_frame_comparison,
)
from robocap_rerun_tools.mocap_metadata import (
    build_mocap_capture_metadata,
    parse_mocap_capture_directory,
)
from robocap_rerun_tools.session_layout import discover_mocap_directories

DEFAULT_SELECTED_MOCAP_SUFFIXES = frozenset({".bvh", ".csv", ".mp4", ".trc"})
MOCAP_PRIMITIVE_PATTERN = re.compile(r"(?<![A-Z0-9])(P\d{2})(?![A-Z0-9])", re.IGNORECASE)
MOCAP_FALLBACK_PRIMITIVE_PATTERN = re.compile(
    r"^mocap[-_]([A-Z]\d{2})(?=[-_]|$)", re.IGNORECASE
)
SEQUENTIAL_UPLOAD_RETRIES = 3
STATISTICS_MOCAP_METADATA_HEADERS = [
    "Update",
    "Session",
    "Mocap directory",
    "Action ID",
    "Collection Session index",
    "Collector",
    "Repetition count",
    "Parse status",
]

EN_DOC = """# Robocap Rerun Tools

This is a local browser UI for Robocap/NOKOV inspection, data packaging, RRD export, and offset checks.

## Basic Workflow

1. Enter a dataset root containing one or more sessions, then click `Scan sessions`.
2. Select a detected session and enter the segment name, usually `segment1`.
3. Run `Inspect` first to check FPS, frame/sample counts, and abnormal intervals.
4. Use `Package Data` to create a zip for sharing. Videos are compressed by default.
5. Use `Export RRD` to create time-aligned or frame-aligned Rerun files.
6. Use `Offset` when you need to inspect or sweep a video-to-NOKOV frame offset.

Each Session may contain one direct child directory whose name starts with `mocap`, matched without
case sensitivity, for example `mocap/`, `mocap_01/`, or `Mocap-NOKOV/`. Multiple `mocap*`
directories are reported as ambiguous. ModelScope staging always publishes this source as `mocap/`.

The export controls can independently include or exclude MAG, IMU, robowrist, and third-person video data.
`Interpolate dropped NOKOV frames` linearly fills GT trajectory gaps at the fixed 240 FPS source
rate before either time or frame alignment. Video and sensor samples are never synthesized.
Interpolated 3D frames are rendered in solid red with a visible source/timeline frame label.
The Environment tab shows Git branch, commit, origin, upstream, dirty state, and ahead/behind counts.
`Check code updates` fetches `origin`; `Update code and restart` requires a clean working tree and uses
`git pull --ff-only`. Updates run in a separate cmd window, close the Web process only after preflight,
sync Web dependencies, print logs, and restart through `start_web.bat`.
Inspection always discovers third-person videos and checks each timestamped ACC, gyro, and MAG SQLite
table as a separate stream. Video average FPS comes from ffprobe; interval statistics use real frame timestamps.
Inspection writes one standalone `timestamp_anomaly_detail_table.html`. Its data, styles, and
JavaScript are embedded, so the file can be shared and opened offline. The report only computes
diffs between adjacent valid rows and lists every timestamp/frame-index anomaly with neighboring rows.
Choose the Mocap/Robocap inspection ratio before running it: `8` checks Mocap at 240 FPS with
`8*(n+1)` expected frames, while `4` checks 120 FPS with `4*(n+1)`. Video remains 30 FPS and
third-person video remains `n+1` in both modes.
The Statistics tab can create only missing inspection reports or rebuild every report in its current
scope. `Rebuild all inspection reports` takes precedence when both options are enabled, and the same
choice applies to sequential clean-Session upload.
It also scans compact Mocap directory metadata into an editable table. Selected rows can update the
remote `metadata.jsonl` and matching Session manifests in one commit without uploading videos.
`Scan files` detects the standard robowrist folders and streams. When none are present, the robowrist
control is turned off and disabled instead of pretending that wrist data can be exported.
Only GT formats that are present create views. BVH/TRC/CSV/XRS skeleton views are arranged from left
to right, while rigid
bodies from one CSV/XRS source stay in one shared world. NOKOV millimetres are converted to metres, and
the file's `BoneAxis` selects the matching up axis. The Web exporter records skeletons and rigid bodies
without model retargeting. Absent skeleton and third-person sources do not create placeholder views.
The middle sensor section is one multi-row, one-column grid: its first row is the complete Robocap
sensor block, followed by optional left- and right-wrist MAG/IMU rows. Missing rows are omitted. If
Robocap MAG and IMU are both absent and no wrist streams are selected, the complete section is omitted.

Long-running CLI actions stream their combined stdout and stderr into the Output box. The display
refreshes about twice per second with command status, elapsed time, a determinate bar for `[n/total]`
or percentage output, an animated bar while the total is unknown, and recent logs. Carriage-return
updates from tools such as tqdm are supported. There is no process timeout; old log lines are bounded
to keep the browser session's memory stable.

## Alignment

Frame alignment uses:

```text
GT frame offset = round(Robocap frame offset * ratio)
video frame N -> NOKOV frame round(N * ratio) + GT frame offset
```

Frame-aligned RRD files use the integer `frame` timeline as the primary timeline. The common
timeline is expressed at the GT/NOKOV frame rate: Robocap video frame `N` is logged at
`frame = round(N * ratio)`, while source GT frame `K` is logged at
`frame = K - GT frame offset`. `capture_time` remains available only as a secondary timeline.

The default is `ratio=auto`. It scans the current session, averages all valid GT FPS values and all
Robocap video FPS values separately, rounds both means to the nearest multiple of 10, divides them,
and rounds that ratio to the nearest positive integer. Enter a number such as `8` to override it.
Robocap video is the reference for the signed offset. A positive value advances NOKOV/GT relative
to Robocap video, so GT appears earlier and the same video frame selects a later GT frame. A
negative value delays NOKOV/GT relative to Robocap video, so GT appears later and the same video
frame selects an earlier GT frame. At ratio 8, offset `5` becomes source-script GT offset `40`;
offset `-5` places GT frame 0 at Robocap video frame 5.

To export only part of a session, enable `Limit Robocap frame range` and enter both
`Robocap start frame` and `Robocap end frame`. The indexes are 0-based and inclusive. One
reference-video `capture_time` window is applied to every video, sensor, NOKOV track, and
third-person stream. Output names include readable alignment and content tags such as
`r8_o5_ref-left_f100-200_rt-none`, plus a stable `cfg-...` fingerprint covering the remaining
export parameters. Different parameter sets therefore do not overwrite each other.

Use `Set as default` beside either Offset field to persist that frame offset and apply it to both
the Export and Offset tabs. The saved value is restored the next time the Web UI starts.

## ModelScope Dataset Publishing

The ModelScope tab prepares the current Session for upload. Full-session videos are copied
byte-for-byte; aligned-intersection crops permit lossless encoding only. A standalone timestamp
inspection HTML is required. Scan the RRD
files and select only the recordings that should be included. The generated dataset `README.md` is
the canonical description of the complete dataset structure and file requirements.

Scan the Session Mocap files before staging. The list includes every packageable file under the
single `mocap*` source directory. Only BVH, CSV, TRC, and MP4 files are selected by default, and any
relative path containing `unnamed` is left unselected regardless of case. Other detected files stay
available for manual selection; only checked Mocap files are staged, and at least one must remain.
Compact action IDs use `mocap-<action:[A-Z]NN>-S<session>-<collector>-<count>p`. When a Session is
selected, the original standalone `PXX` search in direct `mocap*` directory names runs first and is
unchanged. Only when no `PXX` exists does the fallback read the first action field after `mocap-` or
`mocap_`. In `mocap-L01-S07-wangyang-10p`, the action is `L01`, `S07` is the Session number,
`wangyang` is the collector, and `10p` is the repetition count. The first field participates in
action matching; a complete name also records all four fields under `mocap_capture` in new manifests
and dataset-index rows. This is a suggestion; a manual custom primitive value takes precedence.

`MODELSCOPE_API_TOKEN`, `MODELSCOPE_ENDPOINT`, and `MODELSCOPE_REPO_ID` are stored in the
repository-local `.env` file. The token field never displays the saved value; leaving it blank
preserves the current token.
Use `Prepare session` before `Upload prepared dataset`. Upload sends every session referenced by
`metadata.jsonl` and uses the official `modelscope-hub`
client and its resumable upload cache by default. The target repository must already exist.
Preparation writes to local `_prepared/<primitive>/<session>/`. At upload start, all pending
sessions share one uploader-local `YYYYMMDD` date and move to
`EgoMotionActions/<date>/<primitive>/<session>/`; the exact start time remains in metadata and a
failed transfer reuses the assigned date. Legacy `YYYYMMDD_HHMMSS` paths remain readable.
`EgoMotionActions/Demo/` is reserved for migrated legacy examples.
The Statistics tab also uploads clean Sessions sequentially. It reads remote `metadata.jsonl` first.
The upload date field is initialized to the uploader's current local `YYYYMMDD` and remains editable;
every Session in one run uses that date. Existing `(primitive_id, session_id)` entries are skipped by
default. Clearing the skip option uploads them again and replaces their metadata rows. If a different
date is selected, the prior remote directory is not deleted automatically. Each selected Session must
satisfy the exact frame-count relation, uses the curated default Mocap files and no RRD, and completes
prepare, clean validation, and upload in an isolated staging root.
An upload failure is retried three times after the initial attempt. After four failed attempts, that
Session is skipped and processing continues; preparation and clean-validation failures also skip only
the current Session. Completed uploads are not rolled back.
When aligned-intersection staging is enabled, its ratio and Offset are prefilled from the RRD
Export controls and continue to follow changes made there. Edit the ModelScope copies only to
override alignment for that staging operation.
"""


ZH_DOC = """# Robocap Rerun Tools 中文说明

这是一个本地网页工具，用于 Robocap/NOKOV 数据检查、数据打包、RRD 导出和 offset 检查。

## 基本流程

1. 输入包含一个或多个 Session 的数据集根目录，然后点击“扫描 Session”。
2. 从下拉框选择 Session，再输入 segment，通常是 `segment1`。
3. 先运行“检查”，查看 FPS、帧/样本数和异常间隔。
4. 用“打包数据”生成 zip 给别人使用。默认会压缩视频。
5. 用“导出 RRD”生成时间对齐或帧对齐的 Rerun 文件。
6. 如果需要检查视频帧和 NOKOV 帧的偏移关系，用“Offset 检查”。

每个 Session 可包含一个直属子目录，其名称以 `mocap` 开头且大小写不敏感，例如 `mocap/`、
`mocap_01/` 或 `Mocap-NOKOV/`。同时存在多个 `mocap*` 目录时会明确报歧义；ModelScope
暂存时始终将该源目录规范化发布为 `mocap/`。

导出时可以分别勾选是否包含 MAG、IMU、robowrist 和第三人称视频数据。“插值补齐 NOKOV 丢帧”
会在时间或帧对齐之前，按固定 240 FPS 对 GT 轨迹时间缺口做线性插值；视频与传感器不会伪造样本。
插值产生的 3D 帧会显示为纯红色，并显示源帧号与 Rerun timeline 帧号标签。
“环境”页会显示 Git 分支、commit、origin、upstream、工作区状态和 ahead/behind 数量。“检查代码更新”
会 fetch `origin`；“更新代码并重启”仅允许干净工作区，并执行 `git pull --ff-only`。更新在独立 cmd
窗口中运行，通过预检后才关闭 Web，同步网页依赖、打印日志，再调用 `start_web.bat` 重启。
检查会始终发现第三人称视频，并把 SQLite 中带时间戳的 ACC、GYRO、MAG 表作为独立数据流检查。
视频平均 FPS 来自 ffprobe，帧间隔统计使用真实逐帧时间戳。
检查只生成一个独立的 `timestamp_anomaly_detail_table.html`。数据、样式和 JavaScript 都内嵌在
文件中，可以离线打开并直接分享。报告只计算时间戳均有效的相邻数据行，并逐点列出时间戳与
frame_index 异常及其上下行。
执行检查前选择动捕与 Robocap 的帧率比例：选择 `8` 时按动捕 240 FPS 和 `8*(n+1)` 检查；选择
`4` 时按动捕 120 FPS 和 `4*(n+1)` 检查。两种模式下视频仍按 30 FPS，第三人称视频仍为 `n+1`。
“扫描文件”会检测标准 robowrist 目录和数据流；没有检测到时会自动取消并禁用 robowrist 选项，
不会继续显示一个实际无数据可导出的开启状态。
只有实际存在的 GT 格式才会生成视图。BVH/TRC/CSV/XRS 骨骼视图在左下角从左到右并列；同一 CSV/XRS
中的多个刚体保留在同一个 3D 世界坐标系。NOKOV 毫米坐标会转换成米，并根据文件中的 `BoneAxis`
选择向上轴。Web 导出固定记录骨骼和刚体，不执行模型重定向；不存在的骨骼或第三人称源不会创建占位窗口。
中间传感器区域整体是一个单列多行 Grid：内部第 1 行是完整的 Robocap sensors，后续按实际数据添加
左、右 wrist MAG/IMU 行，不存在的行直接省略。Robocap MAG、IMU 和所选 wrist 数据都不存在时，
整个中间区域直接省略。

耗时较长的 CLI 操作会把 stdout 与 stderr 合并后实时写入“输出”框。页面约每 0.5 秒刷新命令状态、
已用时间、进度条和最近日志；`[当前/总数]`、百分比以及 tqdm 使用的回车刷新都能识别，总量未知时
显示持续变化的进度条。任务不设置超时，同时限制保留的旧日志量，避免浏览器会话内存持续增长。

“统计”页既可只补做缺失的检查报告，也可重做当前范围内的全部报告。两个选项同时开启时，
“全部重做检查报告”优先；逐个上传无差帧 Session 时使用同一选择。
该页还会把紧凑 Mocap 目录名解析为可编辑表格；勾选后的行可一次提交更新远端
`metadata.jsonl` 与对应 Session manifest，不会重新上传视频。

## 对齐公式

帧对齐使用：

```text
GT 帧 offset = round(Robocap 帧 offset * ratio)
video frame N -> NOKOV frame round(N * ratio) + GT 帧 offset
```

帧对齐 RRD 的主时间轴是整数 `frame`，统一采用 GT/NOKOV 帧尺度：Robocap 视频第 `N` 帧写在
`frame = round(N * ratio)`，GT 源数据第 `K` 帧写在 `frame = K - GT 帧 offset`。
`capture_time` 仍会保留，但只作为辅助时间轴，不再是帧对齐版默认显示的时间轴。

默认使用 `ratio=auto`：实时扫描当前 session，分别计算有效 GT FPS 和 Robocap 视频 FPS 的
均值，各自取最近的 10 倍数，计算“GT 取整值 / Robocap 取整值”，最后把 ratio 四舍五入为
最接近的正整数。输入 `8` 等数字可覆盖自动值。
Offset 是以 Robocap 视频为基准的有符号视频帧数。正值表示 NOKOV/GT 相对 Robocap 视频
前移、提前出现，同一视频帧会选取更靠后的 GT 帧；负值表示 NOKOV/GT 相对 Robocap 视频
后移、延后出现，同一视频帧会选取更靠前的 GT 帧。ratio 为 8 时，offset `5` 会转换成源脚本
使用的 GT offset `40`；offset `-5` 会把 GT 第 0 帧放到 Robocap 视频第 5 帧。

只导出 session 的一段时，先勾选“限制 Robocap 帧范围”，再填写“Robocap 起始帧”和
“Robocap 结束帧”。帧号从 0 开始，并且首尾都包含。工具会用参考视频的 `capture_time` 生成一个
统一时间窗，同时裁剪视频、传感器、NOKOV 轨迹和第三人称视频。输出名会包含
`r8_o5_ref-left_f100-200_rt-none` 这类可读标签，并用稳定的 `cfg-...` 指纹覆盖其余导出参数，
所以不同参数组合不会互相覆盖。

点击任一 Offset 输入框旁的“设为默认值”，会持久保存当前帧偏移量，并同步到“导出 RRD”和“Offset”页。
下次启动 Web UI 时会自动恢复该值。

## ModelScope 数据集发布

“ModelScope”页用于准备和上传当前 Session。完整 Session 视频逐字节复制；对齐交集裁切只允许无损编码。
已有独立的时间戳检查 HTML。扫描 RRD 后可逐项勾选需要加入数据集的文件。生成的数据集 `README.md`
是完整数据集结构与文件要求的唯一说明位置。

准备前还要扫描当前 Session 的 Mocap 文件。列表会显示唯一 `mocap*` 源目录下所有可打包文件；默认只
勾选 BVH、CSV、TRC 与 MP4，并对相对路径中包含 `unnamed` 的文件取消默认勾选（不区分大小写）。其他
文件仍保留在列表中供手动选择；只有勾选的 Mocap 文件会进入暂存，且至少保留一个。
紧凑动作 ID 统一使用 `mocap-<动作:[A-Z]NN>-S<Session序号>-<采集员>-<次数>p`。选择 Session 时，
原有的直属 `mocap*` 目录名独立 `PXX` 搜索会优先执行且语义不变。只有完全没有 `PXX` 时，后备规则
才读取紧跟 `mocap-` 或 `mocap_` 的第一个动作字段。对于 `mocap-L01-S07-wangyang-10p`，动作是
`L01`，`S07` 是 Session 序号，`wangyang` 是采集员，`10p` 是重复次数；只有第一个字段参与动作
匹配；完整目录名的四项内容还会写入新 manifest 与数据集索引的 `mocap_capture`。动作下拉框仍
只是建议值，手动输入的安全自定义 primitive 具有最终优先级。

`MODELSCOPE_API_TOKEN`、`MODELSCOPE_ENDPOINT` 与 `MODELSCOPE_REPO_ID` 保存在仓库根目录的
`.env`。网页不会回显已保存 token 的内容；token 输入框留空时保留原值。先执行“准备 Session”，
再执行“上传已准备数据集”。
上传会包含 `metadata.jsonl` 引用的全部 session，并使用官方 `modelscope-hub`，默认开启可恢复上传缓存；
目标仓库必须已经存在。
准备阶段写入本地 `_prepared/<动作>/<session>/`。上传开始时，全部待上传 Session 共用上传电脑本地
日期 `YYYYMMDD`，并移动到 `EgoMotionActions/<日期>/<动作>/<session>/`；完整开始时间仍保存在元数据
中，传输失败后重试会复用该日期。旧 `YYYYMMDD_HHMMSS` 路径仍可读取但不再生成。
“统计”页还提供 clean Session 逐个上传。它先读取远端 `metadata.jsonl`。上传日期框默认填入上传
电脑本地当天的 `YYYYMMDD`，允许手工修改；同一次运行的所有 Session 使用同一个日期。默认开启
“跳过远端已有 Session”，按 `(primitive_id, session_id)` 排除已上传数据；关闭后重新上传并替换
同键元数据。若改用其他日期，旧远端目录不会自动删除。各 Session 必须满足精确帧数关系，使用默认
Mocap 文件且不带 RRD，并在独立暂存根目录中依次完成准备、clean 校验和上传。上传首次失败后会再
重试 3 次；共 4 次仍失败则跳过当前 Session 并继续。准备或
clean 校验失败也只跳过当前 Session，不会停止后续队列；已经完成的上传不会回滚。
启用交集裁切时，ratio 与 Offset 默认由“导出 RRD”页填入，并继续跟随该页参数变化；只有本次暂存
需要不同对齐参数时，才单独修改 ModelScope 页中的副本。
"""


LANGUAGE_PACKS = {
    "English": {
        "title": "# Robocap Rerun Tools",
        "language": "Language",
        "dataset_root": "Dataset root directory",
        "scan_sessions_button": "Scan sessions",
        "session": "Session",
        "segment": "Segment",
        "output": "Output",
        "inspect_mocap_ratio": "Inspection Mocap ratio (8: 240 FPS, 4: 120 FPS)",
        "inspect_button": "Inspect",
        "frame_compare_scan_button": "Scan videos",
        "frame_compare_videos": "Videos (one output column per selected video)",
        "frame_compare_start_frame": "Start frame (0-based, inclusive)",
        "frame_compare_end_frame": "End frame (0-based, inclusive)",
        "frame_compare_cell_width": "Cell width",
        "frame_compare_cell_height": "Cell height",
        "frame_compare_generate_button": "Generate frame comparison",
        "frame_compare_open_button": "Open image in default application",
        "frame_compare_file": "Output image",
        "statistics_mocap_ratio": "Mocap ratio for generated inspections",
        "statistics_fill_missing": "Create missing inspection reports",
        "statistics_rebuild_all": "Rebuild all inspection reports",
        "statistics_button": "Calculate statistics",
        "statistics_mocap_metadata": "Mocap directory metadata",
        "statistics_mocap_metadata_help": (
            "Rows are parsed from `mocap-<action:[A-Z]NN>-S<index>-<collector>-<count>p`. "
            "Edit Action ID, Collection Session index, Collector, or Repetition count, select the "
            "rows to update, then batch-update the remote `metadata.jsonl` and Session manifests. "
            "Session and Mocap directory identify the local source and must not be edited. This "
            "operation updates metadata only; it does not upload videos or move remote directories."
        ),
        "statistics_mocap_metadata_refresh": "Scan Mocap naming metadata",
        "statistics_mocap_metadata_update": "Batch update remote Mocap metadata",
        "statistics_batch_help": (
            "Sequential upload reads remote metadata first. The editable date is initialized to "
            "today's local `YYYYMMDD`, and every Session in this run uses it. Existing Sessions "
            "are skipped by default; clear the skip option to upload them again and replace matching "
            "metadata rows. A different date leaves the old remote directory untouched. Each selected "
            "Session must satisfy `n:ratio*(n+1):n+1`, then completes "
            "prepare, validation, and upload before the next starts. Upload failures retry three "
            "times, then skip that Session and continue. It copies full-session video "
            "byte-for-byte, selects BVH/CSV/TRC/MP4 except `unnamed`, includes no RRD, and reads `.env`."
        ),
        "statistics_upload_date": "Upload date (YYYYMMDD)",
        "statistics_skip_existing": "Skip existing remote Sessions",
        "statistics_batch_upload": "Upload clean Sessions one by one",
        "package_output": "Output zip",
        "package_height": "Proxy height",
        "package_crf": "Proxy CRF",
        "package_button": "Package",
        "mode": "Alignment mode",
        "ratio": "Ratio",
        "offset": "Offset (signed Robocap frames)",
        "offset_help": (
            "**Offset direction (Robocap video is the reference):** `+N` advances NOKOV/GT "
            "by N Robocap frames, so it appears earlier and the same video frame uses a later "
            "GT frame. `-N` delays NOKOV/GT by N Robocap frames, so it appears later and the "
            "same video frame uses an earlier GT frame. Internal source-script conversion: "
            "`GT frame offset = round(Robocap frame offset * ratio)`. **Frame-mode RRD uses "
            "`frame` as its primary timeline (GT/NOKOV frame scale): video frame N is at "
            "`round(N * ratio)` and GT source frame K is at `K - GT frame offset`; "
            "`capture_time` is secondary. Third-person video follows the same frame offset in frame-mode RRDs.**"
        ),
        "limit_robocap_frames": "Limit Robocap frame range",
        "robocap_start_frame": "Robocap start frame (0-based, inclusive)",
        "robocap_end_frame": "Robocap end frame (0-based, inclusive)",
        "save_path": "Save path",
        "use_proxy": "Use compressed proxy video",
        "interpolate_dropped_frames": "Interpolate dropped NOKOV frames (240 FPS)",
        "scan_button": "Scan files",
        "gt_dir": "GT/NOKOV export dir",
        "gt_files": "GT files to include",
        "third_person_video": "Third-person video",
        "include_third_person": "Include third-person video",
        "include_robowrist": "Include detected robowrist video and sensors",
        "include_mag": "Include MAG data",
        "include_imu": "Include IMU data",
        "export_height": "Proxy height",
        "export_button": "Export RRD",
        "set_default_offset_button": "Set as default",
        "default_offset_saved": "Default Robocap-frame Offset saved: {value}\nSettings: {path}",
        "nokov_source": "NOKOV source",
        "offset_button": "Inspect Offset",
        "offset_min": "Offset min",
        "offset_max": "Offset max",
        "sweep_button": "Sweep Offset",
        "env_check_button": "Check environment",
        "env_git_check_button": "Check code updates",
        "env_code_update_button": "Update code and restart",
        "env_install_button": "Install/update dependencies",
        "env_output": "Environment output",
        "report_scan_button": "Scan inspection reports",
        "report_open_button": "Open HTML report",
        "report_html_file": "Timestamp anomaly HTML",
        "viewer_scan_button": "Scan RRD files",
        "viewer_open_button": "Open web viewer",
        "viewer_rrd_file": "RRD file",
        "viewer_port": "Web viewer port (0 = auto)",
        "modelscope_primitive": (
            "Action primitive ([A-Z]NN auto-suggestion; custom value allowed)"
        ),
        "modelscope_repo_id": "ModelScope dataset repo (owner/name; blank keeps saved value)",
        "modelscope_endpoint": "ModelScope endpoint",
        "modelscope_revision": "Revision",
        "modelscope_token": "ModelScope token (blank keeps saved value)",
        "modelscope_token_status": "Token status",
        "modelscope_save_token": "Save ModelScope settings",
        "modelscope_clear_token": "Clear saved token",
        "modelscope_check_token": "Check authentication",
        "modelscope_aligned_intersection": "Stage aligned intersection only",
        "modelscope_intersection_ratio": "Intersection ratio",
        "modelscope_intersection_offset": "Intersection offset (signed Robocap frames)",
        "modelscope_intersection_help": (
            "The ratio and Offset are initially copied from the RRD Export tab and follow changes "
            "made there; edit these fields only when staging needs an explicit override. "
            "When enabled, staging keeps only the shared Robocap/mocap/third-person interval. "
            "`+N` advances mocap and third-person video, so their leading `N*ratio` and `N` "
            "source frames are removed; `-N` removes the unmatched leading Robocap frames. "
            "All videos and SQLite sensors are clipped to the resulting Robocap time window. "
            "The staged streams then have zero residual offset; source ranges remain in manifest.json. "
            "Source Session files are never modified."
        ),
        "modelscope_refresh_inspection": "Regenerate inspection HTML before preparing",
        "modelscope_scan_mocap": "Scan Mocap files",
        "modelscope_mocap_files": "Mocap files to upload",
        "modelscope_scan_rrd": "Scan RRD files",
        "modelscope_rrd_files": "RRD files to upload",
        "modelscope_use_cache": "Use resumable upload cache",
        "modelscope_max_workers": "Upload workers",
        "modelscope_stage": "Prepare session",
        "modelscope_upload": "Upload prepared dataset",
        "doc": EN_DOC,
    },
    "中文": {
        "title": "# Robocap Rerun Tools 中文界面",
        "language": "语言",
        "dataset_root": "数据集根目录",
        "scan_sessions_button": "扫描 Session",
        "session": "Session",
        "segment": "Segment",
        "output": "输出",
        "inspect_mocap_ratio": "检查动捕比例（8：240 FPS，4：120 FPS）",
        "inspect_button": "检查",
        "frame_compare_scan_button": "扫描视频",
        "frame_compare_videos": "视频（每个勾选的视频占一列）",
        "frame_compare_start_frame": "起始帧（从 0 开始，包含）",
        "frame_compare_end_frame": "结束帧（从 0 开始，包含）",
        "frame_compare_cell_width": "单格宽度",
        "frame_compare_cell_height": "单格高度",
        "frame_compare_generate_button": "生成帧对比图",
        "frame_compare_open_button": "使用默认应用打开图片",
        "frame_compare_file": "输出图片",
        "statistics_mocap_ratio": "生成检查报告使用的动捕比例",
        "statistics_fill_missing": "补做缺失的检查报告",
        "statistics_rebuild_all": "全部重做检查报告",
        "statistics_button": "统计根目录",
        "statistics_mocap_metadata": "Mocap 命名元数据",
        "statistics_mocap_metadata_help": (
            "按 `mocap-<动作:[A-Z]NN>-S<序号>-<采集员>-<次数>p` 解析。可修改动作、采集 Session "
            "序号、采集员和重复次数；勾选需要更新的行后，批量同步到远端 `metadata.jsonl` 与各 "
            "Session 的 `manifest.json`。Session 和 Mocap 目录用于定位本地数据，不可修改。该操作"
            "只更新元数据，不上传视频，也不移动远端目录。"
        ),
        "statistics_mocap_metadata_refresh": "扫描 Mocap 命名元数据",
        "statistics_mocap_metadata_update": "批量更新远端 Mocap 元数据",
        "statistics_batch_help": (
            "逐个上传先读取远端 metadata.jsonl。日期框默认填入本地当天的 `YYYYMMDD`，允许修改；"
            "本次所有 Session 使用同一日期。默认勾选跳过远端已有 Session；取消勾选后重新上传并"
            "替换同键元数据。改用其他日期不会删除旧远端目录。"
            "选中的 Session 必须满足 `n:ratio*(n+1):n+1`，每条依次完成准备、校验和上传后才处理"
            "下一条。上传失败"
            "会重试 3 次，共 4 次仍失败则跳过并继续。完整 Session 视频逐字节复制，默认选择"
            "BVH/CSV/TRC/MP4 并排除 `unnamed`，不包含 RRD；读取 `.env`。"
        ),
        "statistics_upload_date": "上传日期（YYYYMMDD）",
        "statistics_skip_existing": "跳过远端已有 Session",
        "statistics_batch_upload": "逐个上传无差帧 Session",
        "package_output": "输出 zip",
        "package_height": "压缩视频高度",
        "package_crf": "压缩 CRF",
        "package_button": "打包数据",
        "mode": "对齐模式",
        "ratio": "比例 ratio",
        "offset": "Offset（有符号 Robocap 帧）",
        "offset_help": (
            "**Offset 方向（以 Robocap 视频为基准）：** `+N` 表示 NOKOV/GT 相对 Robocap "
            "视频前移 N 帧、提前出现，同一视频帧会取更靠后的 GT 帧；`-N` 表示 NOKOV/GT "
            "相对 Robocap 视频后移 N 帧、延后出现，同一视频帧会取更靠前的 GT 帧。进入源脚本"
            "前转换为：`GT 帧 offset = round(Robocap 帧 offset * ratio)`。**帧对齐 RRD 的"
            "主时间轴是 GT/NOKOV 帧尺度的 `frame`：视频第 N 帧位于 `round(N * ratio)`，GT "
            "源数据第 K 帧位于 `K - GT 帧 offset`；`capture_time` 只作辅助时间轴。第三人称视频在帧对齐 RRD 中也使用同一个 offset，与 NOKOV/GT 一起前后移动。**"
        ),
        "limit_robocap_frames": "限制 Robocap 帧范围",
        "robocap_start_frame": "Robocap 起始帧（从 0 开始，包含）",
        "robocap_end_frame": "Robocap 结束帧（从 0 开始，包含）",
        "save_path": "RRD 保存路径",
        "use_proxy": "使用压缩视频",
        "interpolate_dropped_frames": "插值补齐 NOKOV 丢帧（240 FPS）",
        "scan_button": "扫描文件",
        "gt_dir": "GT/NOKOV 导出目录",
        "gt_files": "参与导出的 GT 文件",
        "third_person_video": "第三人称视频",
        "include_third_person": "包含第三人称视频",
        "include_robowrist": "包含检测到的 robowrist 视频与传感器",
        "include_mag": "包含 MAG 数据",
        "include_imu": "包含 IMU 数据",
        "export_height": "压缩视频高度",
        "export_button": "导出 RRD",
        "set_default_offset_button": "设为默认值",
        "default_offset_saved": "默认 Offset 已保存：{value} Robocap 帧\n配置文件：{path}",
        "nokov_source": "NOKOV 参考源",
        "offset_button": "检查 Offset",
        "offset_min": "Offset 最小值",
        "offset_max": "Offset 最大值",
        "sweep_button": "扫描 Offset",
        "env_check_button": "检查环境",
        "env_git_check_button": "检查代码更新",
        "env_code_update_button": "更新代码并重启",
        "env_install_button": "安装/更新依赖",
        "env_output": "环境输出",
        "report_scan_button": "扫描检查报告",
        "report_open_button": "打开 HTML 报告",
        "report_html_file": "时间戳异常 HTML",
        "viewer_scan_button": "扫描 RRD 文件",
        "viewer_open_button": "打开 Web Viewer",
        "viewer_rrd_file": "RRD 文件",
        "viewer_port": "Web Viewer 端口（0 = 自动）",
        "modelscope_primitive": "动作基元（从 mocap* 自动建议 A01/P01 等，可任意自定义）",
        "modelscope_repo_id": "ModelScope 数据集仓库（owner/name；留空保留已保存值）",
        "modelscope_endpoint": "ModelScope 站点",
        "modelscope_revision": "分支 / Revision",
        "modelscope_token": "ModelScope Token（留空保留已保存值）",
        "modelscope_token_status": "Token 状态",
        "modelscope_save_token": "保存 ModelScope 配置",
        "modelscope_clear_token": "清除已保存 Token",
        "modelscope_check_token": "检查身份",
        "modelscope_aligned_intersection": "仅暂存对齐后的交集",
        "modelscope_intersection_ratio": "交集比例 ratio",
        "modelscope_intersection_offset": "交集 Offset（有符号 Robocap 帧）",
        "modelscope_intersection_help": (
            "ratio 和 Offset 默认从“导出 RRD”页填入，并随该页参数变化自动同步；只有暂存需要"
            "单独覆盖时才在这里修改。"
            "启用后只暂存 Robocap、动捕和第三人称视频共同有效的区间。`+N` 表示动捕和"
            "第三人称视频前移，因此会删除其开头 `N*ratio` 帧和 `N` 帧；`-N` 会删除没有"
            "对应数据的 Robocap 开头帧。其他视频和 SQLite 传感器也裁到最终 Robocap "
            "时间窗。暂存副本的残余 offset 为 0，原始区间记录在 manifest.json；源 Session "
            "永远不会被修改。"
        ),
        "modelscope_refresh_inspection": "准备前重新生成检查 HTML",
        "modelscope_scan_mocap": "扫描 Mocap 文件",
        "modelscope_mocap_files": "参与上传的 Mocap 文件",
        "modelscope_scan_rrd": "扫描 RRD 文件",
        "modelscope_rrd_files": "参与上传的 RRD 文件",
        "modelscope_use_cache": "使用可恢复上传缓存",
        "modelscope_max_workers": "上传并发数",
        "modelscope_stage": "准备 Session",
        "modelscope_upload": "上传已准备数据集",
        "doc": ZH_DOC,
    },
}


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TIMESTAMP_REPORT_NAME = "timestamp_anomaly_detail_table.html"
DEFAULT_OFFSET = 5
OFFSET_UNIT = "robocap_video_frames"
LEGACY_OFFSET_RATIO = 8
WEB_SETTINGS_ENV = "ROBOCAP_RERUN_WEB_SETTINGS"
DATASET_ROOT_SETTING = "dataset_root"
SESSION_DIR_SETTING = "session_dir"
SESSION_SCAN_MAX_DEPTH = 6
SESSION_SCAN_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "_analysis",
        "_artifacts",
        "_modelscope_dataset",
        "build",
        "dist",
        "node_modules",
        "raw_calibration",
    }
)
STREAM_REFRESH_SECONDS = 0.5
STREAM_LOG_MAX_LINES = 1000
STREAM_LOG_MAX_CHARS = 256 * 1024
WEB_CSS = """
#statistics-result {
    max-width: 100%;
    overflow-x: auto;
}
#statistics-result table {
    min-width: 900px;
}
#statistics-result th,
#statistics-result td {
    white-space: nowrap;
}
"""
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
FRACTION_PROGRESS_PATTERN = re.compile(r"\[(\d+)\s*/\s*(\d+)\]")
PERCENT_PROGRESS_PATTERN = re.compile(r"(?<![\d.])(100(?:\.0+)?|\d{1,2}(?:\.\d+)?)%")
STREAM_END = object()


@dataclass(frozen=True)
class StreamCommandResult:
    returncode: int
    output: str
    rendered: str


class LiveCommandOutput:
    def __init__(self, display_command: list[str]) -> None:
        self.display_command = display_command
        self.lines: deque[str] = deque()
        self.character_count = 0
        self.dropped_lines = 0
        self.current: int | None = None
        self.total: int | None = None
        self.percent: float | None = None

    def add(self, value: str) -> None:
        line = ANSI_ESCAPE_PATTERN.sub("", value).replace("\b", "").strip()
        if not line:
            return
        if len(line) + 1 > STREAM_LOG_MAX_CHARS:
            line = line[-(STREAM_LOG_MAX_CHARS - 1) :]
        self.lines.append(line)
        self.character_count += len(line) + 1
        while len(self.lines) > STREAM_LOG_MAX_LINES or self.character_count > STREAM_LOG_MAX_CHARS:
            removed = self.lines.popleft()
            self.character_count -= len(removed) + 1
            self.dropped_lines += 1

        fraction = FRACTION_PROGRESS_PATTERN.search(line)
        if fraction is not None:
            current, total = (int(value) for value in fraction.groups())
            if total > 0:
                self.current = min(current, total)
                self.total = total
                self.percent = 100.0 * self.current / total
                return
        percentage = PERCENT_PROGRESS_PATTERN.search(line)
        if percentage is not None:
            self.percent = min(100.0, max(0.0, float(percentage.group(1))))

    def output(self) -> str:
        parts = list(self.lines)
        if self.dropped_lines:
            parts.insert(0, f"... {self.dropped_lines} earlier log lines omitted ...")
        return "\n".join(parts)

    def _progress_bar(self, elapsed: float, returncode: int | None) -> str:
        width = 30
        if returncode == 0:
            ratio = 1.0
        elif self.percent is not None:
            ratio = self.percent / 100.0
        else:
            ratio = None

        if ratio is None:
            if returncode is not None:
                return f"[{'!' + '.' * (width - 1)}] failed"
            position = int(elapsed * 4) % width
            cells = ["."] * width
            cells[position] = ">"
            return f"[{''.join(cells)}] working"

        filled = min(width, max(0, round(width * ratio)))
        bar = "#" * filled + "-" * (width - filled)
        detail = f" {self.current}/{self.total}" if self.total is not None else ""
        return f"[{bar}] {ratio * 100:5.1f}%{detail}"

    def render(self, elapsed: float, returncode: int | None) -> str:
        if returncode is None:
            status = "RUNNING"
        elif returncode == 0:
            status = "COMPLETED"
        else:
            status = f"FAILED (exit code {returncode})"
        elapsed_seconds = max(0, int(elapsed))
        hours, remainder = divmod(elapsed_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        elapsed_text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        command_text = subprocess.list2cmdline(self.display_command)
        log_text = self.output() or "(waiting for command output)"
        return "\n".join(
            [
                f"Command: {command_text}",
                f"Status: {status} | Elapsed: {elapsed_text}",
                f"Progress: {self._progress_bar(elapsed, returncode)}",
                "",
                "Log:",
                log_text,
            ]
        )


def ensure_localhost_no_proxy() -> None:
    required = ("127.0.0.1", "localhost")
    values: list[str] = []
    lowered: set[str] = set()
    for name in ("NO_PROXY", "no_proxy"):
        for item in os.environ.get(name, "").split(","):
            value = item.strip()
            if value and value.lower() not in lowered:
                values.append(value)
                lowered.add(value.lower())
    values.extend(item for item in required if item.lower() not in lowered)
    combined = ",".join(values)
    os.environ["NO_PROXY"] = combined
    os.environ["no_proxy"] = combined


def web_settings_path() -> Path:
    override = os.environ.get(WEB_SETTINGS_ENV)
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    base_dir = Path(local_app_data) if local_app_data else Path.home() / ".config"
    return base_dir / "robocap-rerun-tools" / "web_settings.json"


def load_web_settings(settings_path: Path | None = None) -> dict[str, object]:
    path = settings_path or web_settings_path()
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return settings if isinstance(settings, dict) else {}


def save_web_settings(settings: dict[str, object], settings_path: Path | None = None) -> Path:
    path = settings_path or web_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary_path.replace(path)
    return path


def normalize_offset(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError(  # noqa: TRY004 - callers use one validation exception type
            "Offset must be an integer frame count."
        )
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Offset must be an integer frame count.") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError("Offset must be an integer frame count.")
    return int(number)


def normalize_optional_frame_index(value: object) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    frame_index = normalize_offset(value)
    if frame_index < 0:
        raise ValueError("Robocap frame indexes must be non-negative (0-based).")
    return frame_index


def load_default_offset(settings_path: Path | None = None) -> int:
    settings = load_web_settings(settings_path)
    if "default_offset" not in settings:
        return DEFAULT_OFFSET
    try:
        value = normalize_offset(settings["default_offset"])
    except ValueError:
        return DEFAULT_OFFSET
    if settings.get("offset_unit") == OFFSET_UNIT:
        return value

    scaled = value / LEGACY_OFFSET_RATIO
    migrated = int(math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5))
    settings["default_offset"] = migrated
    settings["offset_unit"] = OFFSET_UNIT
    try:
        save_web_settings(settings, settings_path)
    except OSError:
        pass
    return migrated


def save_default_offset(offset: object, settings_path: Path | None = None) -> tuple[int, Path]:
    value = normalize_offset(offset)
    settings = load_web_settings(settings_path)
    settings["default_offset"] = value
    settings["offset_unit"] = OFFSET_UNIT
    return value, save_web_settings(settings, settings_path)


def _clean_directory_value(value: object) -> str:
    return str(value or "").strip().strip('"')


def dataset_root_path(value: object) -> Path:
    raw_value = _clean_directory_value(value)
    if not raw_value:
        raise ValueError("Dataset root directory is required.")
    path = Path(raw_value).expanduser()
    if not path.exists():
        raise ValueError(f"Dataset root does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Dataset root is not a directory: {path}")
    return path.resolve()


def is_robocap_session_dir(path: Path) -> bool:
    try:
        return any(
            child.is_file() and child.name.lower().startswith("robocap_")
            for child in path.iterdir()
        )
    except OSError:
        return False


def discover_session_directories(
    dataset_root: Path, *, max_depth: int = SESSION_SCAN_MAX_DEPTH
) -> list[Path]:
    root = dataset_root.expanduser().resolve()
    if max_depth < 0:
        raise ValueError("Session scan depth must be non-negative.")

    sessions: list[Path] = []
    visited: set[str] = set()

    def visit(path: Path, depth: int) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        identity = os.path.normcase(str(resolved))
        if identity in visited:
            return
        visited.add(identity)

        if is_robocap_session_dir(resolved):
            sessions.append(resolved)
            return
        if depth >= max_depth:
            return

        try:
            children = sorted(
                (
                    child
                    for child in resolved.iterdir()
                    if child.name.lower() not in SESSION_SCAN_SKIP_DIRS
                    and child.is_dir()
                    and not child.is_symlink()
                ),
                key=lambda child: child.name.casefold(),
            )
        except OSError:
            return
        for child in children:
            visit(child, depth + 1)

    visit(root, 0)
    return sorted(sessions, key=lambda path: str(path).casefold())


def session_dropdown_choices(dataset_root: Path, sessions: list[Path]) -> list[tuple[str, str]]:
    root = dataset_root.resolve()
    choices: list[tuple[str, str]] = []
    for session in sessions:
        resolved = session.resolve()
        try:
            label = str(resolved.relative_to(root))
        except ValueError:
            label = resolved.name
        if label == ".":
            label = resolved.name
        choices.append((label, str(resolved)))
    return choices


def _matching_session_value(value: object, sessions: list[Path]) -> str | None:
    candidate = _clean_directory_value(value)
    if not candidate:
        return None
    candidate_identity = os.path.normcase(os.path.abspath(candidate))
    for session in sessions:
        resolved = str(session.resolve())
        if os.path.normcase(os.path.abspath(resolved)) == candidate_identity:
            return resolved
    return None


def save_session_browser_settings(
    dataset_root: object,
    session_dir: object,
    settings_path: Path | None = None,
) -> Path:
    settings = load_web_settings(settings_path)
    root_value = _clean_directory_value(dataset_root)
    session_value = _clean_directory_value(session_dir)
    if root_value:
        settings[DATASET_ROOT_SETTING] = root_value
    else:
        settings.pop(DATASET_ROOT_SETTING, None)
    if session_value:
        settings[SESSION_DIR_SETTING] = session_value
    else:
        settings.pop(SESSION_DIR_SETTING, None)
    return save_web_settings(settings, settings_path)


def load_session_browser_settings(
    settings_path: Path | None = None,
) -> tuple[str, list[tuple[str, str]], str | None]:
    settings = load_web_settings(settings_path)
    root_value = settings.get(DATASET_ROOT_SETTING)
    session_value = settings.get(SESSION_DIR_SETTING)
    root = root_value if isinstance(root_value, str) else ""
    session = session_value if isinstance(session_value, str) and session_value else None
    if root:
        try:
            resolved_root = dataset_root_path(root)
            sessions = discover_session_directories(resolved_root)
        except (OSError, ValueError):
            sessions = []
        if sessions:
            selected = _matching_session_value(session, sessions) or str(sessions[0])
            return (
                str(resolved_root),
                session_dropdown_choices(resolved_root, sessions),
                selected,
            )
    if session is None:
        return root, [], None
    label = Path(session).name or session
    return root, [(label, session)], session


def scan_dataset_sessions(
    dataset_root: object,
    current_session: object,
    language: str = "中文",
    settings_path: Path | None = None,
) -> tuple[str, object]:
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc

    root = dataset_root_path(dataset_root)
    sessions = discover_session_directories(root)
    selected = _matching_session_value(current_session, sessions)
    if selected is None and sessions:
        selected = str(sessions[0])
    settings_file = save_session_browser_settings(root, selected, settings_path)
    choices = session_dropdown_choices(root, sessions)

    if language == "中文":
        message = (
            f"数据集根目录：{root}\n"
            f"识别到 Session：{len(sessions)}\n"
            f"当前 Session：{selected or '无'}\n"
            f"配置文件：{settings_file}"
        )
        if not sessions:
            message += "\n未找到根目录直接包含 robocap_* 源文件的 Session。"
    else:
        message = (
            f"Dataset root: {root}\n"
            f"Detected sessions: {len(sessions)}\n"
            f"Current session: {selected or 'none'}\n"
            f"Settings: {settings_file}"
        )
        if not sessions:
            message += "\nNo session containing direct robocap_* source files was found."
    return message, gr.update(choices=choices, value=selected)


def infer_modelscope_primitive(session_dir: object) -> str | None:
    candidate = Path(str(session_dir).strip().strip('"')).expanduser()
    if not candidate.is_dir():
        return None
    matches = {
        match.group(1).upper()
        for mocap_dir in discover_mocap_directories(candidate)
        if (match := MOCAP_PRIMITIVE_PATTERN.search(mocap_dir.name)) is not None
    }
    if matches:
        return next(iter(matches)) if len(matches) == 1 else None
    matches = {
        match.group(1).upper()
        for mocap_dir in discover_mocap_directories(candidate)
        if (match := MOCAP_FALLBACK_PRIMITIVE_PATTERN.search(mocap_dir.name)) is not None
    }
    return next(iter(matches)) if len(matches) == 1 else None


def select_session(
    dataset_root: object,
    session_dir: object,
    settings_path: Path | None = None,
) -> tuple[object, ...]:
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc

    save_session_browser_settings(dataset_root, session_dir, settings_path)
    inferred_primitive = infer_modelscope_primitive(session_dir)
    return (
        gr.update(value=""),
        gr.update(choices=[], value=[]),
        gr.update(value=""),
        gr.update(choices=[], value=None),
        gr.update(choices=[], value=None),
        gr.update(choices=[], value=[]),
        gr.update(choices=[], value=[]),
        gr.update(value=""),
        gr.update(value=True, interactive=True),
        gr.update(value=inferred_primitive) if inferred_primitive else gr.update(),
        gr.update(choices=[], value=[]),
        gr.update(value=None),
        gr.update(value=""),
        gr.update(interactive=False),
    )


def run_process(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        return 127, str(exc)
    output_parts = []
    if proc.stdout:
        output_parts.append(proc.stdout.rstrip())
    if proc.stderr:
        output_parts.append(proc.stderr.rstrip())
    return proc.returncode, "\n".join(output_parts)


def _read_process_stream(stream: TextIO, events: queue.Queue[object]) -> None:
    buffer: list[str] = []
    try:
        while True:
            character = stream.read(1)
            if not character:
                if buffer:
                    events.put("".join(buffer))
                return
            if character in {"\r", "\n"}:
                if buffer:
                    events.put("".join(buffer))
                    buffer.clear()
                continue
            buffer.append(character)
    finally:
        try:
            stream.close()
        finally:
            events.put(STREAM_END)


def stream_process_output(
    args: list[str],
    *,
    cwd: Path | None = None,
    display_command: list[str] | None = None,
) -> Generator[str, None, StreamCommandResult]:
    live = LiveCommandOutput(display_command or args)
    started = time.monotonic()
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
        )
    except OSError as exc:
        live.add(str(exc))
        live.add("Command failed with exit code 127.")
        rendered = live.render(time.monotonic() - started, 127)
        yield rendered
        return StreamCommandResult(127, live.output(), rendered)

    assert process.stdout is not None
    events: queue.Queue[object] = queue.Queue()
    reader = threading.Thread(
        target=_read_process_stream,
        args=(process.stdout, events),
        name=f"robocap-web-stream-{process.pid}",
        daemon=True,
    )
    reader.start()
    rendered = live.render(0.0, None)
    yield rendered
    next_render = time.monotonic() + STREAM_REFRESH_SECONDS
    reader_done = False

    try:
        while True:
            now = time.monotonic()
            wait_seconds = max(0.0, min(0.2, next_render - now))
            try:
                item = events.get(timeout=wait_seconds)
                if item is STREAM_END:
                    reader_done = True
                else:
                    live.add(str(item))
            except queue.Empty:
                pass

            while True:
                try:
                    item = events.get_nowait()
                except queue.Empty:
                    break
                if item is STREAM_END:
                    reader_done = True
                else:
                    live.add(str(item))

            returncode = process.poll()
            done = returncode is not None and reader_done and events.empty()
            now = time.monotonic()
            if now >= next_render or done:
                if done and returncode != 0:
                    live.add(f"Command failed with exit code {returncode}.")
                elif done and not live.lines:
                    live.add("Done.")
                rendered = live.render(now - started, returncode if done else None)
                yield rendered
                next_render = now + STREAM_REFRESH_SECONDS
            if done:
                return StreamCommandResult(returncode, live.output(), rendered)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join()


def stream_cli_command(args: list[str]) -> Generator[str, None, StreamCommandResult]:
    command = [sys.executable, "-u", "-m", "robocap_rerun_tools.cli", *args]
    display_command = ["robocap-rerun", *args]
    return (yield from stream_process_output(command, display_command=display_command))


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def first_line(command: list[str], cwd: Path | None = None) -> str:
    returncode, output = run_process(command, cwd=cwd)
    line = output.splitlines()[0] if output else ""
    return line if returncode == 0 else f"failed: {line or returncode}"


def git_repository_report(*, fetch: bool = False) -> str:
    git = shutil.which("git")
    if not git:
        return "## Git repository\n\n- git: `not found`"
    if not (PROJECT_ROOT / ".git").exists():
        return f"## Git repository\n\n- repository: `not found at {PROJECT_ROOT}`"

    lines = ["## Git repository", "", f"- root: `{PROJECT_ROOT}`"]
    fetch_code: int | None = None
    if fetch:
        fetch_code, fetch_output = run_process(
            [git, "fetch", "--prune", "origin"], cwd=PROJECT_ROOT
        )
        lines.append(f"- fetch_origin: `{'ok' if fetch_code == 0 else 'failed'}`")
        if fetch_output:
            lines.append(f"- fetch_output: `{fetch_output.splitlines()[-1]}`")

    def value(arguments: list[str], fallback: str = "unavailable") -> str:
        returncode, output = run_process([git, *arguments], cwd=PROJECT_ROOT)
        return output.strip() if returncode == 0 and output.strip() else fallback

    branch = value(["branch", "--show-current"], "detached HEAD")
    commit = value(["rev-parse", "--short=12", "HEAD"])
    remote = value(["remote", "get-url", "origin"], "origin not configured")
    upstream = value(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        "not configured",
    )
    status_code, status_output = run_process([git, "status", "--porcelain"], cwd=PROJECT_ROOT)
    status_lines = status_output.splitlines() if status_code == 0 else []
    lines.extend(
        [
            f"- branch: `{branch}`",
            f"- commit: `{commit}`",
            f"- remote_origin: `{remote}`",
            f"- upstream: `{upstream}`",
            f"- working_tree: `{'clean' if not status_lines else f'dirty ({len(status_lines)} paths)'}`",
        ]
    )

    if upstream != "not configured":
        counts = value(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], "")
        try:
            ahead_text, behind_text = counts.split()
            ahead, behind = int(ahead_text), int(behind_text)
        except (TypeError, ValueError):
            status = (
                "unknown (fetch failed; remote-tracking data may be stale)"
                if fetch and fetch_code != 0
                else "unavailable"
            )
            lines.append(f"- update_status: `{status}`")
        else:
            if fetch and fetch_code != 0:
                lines.extend(
                    [
                        f"- cached_ahead: `{ahead}`",
                        f"- cached_behind: `{behind}`",
                        "- update_status: `unknown (fetch failed; remote-tracking data may be stale)`",
                    ]
                )
            else:
                if ahead and behind:
                    update_status = "diverged; manual Git resolution required"
                elif behind:
                    update_status = f"update available ({behind} commits behind)"
                elif ahead:
                    update_status = f"local branch ahead by {ahead} commits"
                else:
                    update_status = "up to date"
                lines.extend(
                    [
                        f"- ahead: `{ahead}`",
                        f"- behind: `{behind}`",
                        f"- update_status: `{update_status}`",
                    ]
                )
    if status_lines:
        lines.extend(["", "### Local changes", "", "```text", *status_lines[:50], "```"])
        if len(status_lines) > 50:
            lines.append(f"... {len(status_lines) - 50} more paths")
    return "\n".join(lines)


def check_environment() -> str:
    packages = (
        "robocap-rerun-tools",
        "numpy",
        "rerun-sdk",
        "scipy",
        "gradio",
        "modelscope-hub",
        "python-dotenv",
        "ffmpeg-binaries-compat",
    )
    lines = [
        "# Environment check",
        "",
        f"- python: `{sys.executable}`",
        f"- python_version: `{platform.python_version()}`",
        f"- platform: `{platform.platform()}`",
        f"- virtual_env: `{os.environ.get('VIRTUAL_ENV', '') or 'not set'}`",
        f"- uv: `{shutil.which('uv') or 'not found'}`",
        f"- ffmpeg: `{shutil.which('ffmpeg') or 'not found'}`",
        f"- ffprobe: `{shutil.which('ffprobe') or 'not found'}`",
        f"- ffmpeg_source: `{MEDIA_TOOLS.source}`",
        f"- git: `{shutil.which('git') or 'not found'}`",
        "",
        "## Tool versions",
        "",
        f"- uv: `{first_line(['uv', '--version']) if shutil.which('uv') else 'not found'}`",
        f"- ffmpeg: `{first_line(['ffmpeg', '-version']) if shutil.which('ffmpeg') else 'not found'}`",
        f"- ffprobe: `{first_line(['ffprobe', '-version']) if shutil.which('ffprobe') else 'not found'}`",
        f"- git: `{first_line(['git', '--version']) if shutil.which('git') else 'not found'}`",
        "",
        "## Python packages",
        "",
    ]
    lines.extend(f"- {name}: `{package_version(name)}`" for name in packages)
    lines.extend(["", git_repository_report(fetch=False)])
    return "\n".join(lines)


def check_code_updates() -> str:
    return git_repository_report(fetch=True)


def install_or_update_dependencies() -> str:
    return launch_update_window("dependencies")


def update_code_and_restart() -> str:
    git = shutil.which("git")
    if not git:
        return "Git executable was not found. Install Git before updating code."
    if not (PROJECT_ROOT / ".git").exists():
        return f"Git repository was not found: {PROJECT_ROOT}"
    returncode, output = run_process([git, "status", "--porcelain"], cwd=PROJECT_ROOT)
    if returncode != 0:
        return f"Failed to inspect Git working tree:\n{output}"
    if output.strip():
        return (
            "Working tree is not clean. Commit or stash local changes before updating code.\n\n"
            "No process was stopped and no files were changed.\n\n"
            f"```text\n{output.strip()}\n```"
        )
    return launch_update_window("code")


def launch_update_window(mode: str) -> str:
    if mode not in {"dependencies", "code"}:
        raise ValueError(f"Unsupported update mode: {mode}")
    script = PROJECT_ROOT / "scripts" / "web_update_and_restart.bat"
    if not script.exists():
        return f"Update script not found: {script}"
    temporary_dir = Path(tempfile.gettempdir()) / "robocap-rerun-tools"
    temporary_dir.mkdir(parents=True, exist_ok=True)
    temporary_script = temporary_dir / f"web_update_and_restart_{os.getpid()}.bat"
    shutil.copy2(script, temporary_script)
    command = [
        "cmd.exe",
        "/c",
        "start",
        "Robocap Rerun Tools Update",
        str(temporary_script),
        str(os.getpid()),
        mode,
        str(PROJECT_ROOT),
    ]
    try:
        subprocess.Popen(command, cwd=PROJECT_ROOT)
    except OSError as exc:
        return f"Failed to launch update window: {exc}"
    action = "pull code and update dependencies" if mode == "code" else "update dependencies"
    return (
        f"Opened a separate cmd window to {action}.\n\n"
        "That window will close this running web process, print all logs, and restart with start_web.bat."
    )


def session_path(value: str) -> str:
    path = Path(value.strip().strip('"'))
    if not path.exists():
        raise ValueError(f"Session path does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Session path is not a directory: {path}")
    return str(path)


def scan_frame_comparison_videos(session_dir: str, language: str) -> tuple[str, object]:
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc

    session = Path(session_path(session_dir)).resolve()
    try:
        videos = discover_video_files(session)
    except FrameComparisonError as exc:
        return str(exc), gr.update(choices=[], value=[])
    choices = [(video.relative_to(session).as_posix(), str(video)) for video in videos]
    if language == "中文":
        lines = [f"Session：{session}", f"检测到视频：{len(videos)}"]
    else:
        lines = [f"Session: {session}", f"Detected videos: {len(videos)}"]
    lines.extend(f"- {label}" for label, _value in choices)
    return "\n".join(lines), gr.update(choices=choices, value=[])


def generate_frame_comparison(
    session_dir: str,
    selected_videos: list[str] | None,
    start_frame: object,
    end_frame: object,
    cell_width: object,
    cell_height: object,
    language: str,
) -> Iterator[tuple[str, str | None, str, object]]:
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc

    try:
        session = Path(session_path(session_dir)).resolve()
        progress_stream = iter_frame_comparison(
            selected_videos or [],
            start_frame,
            end_frame,
            cell_width=cell_width,
            cell_height=cell_height,
            output_dir=session / "_artifacts" / "frame_comparison",
        )
        for progress in progress_stream:
            ratio = progress.completed / progress.total
            width = 30
            filled = min(width, max(0, round(width * ratio)))
            bar = "#" * filled + "-" * (width - filled)
            if progress.output_path is None:
                if language == "中文":
                    status = (
                        "状态：生成中\n"
                        f"[{bar}] {ratio * 100:5.1f}% {progress.completed}/{progress.total}\n"
                        f"视频：{progress.video_index}/{progress.video_count} "
                        f"{progress.video_path.name}\n"
                        f"当前帧：{progress.frame_index}"
                    )
                else:
                    status = (
                        "Status: RUNNING\n"
                        f"[{bar}] {ratio * 100:5.1f}% {progress.completed}/{progress.total}\n"
                        f"Video: {progress.video_index}/{progress.video_count} "
                        f"{progress.video_path.name}\n"
                        f"Current frame: {progress.frame_index}"
                    )
                yield status, None, "", gr.update(interactive=False)
                continue

            result = str(progress.output_path)
            size_mib = progress.output_path.stat().st_size / (1024 * 1024)
            if language == "中文":
                status = (
                    "状态：已完成\n"
                    f"[{bar}] 100.0% {progress.total}/{progress.total}\n"
                    f"列数：{progress.video_count}\n"
                    f"帧范围：{start_frame}-{end_frame}（包含）\n"
                    f"文件：{result}\n"
                    f"大小：{size_mib:.2f} MiB"
                )
            else:
                status = (
                    "Status: COMPLETED\n"
                    f"[{bar}] 100.0% {progress.total}/{progress.total}\n"
                    f"Columns: {progress.video_count}\n"
                    f"Frame range: {start_frame}-{end_frame} (inclusive)\n"
                    f"File: {result}\n"
                    f"Size: {size_mib:.2f} MiB"
                )
            yield status, result, result, gr.update(interactive=True)
    except (FrameComparisonError, OSError, ValueError) as exc:
        prefix = "帧对比生成失败" if language == "中文" else "Frame comparison failed"
        yield f"{prefix}: {exc}", None, "", gr.update(interactive=False)


def launch_default_application(path: Path) -> None:
    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise OSError("Windows default-application launcher is unavailable.")
        startfile(str(path))
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    opener = shutil.which("xdg-open")
    if opener is None:
        raise OSError("xdg-open is unavailable.")
    subprocess.Popen([opener, str(path)])


def open_frame_comparison_image(session_dir: str, image_path: str, language: str) -> str:
    session = Path(session_path(session_dir)).resolve()
    allowed_directory = (session / "_artifacts" / "frame_comparison").resolve()
    path = Path((image_path or "").strip().strip('"')).expanduser().resolve()
    if not path.is_file():
        message = f"图片不存在：{path}" if language == "中文" else f"Image does not exist: {path}"
        return message
    try:
        path.relative_to(allowed_directory)
    except ValueError:
        message = (
            f"图片不在当前 Session 的帧对比输出目录中：{path}"
            if language == "中文"
            else f"Image is outside the current Session frame-comparison directory: {path}"
        )
        return message
    if path.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}:
        message = f"不是支持的图片：{path}" if language == "中文" else f"Unsupported image: {path}"
        return message
    try:
        launch_default_application(path)
    except OSError as exc:
        prefix = "无法使用默认应用打开图片" if language == "中文" else "Could not open image"
        return f"{prefix}: {exc}"
    return (
        f"已使用默认应用打开图片：\n{path}"
        if language == "中文"
        else f"Opened image in the default application:\n{path}"
    )


def optional_text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def rrd_alignment_defaults(ratio: object, offset: object) -> tuple[str, int]:
    ratio_text = "" if ratio is None else str(ratio)
    return optional_text(ratio_text) or "auto", normalize_offset(offset)


def default_gt_dir(path: Path) -> Path | None:
    try:
        from robocap_rerun_tools.exporter import discover_gt_dir

        return discover_gt_dir(path, None)
    except ValueError:
        return None


def detected_robowrist_streams(session_dir: Path, segment: str) -> tuple[str, ...]:
    from robocap_rerun_tools.exporter import discover_session, robowrist_stream_labels

    try:
        config = discover_session(
            session_dir,
            optional_text(segment),
            include_robowrist=True,
            include_mag=True,
            include_imu=True,
        )
    except (OSError, ValueError):
        return ()
    return robowrist_stream_labels(config)


def scan_files(
    session_dir: str,
    segment: str,
    gt_dir_value: str,
    include_robowrist: bool,
) -> tuple[str, object, str, str, object]:
    path = Path(session_path(session_dir))
    robowrist_streams = detected_robowrist_streams(path, segment)
    has_robowrist = bool(robowrist_streams)
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc
    robowrist_update = gr.update(
        value=bool(include_robowrist and has_robowrist),
        interactive=has_robowrist,
    )
    gt_dir = (
        Path(gt_dir_value.strip().strip('"'))
        if optional_text(gt_dir_value)
        else default_gt_dir(path)
    )
    if gt_dir is None or not gt_dir.exists():
        return (
            "No single GT/NOKOV data directory found. Fill GT/NOKOV export dir manually.",
            gr.update(choices=[], value=[]),
            "",
            "",
            robowrist_update,
        )

    gt_suffixes = {".bvh", ".trc", ".csv", ".xrs"}
    gt_files = sorted(
        file
        for file in gt_dir.rglob("*")
        if file.is_file()
        and "_artifacts" not in file.relative_to(gt_dir).parts
        and file.suffix.lower() in gt_suffixes
    )
    choices = [str(file.relative_to(gt_dir)) for file in gt_files]
    videos = sorted(file for file in gt_dir.rglob("*.mp4") if file.is_file())
    third_person = str(videos[0]) if videos else ""
    robowrist_count = len(list(path.glob("robowrist_*")))
    summary = "\n".join(
        [
            f"GT dir: {gt_dir}",
            f"GT files: {len(choices)}",
            f"Third-person video candidates: {len(videos)}",
            f"Robowrist folders: {robowrist_count}",
            f"Robowrist streams: {len(robowrist_streams)}",
            *(f"- {label}" for label in robowrist_streams),
        ]
    )
    return (
        summary,
        gr.update(choices=choices, value=choices),
        str(gt_dir),
        third_person,
        robowrist_update,
    )


def scan_rrd_files(session_dir: str) -> tuple[str, object]:
    path = Path(session_path(session_dir))
    rrd_files = sorted(
        (
            file
            for file in path.rglob("*.rrd")
            if file.is_file() and ".venv" not in file.relative_to(path).parts
        ),
        key=lambda file: (file.stat().st_mtime_ns, str(file).lower()),
        reverse=True,
    )
    choices = [str(file) for file in rrd_files]
    summary = "\n".join(
        [
            f"Session: {path}",
            f"RRD files: {len(choices)}",
            *(f"- {choice}" for choice in choices[:50]),
        ]
    )
    if len(choices) > 50:
        summary += f"\n... {len(choices) - 50} more"
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc
    return summary, gr.update(choices=choices, value=choices[0] if choices else None)


def scan_modelscope_rrd_files(session_dir: str, segment: str) -> tuple[str, object]:
    from robocap_rerun_tools.modelscope_publisher import find_rerun_files

    path = Path(session_path(session_dir)).resolve()
    rrd_files = find_rerun_files(path, optional_text(segment))
    choices = [str(file.relative_to(path)) for file in rrd_files]
    summary = "\n".join(
        [
            f"Session: {path}",
            f"Segment: {optional_text(segment) or 'all'}",
            f"Selectable RRD files: {len(choices)}",
            *(f"- {choice}" for choice in choices),
        ]
    )
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc
    return summary, gr.update(choices=choices, value=choices)


def default_modelscope_mocap_files(session_dir: Path) -> list[Path]:
    from robocap_rerun_tools.modelscope_publisher import find_mocap_files

    path = session_dir.expanduser().resolve()
    return [
        file.relative_to(path)
        for file in find_mocap_files(path)
        if file.suffix.casefold() in DEFAULT_SELECTED_MOCAP_SUFFIXES
        and "unnamed" not in file.relative_to(path).as_posix().casefold()
    ]


def scan_modelscope_mocap_files(session_dir: str) -> tuple[str, object]:
    from robocap_rerun_tools.modelscope_publisher import find_mocap_files

    path = Path(session_path(session_dir)).resolve()
    relative_files = [file.relative_to(path) for file in find_mocap_files(path)]
    choices = [str(file) for file in relative_files]
    default_selection = [str(file) for file in default_modelscope_mocap_files(path)]
    summary = "\n".join(
        [
            f"Session: {path}",
            f"Selectable Mocap files: {len(choices)}",
            f"Default-selected Mocap files: {len(default_selection)}",
            *(f"- {choice}" for choice in choices),
        ]
    )
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc
    return summary, gr.update(choices=choices, value=default_selection)


def timestamp_report_path(session_dir: Path, segment: str | None) -> Path:
    return session_dir / "_artifacts" / (segment or "all") / "inspection" / TIMESTAMP_REPORT_NAME


def scan_timestamp_reports(session_dir: str) -> tuple[str, object]:
    path = Path(session_path(session_dir))
    reports = sorted(
        (
            file
            for file in path.rglob(TIMESTAMP_REPORT_NAME)
            if file.is_file() and ".venv" not in file.relative_to(path).parts
        ),
        key=lambda file: (file.stat().st_mtime_ns, str(file).lower()),
        reverse=True,
    )
    choices = [str(file) for file in reports]
    summary = "\n".join(
        [
            f"Session: {path}",
            f"Timestamp anomaly reports: {len(choices)}",
            *(f"- {choice}" for choice in choices[:50]),
        ]
    )
    if len(choices) > 50:
        summary += f"\n... {len(choices) - 50} more"
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc
    return summary, gr.update(choices=choices, value=choices[0] if choices else None)


def timestamp_report_choices(session_dir: str) -> object:
    return scan_timestamp_reports(session_dir)[1]


def open_timestamp_report(html_file: str) -> str:
    path = Path((html_file or "").strip().strip('"')).resolve()
    if not path.is_file():
        raise ValueError(f"HTML report does not exist: {path}")
    if path.name != TIMESTAMP_REPORT_NAME:
        raise ValueError(f"Expected {TIMESTAMP_REPORT_NAME}: {path}")
    uri = path.as_uri()
    if not webbrowser.open(uri, new=2):
        return f"Default browser did not accept the report URL: {uri}"
    return f"Opened timestamp anomaly report:\n{path}"


def tcp_port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def available_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def resolve_web_server_port(port: int | None) -> int:
    if port in (None, 0):
        return available_tcp_port()
    if port < 1 or port > 65535:
        raise ValueError(f"Web server port must be from 0 to 65535: {port}")
    return port


def choose_web_viewer_port(viewer_port: object) -> tuple[int, str]:
    try:
        port = normalize_offset(viewer_port)
    except ValueError as exc:
        raise ValueError("Web viewer port must be an integer from 0 to 65535.") from exc
    if port < 0 or port > 65535:
        raise ValueError(f"Invalid port: {port}")
    if port != 0 and tcp_port_is_available(port):
        return port, f"Using requested web viewer port: {port}"

    selected = available_tcp_port()
    if port == 0:
        return selected, f"Auto-selected web viewer port: {selected}"
    return selected, f"Requested port {port} is unavailable; using {selected} instead."


def open_rerun_webviewer(rrd_file: str, viewer_port: int) -> tuple[str, int]:
    path = Path((rrd_file or "").strip().strip('"'))
    if not path.exists():
        raise ValueError(f"RRD file does not exist: {path}")
    if path.suffix.lower() != ".rrd":
        raise ValueError(f"Expected an .rrd file: {path}")
    port, port_message = choose_web_viewer_port(viewer_port)
    script = PROJECT_ROOT / "scripts" / "open_rerun_webviewer.bat"
    if not script.exists():
        return f"Viewer script not found: {script}", port
    command = [
        "cmd.exe",
        "/c",
        "start",
        "Rerun Web Viewer",
        str(script),
        str(path),
        str(port),
        sys.executable,
    ]
    try:
        subprocess.Popen(command, cwd=PROJECT_ROOT)
    except OSError as exc:
        return f"Failed to open Rerun web viewer: {exc}", port
    return (
        "\n".join(
            [
                "Opened a separate cmd window for Rerun web viewer.",
                port_message,
                f"RRD: {path}",
                "Rerun opens the connected recording in your default browser automatically.",
                "The cmd window prints Rerun logs and stays open if the viewer exits.",
            ]
        ),
        port,
    )


def inspect_session(session_dir: str, segment: str, mocap_ratio: int = 8) -> Iterator[str]:
    resolved_session = Path(session_path(session_dir))
    resolved_segment = optional_text(segment)
    args = ["inspect", str(resolved_session)]
    if resolved_segment:
        args.extend(["--segment", resolved_segment])
    args.extend(["--mocap-ratio", str(int(mocap_ratio))])
    result = yield from stream_cli_command(args)
    if result.returncode != 0:
        return

    report_path = timestamp_report_path(resolved_session, resolved_segment)
    if not report_path.is_file():
        yield (
            f"{result.rendered}\n\nInspection command succeeded, but the HTML report was not found:\n"
            f"`{report_path}`"
        )
        return
    yield f"{result.rendered}\n\nTimestamp anomaly HTML: `{report_path}`"


def inspection_report_mode(fill_missing_reports: bool, rebuild_all_reports: bool) -> str:
    if rebuild_all_reports:
        return "all"
    if fill_missing_reports:
        return "missing"
    return "none"


def _statistics_session_label(dataset_root: Path, session_dir: Path) -> str:
    try:
        relative = session_dir.resolve().relative_to(dataset_root.resolve()).as_posix()
    except (OSError, ValueError):
        return session_dir.name
    return relative if relative != "." else session_dir.name


def statistics_mocap_metadata_rows(dataset_root: object) -> list[list[object]]:
    root = dataset_root_path(dataset_root)
    rows: list[list[object]] = []
    for session in discover_session_directories(root):
        session_label = _statistics_session_label(root, session)
        mocap_dirs = discover_mocap_directories(session)
        primitive = infer_batch_modelscope_primitive(root, session)
        if not mocap_dirs:
            rows.append(
                [False, session_label, "", "", None, "", None, "MISSING mocap* directory"]
            )
            continue
        ambiguous = len(mocap_dirs) > 1
        for mocap_dir in mocap_dirs:
            capture = parse_mocap_capture_directory(mocap_dir.name)
            if capture is None:
                rows.append(
                    [
                        False,
                        session_label,
                        mocap_dir.name,
                        "",
                        None,
                        "",
                        None,
                        "INVALID expected mocap-[A-Z]NN-S<index>-<collector>-<count>p",
                    ]
                )
                continue
            if ambiguous:
                status = "AMBIGUOUS multiple mocap* directories"
            elif primitive is None:
                status = "UNRESOLVED remote primitive_id"
            else:
                status = f"OK remote key {primitive}/{session.name}"
            rows.append(
                [
                    not ambiguous and primitive is not None,
                    session_label,
                    capture.source_directory,
                    capture.action_id,
                    capture.collection_session_index,
                    capture.collector,
                    capture.repetition_count,
                    status,
                ]
            )
    return rows


def scan_statistics_mocap_metadata(
    dataset_root: object,
    language: str,
) -> tuple[str, list[list[object]]]:
    root = dataset_root_path(dataset_root)
    rows = statistics_mocap_metadata_rows(root)
    selected = sum(bool(row[0]) for row in rows)
    if language == "中文":
        message = f"Mocap 命名元数据：共 {len(rows)} 行，可更新 {selected} 行。"
    else:
        message = f"Mocap naming metadata: {len(rows)} row(s), {selected} updateable."
    return message, rows


def _dataframe_rows(value: object) -> list[list[object]]:
    if value is None:
        return []
    if hasattr(value, "values") and hasattr(value.values, "tolist"):
        value = value.values.tolist()
    elif hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, list):
        raise TypeError("Mocap metadata table must be a two-dimensional table.")
    rows: list[list[object]] = []
    for row in value:
        if not isinstance(row, (list, tuple)):
            raise TypeError("Mocap metadata table contains an invalid row.")
        rows.append(list(row))
    return rows


def _table_row_selected(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def build_remote_mocap_metadata_updates(
    dataset_root: object,
    table: object,
) -> tuple[object, ...]:
    from robocap_rerun_tools.modelscope_publisher import (
        RemoteMocapMetadataUpdate,
        validate_session_id,
    )

    root = dataset_root_path(dataset_root)
    sessions = discover_session_directories(root)
    sessions_by_label = {
        _statistics_session_label(root, session): session for session in sessions
    }
    updates: list[RemoteMocapMetadataUpdate] = []
    seen: set[tuple[str, str]] = set()
    for row_number, row in enumerate(_dataframe_rows(table), start=1):
        if not row or not _table_row_selected(row[0]):
            continue
        if len(row) < len(STATISTICS_MOCAP_METADATA_HEADERS):
            raise ValueError(f"Mocap metadata row {row_number} has missing columns.")
        session_label = str(row[1]).strip().replace("\\", "/")
        session = sessions_by_label.get(session_label)
        if session is None:
            raise ValueError(
                f"Mocap metadata row {row_number} refers to an unknown Session: {session_label}"
            )
        mocap_name = str(row[2]).strip()
        mocap_dirs = discover_mocap_directories(session)
        if len(mocap_dirs) != 1 or mocap_dirs[0].name != mocap_name:
            raise ValueError(
                f"Mocap metadata row {row_number} no longer matches the Session's unique "
                "mocap* directory. Scan the table again."
            )
        primitive = infer_batch_modelscope_primitive(root, session)
        if primitive is None:
            raise ValueError(
                f"Mocap metadata row {row_number} has no unambiguous remote primitive_id."
            )
        session_id = validate_session_id(session.name)
        key = (primitive, session_id)
        if key in seen:
            raise ValueError(f"Duplicate selected remote Session: {primitive}/{session_id}.")
        seen.add(key)
        capture = build_mocap_capture_metadata(
            mocap_name,
            row[3],
            row[4],
            row[5],
            row[6],
        )
        updates.append(RemoteMocapMetadataUpdate(primitive, session_id, capture))
    if not updates:
        raise ValueError("Select at least one valid Mocap metadata row to update.")
    return tuple(updates)


def batch_update_remote_mocap_metadata(
    dataset_root: object,
    table: object,
    repo_id: str,
    revision: str,
    use_cache: bool,
    max_workers: int,
    language: str,
) -> Iterator[str]:
    from robocap_rerun_tools.modelscope_publisher import (
        ModelScopePublisherError,
        update_remote_mocap_metadata,
    )

    is_chinese = language == "中文"
    try:
        updates = build_remote_mocap_metadata_updates(dataset_root, table)
        workers = int(max_workers)
        if float(max_workers) != workers or workers < 1:
            raise ValueError("ModelScope max workers must be a positive integer.")
    except (OSError, TypeError, ValueError) as exc:
        yield f"元数据校验失败：{exc}" if is_chinese else f"Metadata validation failed: {exc}"
        return

    events: queue.Queue[object] = queue.Queue()
    finished = object()
    outcome: dict[str, object] = {}

    def progress(message: str) -> None:
        events.put(str(message))

    def worker() -> None:
        try:
            outcome["result"] = update_remote_mocap_metadata(
                updates,
                repo_id or None,
                revision=revision,
                max_workers=workers,
                use_cache=bool(use_cache),
                progress=progress,
            )
        except (OSError, ValueError, ModelScopePublisherError) as exc:
            outcome["error"] = exc
        finally:
            events.put(finished)

    history: deque[str] = deque(maxlen=STREAM_LOG_MAX_LINES)
    history.append(
        f"准备更新 {len(updates)} 条远端 Mocap 元数据。"
        if is_chinese
        else f"Preparing {len(updates)} remote Mocap metadata update(s)."
    )
    yield "\n".join(history)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while True:
        event = events.get()
        if event is finished:
            break
        history.extend(line for line in str(event).splitlines() if line.strip())
        yield "\n".join(history)
    thread.join()

    error = outcome.get("error")
    if error is not None:
        history.append(
            f"远端元数据更新失败：{error}"
            if is_chinese
            else f"Remote metadata update failed: {error}"
        )
        yield "\n".join(history)
        return

    result = outcome["result"]
    updated = len(result.updated_keys)
    unchanged = len(result.unchanged_keys)
    missing = len(result.missing_keys)
    if is_chinese:
        history.append(
            f"远端元数据更新完成：更新 {updated}，无需变更 {unchanged}，远端不存在 {missing}。"
        )
        history.append(f"数据集：{result.repo_url}；分支：{result.revision}")
    else:
        history.append(
            f"Remote metadata update complete: {updated} updated, {unchanged} unchanged, "
            f"{missing} missing remotely."
        )
        history.append(f"Dataset: {result.repo_url}; revision: {result.revision}")
    for primitive, session_id in result.missing_keys:
        history.append(f"- missing: {primitive}/{session_id}")
    yield "\n".join(history)


def calculate_dataset_statistics(
    dataset_root: object,
    mocap_ratio: int,
    fill_missing_reports: bool,
    language: str = "中文",
    rebuild_all_reports: bool = False,
) -> Iterator[tuple[str, str]]:
    from robocap_rerun_tools.cli import resolve_ffprobe
    from robocap_rerun_tools.dataset_statistics import (
        aggregate_by_primitive,
        discover_segment_references,
        render_statistics_markdown,
        summarize_session,
    )

    root = dataset_root_path(dataset_root)
    sessions = discover_session_directories(root)
    try:
        ratio = int(mocap_ratio)
    except (TypeError, ValueError) as exc:
        raise ValueError("Statistics Mocap ratio must be 4 or 8.") from exc
    if ratio not in {4, 8}:
        raise ValueError("Statistics Mocap ratio must be 4 or 8.")

    is_chinese = language == "中文"
    history: deque[str] = deque(maxlen=STREAM_LOG_MAX_LINES)

    def add(message: str) -> None:
        history.extend(line for line in message.splitlines() if line.strip())

    def output(current: str = "") -> str:
        rendered = "\n".join(history)
        if current:
            rendered = f"{rendered}\n\n{current}" if rendered else current
        if len(rendered) > STREAM_LOG_MAX_CHARS:
            rendered = (
                "... earlier statistics logs omitted ...\n" + rendered[-STREAM_LOG_MAX_CHARS:]
            )
        return rendered

    if is_chinese:
        add(f"统计根目录：{root}")
        add(f"识别到 Session：{len(sessions)}")
    else:
        add(f"Statistics root: {root}")
        add(f"Detected sessions: {len(sessions)}")
    if not sessions:
        add("未发现 Session。" if is_chinese else "No sessions were discovered.")
        yield output(), ""
        return

    references = [
        reference for session in sessions for reference in discover_segment_references(session)
    ]
    missing = [reference for reference in references if not reference.report_path.is_file()]
    if is_chinese:
        add(f"参考视频 Segment：{len(references)}")
        add(f"缺少检查报告：{len(missing)}")
    else:
        add(f"Reference-video segments: {len(references)}")
        add(f"Missing inspection reports: {len(missing)}")
    report_mode = inspection_report_mode(fill_missing_reports, rebuild_all_reports)
    reports_to_generate = (
        references if report_mode == "all" else missing if report_mode == "missing" else []
    )
    if report_mode == "all":
        add(
            f"检查报告处理：全部重做，共 {len(reports_to_generate)} 个。"
            if is_chinese
            else f"Inspection reports: rebuild all {len(reports_to_generate)}."
        )
    yield output(), ""

    for index, reference in enumerate(reports_to_generate, start=1):
        label = f"{reference.session_dir.name}/{reference.segment}"
        if is_chinese:
            action = "重做检查" if report_mode == "all" else "补做检查"
        else:
            action = "Rebuild inspection" if report_mode == "all" else "Create inspection"
        prefix = f"[{index}/{len(reports_to_generate)}] {action}: {label}"
        command = stream_cli_command(
            [
                "inspect",
                str(reference.session_dir),
                "--segment",
                reference.segment,
                "--mocap-ratio",
                str(ratio),
            ]
        )
        result: StreamCommandResult | None = None
        try:
            while True:
                try:
                    current = next(command)
                except StopIteration as stop:
                    result = stop.value
                    break
                yield output(f"{prefix}\n\n{current}"), ""
        except OSError as exc:
            add(f"{prefix}: {exc}")
            continue

        if result is not None and result.returncode == 0 and reference.report_path.is_file():
            add(f"{prefix}: OK")
        else:
            returncode = result.returncode if result is not None else "unknown"
            add(f"{prefix}: failed (exit {returncode})")
        yield output(), ""

    ffprobe = resolve_ffprobe("ffprobe", "ffmpeg")
    session_statistics = []
    for index, session in enumerate(sessions, start=1):
        progress = (
            f"[{index}/{len(sessions)}] 统计时长：{session.name}"
            if is_chinese
            else f"[{index}/{len(sessions)}] Measure duration: {session.name}"
        )
        add(progress)
        statistic = summarize_session(root, session, ffprobe)
        session_statistics.append(statistic)
        partial = render_statistics_markdown(
            root,
            aggregate_by_primitive(session_statistics),
            language=language,
        )
        yield output(), partial

    primitives = aggregate_by_primitive(session_statistics)
    markdown = render_statistics_markdown(root, primitives, language=language)
    error_count = sum(len(item.errors) for item in session_statistics)
    if error_count:
        add(
            f"统计警告：{error_count} 项；对应时长可能为 0 或被计入未检查。"
            if is_chinese
            else f"Statistics warnings: {error_count}; affected durations may be zero or unchecked."
        )
        for statistic in session_statistics:
            for error in statistic.errors:
                add(f"- {statistic.session_dir.name}: {error}")
    add("统计完成。" if is_chinese else "Statistics complete.")
    yield output(), markdown


def infer_batch_modelscope_primitive(dataset_root: Path, session_dir: Path) -> str | None:
    from robocap_rerun_tools.dataset_statistics import (
        UNASSIGNED_PRIMITIVE,
        infer_action_primitive,
    )
    from robocap_rerun_tools.modelscope_publisher import validate_primitive_id

    detected = infer_action_primitive(dataset_root, session_dir)
    if detected != UNASSIGNED_PRIMITIVE:
        return detected

    try:
        relative_parts = session_dir.resolve().relative_to(dataset_root.resolve()).parts
    except (OSError, ValueError):
        return None

    action_parts: tuple[str, ...] = ()
    if dataset_root.name.casefold() == "egomotionactions":
        action_parts = relative_parts
    else:
        for index, part in enumerate(relative_parts):
            if part.casefold() == "egomotionactions":
                action_parts = relative_parts[index + 1 :]
                break
    if len(action_parts) < 2:
        return None
    if action_parts[0].casefold() == "demo" or re.fullmatch(r"\d{8}(?:_\d{6})?", action_parts[0]):
        if len(action_parts) < 3:
            return None
        candidate = action_parts[1]
    else:
        candidate = action_parts[0]
    try:
        return validate_primitive_id(candidate)
    except ValueError:
        return None


def validate_pending_modelscope_frame_counts(dataset_root: Path) -> tuple[str, ...]:
    from robocap_rerun_tools.dataset_statistics import (
        load_report_payload,
        report_has_frame_count_difference,
    )
    from robocap_rerun_tools.modelscope_publisher import REPORT_NAME, load_staged_dataset

    staged = load_staged_dataset(dataset_root)
    if not staged.pending_session_paths:
        raise ValueError(f"No pending ModelScope sessions were prepared under {dataset_root}.")

    failures: list[str] = []
    for relative_path in staged.pending_session_paths:
        report_path = dataset_root / Path(relative_path) / REPORT_NAME
        try:
            payload = load_report_payload(report_path)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            failures.append(f"{relative_path}: unreadable inspection report ({exc})")
            continue
        if report_has_frame_count_difference(payload):
            failures.append(f"{relative_path}: frame counts do not match n:ratio*(n+1):n+1")
    if failures:
        raise ValueError(
            "Pending ModelScope sessions failed the clean frame-count requirement:\n- "
            + "\n- ".join(failures)
        )
    return staged.pending_session_paths


def sequential_modelscope_dataset_root(
    dataset_root: Path,
    primitive_id: str,
    session_id: str,
) -> Path:
    from robocap_rerun_tools.modelscope_publisher import (
        validate_primitive_id,
        validate_session_id,
    )

    primitive = validate_primitive_id(primitive_id)
    resolved_session = validate_session_id(session_id)
    return dataset_root / "_modelscope_dataset" / "sequential" / primitive / resolved_session


def current_modelscope_upload_date() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d")


def resolve_modelscope_upload_date(value: object | None) -> str:
    from robocap_rerun_tools.modelscope_publisher import validate_upload_date

    requested = current_modelscope_upload_date() if value is None else str(value).strip()
    if not requested:
        raise ValueError("Upload date is required and must use YYYYMMDD.")
    return validate_upload_date(requested)


def bulk_upload_clean_modelscope_sessions(
    dataset_root: object,
    mocap_ratio: int,
    fill_missing_reports: bool,
    language: str = "中文",
    upload_date: object | None = None,
    skip_existing: bool = True,
    rebuild_all_reports: bool = False,
) -> Iterator[str]:
    from robocap_rerun_tools.cli import resolve_ffprobe
    from robocap_rerun_tools.dataset_statistics import (
        discover_segment_references,
        session_has_clean_frame_counts,
        summarize_session,
    )
    from robocap_rerun_tools.modelscope_publisher import (
        ModelScopePublisherError,
        fetch_remote_session_keys,
        validate_session_id,
    )

    root = dataset_root_path(dataset_root)
    selected_upload_date = resolve_modelscope_upload_date(upload_date)
    try:
        ratio = int(mocap_ratio)
    except (TypeError, ValueError) as exc:
        raise ValueError("Statistics Mocap ratio must be 4 or 8.") from exc
    if ratio not in {4, 8}:
        raise ValueError("Statistics Mocap ratio must be 4 or 8.")

    is_chinese = language == "中文"
    history: deque[str] = deque(maxlen=STREAM_LOG_MAX_LINES)

    def add(message: str) -> None:
        history.extend(line for line in message.splitlines() if line.strip())

    def render(current: str = "") -> str:
        value = "\n".join(history)
        if current:
            value = f"{value}\n\n{current}" if value else current
        if len(value) > STREAM_LOG_MAX_CHARS:
            value = "... earlier batch logs omitted ...\n" + value[-STREAM_LOG_MAX_CHARS:]
        return value

    def command_step(args: list[str], label: str) -> Generator[str, None, StreamCommandResult]:
        add(label)
        stream = stream_cli_command(args)
        while True:
            try:
                snapshot = next(stream)
            except StopIteration as stop:
                result = stop.value
                if not isinstance(result, StreamCommandResult):
                    raise TypeError(f"CLI stream returned no result for {args[0]}.")
                add(f"{label}: {'done' if result.returncode == 0 else 'failed'}")
                return result
            yield render(snapshot)

    sessions = discover_session_directories(root)
    add(f"统计根目录：{root}" if is_chinese else f"Statistics root: {root}")
    add(
        (
            f"上传日期：{selected_upload_date}；"
            f"跳过远端已有 Session：{'是' if skip_existing else '否（重新上传并覆盖元数据）'}"
        )
        if is_chinese
        else (
            f"Upload date: {selected_upload_date}; skip existing remote Sessions: "
            f"{'yes' if skip_existing else 'no (re-upload and replace metadata)'}"
        )
    )
    add(f"识别到 Session：{len(sessions)}" if is_chinese else f"Detected sessions: {len(sessions)}")
    if not sessions:
        add("未发现 Session。" if is_chinese else "No sessions were discovered.")
        yield render()
        return

    identified: list[tuple[Path, str, str]] = []
    excluded: list[str] = []
    for index, session in enumerate(sessions, start=1):
        add(
            f"[{index}/{len(sessions)}] 识别：{session.name}"
            if is_chinese
            else f"[{index}/{len(sessions)}] Identify: {session.name}"
        )
        primitive = infer_batch_modelscope_primitive(root, session)
        if primitive is None:
            excluded.append(f"{session}: action primitive is ambiguous or missing")
            yield render()
            continue
        try:
            session_id = validate_session_id(session.name)
        except ValueError as exc:
            excluded.append(f"{session}: invalid Session ID ({exc})")
            yield render()
            continue
        identified.append((session, primitive, session_id))
        yield render()

    if not identified:
        for reason in excluded:
            add(f"- {reason}")
        add(
            "没有可识别的 Session，未连接 ModelScope。"
            if is_chinese
            else "No identifiable Session; ModelScope was not contacted."
        )
        yield render()
        return

    auth_result = yield from command_step(
        ["modelscope-auth"],
        "检查 ModelScope 身份。" if is_chinese else "Check ModelScope authentication.",
    )
    if auth_result.returncode != 0:
        yield render()
        return

    add(
        "读取远端 metadata.jsonl，识别已上传 Session。"
        if is_chinese
        else "Read remote metadata.jsonl to identify uploaded Sessions."
    )
    yield render()
    try:
        remote_keys = fetch_remote_session_keys()
    except (OSError, ValueError, ModelScopePublisherError) as exc:
        add(
            f"读取远端 Session 索引失败，未开始上传：{exc}"
            if is_chinese
            else f"Remote Session lookup failed; upload did not start: {exc}"
        )
        yield render()
        return

    remote_existing = [item for item in identified if (item[1], item[2]) in remote_keys]
    if skip_existing:
        remote_skipped = remote_existing
        pending = [item for item in identified if (item[1], item[2]) not in remote_keys]
    else:
        remote_skipped = []
        pending = identified
    replacement_candidates = len(remote_existing) if not skip_existing else 0
    add(
        (
            f"远端索引：{len(remote_keys)}；替换候选：{replacement_candidates}；"
            f"已上传跳过：{len(remote_skipped)}；待处理：{len(pending)}"
        )
        if is_chinese
        else (
            f"Remote index: {len(remote_keys)}; replacement candidates: "
            f"{replacement_candidates}; already uploaded: "
            f"{len(remote_skipped)}; remaining: {len(pending)}"
        )
    )
    for session, primitive, session_id in remote_skipped:
        add(f"= {primitive}/{session_id}: {session}")
    yield render()
    if not pending:
        add(
            "所有可识别 Session 均已上传；未补检查、未暂存、未上传。"
            if is_chinese
            else (
                "All identifiable Sessions are already uploaded; no inspection, staging, "
                "or upload was run."
            )
        )
        yield render()
        return

    duplicate_keys: dict[tuple[str, str], list[Path]] = {}
    for session, primitive, session_id in pending:
        duplicate_keys.setdefault((primitive, session_id), []).append(session)
    duplicates = {key: paths for key, paths in duplicate_keys.items() if len(paths) > 1}
    if duplicates:
        add(
            "逐个上传已停止：未上传数据中动作名称和 Session ID 组合重复。"
            if is_chinese
            else (
                "Sequential upload stopped: duplicate action/session ID combinations "
                "among not-yet-uploaded data."
            )
        )
        for (primitive, session_id), paths in sorted(duplicates.items()):
            add(f"- {primitive}/{session_id}: {', '.join(str(path) for path in paths)}")
        yield render()
        return

    references = [
        reference for session, _, _ in pending for reference in discover_segment_references(session)
    ]
    missing = [reference for reference in references if not reference.report_path.is_file()]
    report_mode = inspection_report_mode(fill_missing_reports, rebuild_all_reports)
    reports_to_generate = (
        references if report_mode == "all" else missing if report_mode == "missing" else []
    )
    for index, reference in enumerate(reports_to_generate, start=1):
        if is_chinese:
            action = "重做检查" if report_mode == "all" else "补做检查"
        else:
            action = "Rebuild inspection" if report_mode == "all" else "Create inspection"
        separator = "：" if is_chinese else ": "
        label = (
            f"[{index}/{len(reports_to_generate)}] {action}{separator}"
            f"{reference.session_dir.name}/{reference.segment}"
        )
        result = yield from command_step(
            [
                "inspect",
                str(reference.session_dir),
                "--segment",
                reference.segment,
                "--mocap-ratio",
                str(ratio),
            ],
            label,
        )
        if result.returncode != 0:
            add(
                "检查失败；该 Session 将被排除。"
                if is_chinese
                else "Inspection failed; this Session will be excluded."
            )

    ffprobe = resolve_ffprobe("ffprobe", "ffmpeg")
    candidates: list[tuple[Path, str, str, list[Path], bool]] = []
    for index, (session, primitive, session_id) in enumerate(pending, start=1):
        add(
            f"[{index}/{len(pending)}] 筛选：{session.name}"
            if is_chinese
            else f"[{index}/{len(pending)}] Filter: {session.name}"
        )
        statistic = summarize_session(root, session, ffprobe)
        if not session_has_clean_frame_counts(statistic):
            excluded.append(f"{session}: unchecked or frame-count difference")
            yield render()
            continue
        try:
            mocap_files = default_modelscope_mocap_files(session)
        except (FileNotFoundError, OSError, ValueError, ModelScopePublisherError) as exc:
            excluded.append(f"{session}: {exc}")
            yield render()
            continue
        if not mocap_files:
            excluded.append(f"{session}: no default-selected BVH/CSV/TRC/MP4 Mocap file")
            yield render()
            continue
        key = (primitive, session_id)
        is_replacement = key in remote_keys
        candidates.append((session, primitive, session_id, mocap_files, is_replacement))
        yield render()

    replacement_count = sum(1 for item in candidates if item[4])
    new_count = len(candidates) - replacement_count
    add(
        (
            f"候选：{len(candidates)}（新增 {new_count}，替换 {replacement_count}）；"
            f"已上传跳过：{len(remote_skipped)}；"
            f"本地排除：{len(excluded)}"
        )
        if is_chinese
        else (
            f"Candidates: {len(candidates)} ({new_count} new, {replacement_count} replacement); "
            "already uploaded: "
            f"{len(remote_skipped)}; locally excluded: {len(excluded)}"
        )
    )
    for session, primitive, session_id, _, is_replacement in candidates:
        action = "replace" if is_replacement else "new"
        add(f"+ {primitive}/{session_id}: {session} [{action}, date={selected_upload_date}]")
    for reason in excluded:
        add(f"- {reason}")
    yield render()
    if not candidates:
        add(
            "没有新的无差帧 Session 可上传。"
            if is_chinese
            else "No new clean Session is available to upload."
        )
        yield render()
        return

    completed_new = 0
    completed_replacements = 0
    failed_items = 0
    for index, (
        session,
        primitive,
        session_id,
        mocap_files,
        is_replacement,
    ) in enumerate(candidates, start=1):
        staged_root = sequential_modelscope_dataset_root(root, primitive, session_id)
        args = [
            "modelscope-stage",
            str(session),
            "--primitive-id",
            primitive,
            "--dataset-root",
            str(staged_root),
        ]
        for mocap_file in mocap_files:
            args.extend(["--mocap-file", str(mocap_file)])
        label = (
            f"[{index}/{len(candidates)}] 准备：{primitive}/{session_id}\n暂存目录：{staged_root}"
            if is_chinese
            else (
                f"[{index}/{len(candidates)}] Prepare: {primitive}/{session_id}\n"
                f"Staging root: {staged_root}"
            )
        )
        stage_result = yield from command_step(args, label)
        if stage_result.returncode != 0:
            failed_items += 1
            add(
                f"[{index}/{len(candidates)}] 准备失败，跳过 {primitive}/{session_id}；"
                "暂存数据已保留，继续下一条。"
                if is_chinese
                else (
                    f"[{index}/{len(candidates)}] Preparation failed; skipped "
                    f"{primitive}/{session_id}. Staging data was kept; continuing."
                )
            )
            yield render()
            continue

        try:
            pending_paths = validate_pending_modelscope_frame_counts(staged_root)
            if len(pending_paths) != 1:
                raise ValueError(
                    "Isolated staging root must contain exactly one pending Session; "
                    f"found {len(pending_paths)}."
                )
        except (FileNotFoundError, OSError, ValueError, ModelScopePublisherError) as exc:
            failed_items += 1
            add(
                f"[{index}/{len(candidates)}] 上传前 clean 校验失败：{exc}"
                if is_chinese
                else f"[{index}/{len(candidates)}] Pre-upload clean validation failed: {exc}"
            )
            add(
                f"[{index}/{len(candidates)}] 跳过 {primitive}/{session_id}；"
                "暂存数据已保留，继续下一条。"
                if is_chinese
                else (
                    f"[{index}/{len(candidates)}] Skipped {primitive}/{session_id}. "
                    "Staging data was kept; continuing."
                )
            )
            yield render()
            continue
        add(
            f"[{index}/{len(candidates)}] 上传前校验通过：1 个 pending Session。"
            if is_chinese
            else f"[{index}/{len(candidates)}] Pre-upload validation passed: 1 pending Session."
        )

        upload_attempts = SEQUENTIAL_UPLOAD_RETRIES + 1
        upload_args = [
            "modelscope-upload",
            str(staged_root),
            "--upload-date",
            selected_upload_date,
        ]
        for attempt in range(1, upload_attempts + 1):
            upload_result = yield from command_step(
                upload_args,
                (
                    f"[{index}/{len(candidates)}] 上传尝试 {attempt}/{upload_attempts}："
                    f"{primitive}/{session_id}"
                    if is_chinese
                    else (
                        f"[{index}/{len(candidates)}] Upload attempt {attempt}/{upload_attempts}: "
                        f"{primitive}/{session_id}"
                    )
                ),
            )
            if upload_result.returncode == 0:
                break
            if attempt <= SEQUENTIAL_UPLOAD_RETRIES:
                add(
                    f"[{index}/{len(candidates)}] 上传失败；开始第 "
                    f"{attempt}/{SEQUENTIAL_UPLOAD_RETRIES} 次重试。"
                    if is_chinese
                    else (
                        f"[{index}/{len(candidates)}] Upload failed; starting retry "
                        f"{attempt}/{SEQUENTIAL_UPLOAD_RETRIES}."
                    )
                )
                yield render()
        if upload_result.returncode != 0:
            failed_items += 1
            add(
                f"[{index}/{len(candidates)}] 共尝试 {upload_attempts} 次仍失败，跳过 "
                f"{primitive}/{session_id}；暂存数据已保留，继续下一条。"
                if is_chinese
                else (
                    f"[{index}/{len(candidates)}] Upload failed after {upload_attempts} attempts; "
                    f"skipped {primitive}/{session_id}. Staging data was kept; continuing."
                )
            )
            yield render()
            continue
        if is_replacement:
            completed_replacements += 1
        else:
            completed_new += 1
        add(
            f"[{index}/{len(candidates)}] 上传完成：{primitive}/{session_id} "
            f"（{'替换' if is_replacement else '新增'}，日期 {selected_upload_date}）"
            if is_chinese
            else (
                f"[{index}/{len(candidates)}] Upload complete: {primitive}/{session_id} "
                f"({'replacement' if is_replacement else 'new'}, "
                f"date {selected_upload_date})"
            )
        )
        yield render()

    add(
        (
            f"逐个处理完成：新增 {completed_new}；替换 {completed_replacements}；"
            f"失败跳过 {failed_items}；"
            f"已上传跳过 {len(remote_skipped)}；本地排除 {len(excluded)}。"
        )
        if is_chinese
        else (
            f"Sequential processing complete: {completed_new} new; "
            f"{completed_replacements} replaced; {failed_items} failed and skipped; "
            f"{len(remote_skipped)} already uploaded; {len(excluded)} locally excluded."
        )
    )
    yield render()


def package_data(
    session_dir: str, segment: str, output_zip: str, proxy_height: int, proxy_crf: int
) -> Iterator[str]:
    args = ["package-data", session_path(session_dir)]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if optional_text(output_zip):
        args.extend(["--output", output_zip.strip()])
    args.extend(["--proxy-height", str(int(proxy_height)), "--proxy-crf", str(int(proxy_crf))])
    yield from stream_cli_command(args)


def format_modelscope_status(settings: object, language: str) -> str:
    configured = bool(getattr(settings, "token", None))
    source = str(getattr(settings, "token_source", "missing"))
    endpoint = str(getattr(settings, "endpoint", ""))
    repository = str(getattr(settings, "repo_id", None) or "not configured")
    env_path = str(getattr(settings, "env_path", ""))
    if language == "中文":
        state = "已配置" if configured else "未配置"
        repository_text = repository if repository != "not configured" else "未配置"
        return (
            f"**Token：{state}** · 来源：`{source}` · 站点：`{endpoint}` · "
            f"仓库：`{repository_text}` · `.env`：`{env_path}`"
        )
    state = "configured" if configured else "not configured"
    return (
        f"**Token: {state}** · source: `{source}` · endpoint: `{endpoint}` · "
        f"repository: `{repository}` · `.env`: `{env_path}`"
    )


def modelscope_status(language: str = "中文") -> str:
    from robocap_rerun_tools.modelscope_publisher import load_modelscope_settings

    try:
        return format_modelscope_status(load_modelscope_settings(), language)
    except (OSError, ValueError) as exc:
        return f"ModelScope configuration error: {exc}"


def save_modelscope_web_settings(
    token: str, endpoint: str, repo_id: str, language: str
) -> tuple[str, str, str]:
    from robocap_rerun_tools.modelscope_publisher import save_modelscope_settings

    try:
        settings = save_modelscope_settings(token, endpoint, repo_id=repo_id)
    except (OSError, ValueError) as exc:
        return f"ModelScope configuration error: {exc}", modelscope_status(language), ""
    if language == "中文":
        action = "ModelScope 配置已保存；空白 Token 或仓库值保持不变"
    else:
        action = "ModelScope settings saved; blank token or repository values remain unchanged"
    return f"{action}: {settings.env_path}", format_modelscope_status(settings, language), ""


def clear_modelscope_web_token(language: str) -> tuple[str, str, str]:
    from robocap_rerun_tools.modelscope_publisher import clear_modelscope_token

    try:
        settings = clear_modelscope_token()
    except (OSError, ValueError) as exc:
        return f"ModelScope configuration error: {exc}", modelscope_status(language), ""
    message = (
        "已清除本地 ModelScope Token。" if language == "中文" else "Saved ModelScope token cleared."
    )
    return message, format_modelscope_status(settings, language), ""


def check_modelscope_web_auth(token: str, endpoint: str, language: str) -> str:
    from robocap_rerun_tools.modelscope_publisher import (
        ModelScopePublisherError,
        ModelScopeSettings,
        load_modelscope_settings,
        validate_endpoint,
        verify_modelscope_auth,
    )

    try:
        saved = load_modelscope_settings()
        candidate = (token or "").strip() or saved.token
        source = "unsaved Web input" if (token or "").strip() else saved.token_source
        settings = ModelScopeSettings(
            candidate,
            validate_endpoint(endpoint),
            saved.env_path,
            source,
        )
        username = verify_modelscope_auth(settings)
    except (OSError, ValueError, ModelScopePublisherError) as exc:
        return str(exc)
    if language == "中文":
        return f"ModelScope 身份验证成功：`{username}`。Token 来源：`{source}`。"
    return f"ModelScope authentication succeeded as `{username}`. Token source: `{source}`."


def stage_modelscope_data(
    session_dir: str,
    segment: str,
    primitive_id: str,
    refresh_inspection: bool,
    selected_mocap_files: list[str] | None,
    selected_rrd_files: list[str] | None,
    aligned_intersection: bool,
    intersection_ratio: str,
    intersection_offset: float,
    inspection_mocap_ratio: int = 8,
) -> Iterator[str]:
    if not selected_mocap_files:
        yield (
            "No Mocap files selected. Scan Mocap files and keep at least one selection. / "
            "未选择 Mocap 文件，请先扫描并至少勾选一个文件。"
        )
        return
    args = [
        "modelscope-stage",
        session_path(session_dir),
        "--primitive-id",
        primitive_id,
        "--inspection-mocap-ratio",
        str(int(inspection_mocap_ratio)),
    ]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if refresh_inspection:
        args.append("--refresh-inspection")
    if aligned_intersection:
        args.extend(
            [
                "--aligned-intersection",
                "--ratio",
                optional_text(intersection_ratio) or "auto",
                "--offset",
                str(normalize_offset(intersection_offset)),
            ]
        )
    for mocap_file in selected_mocap_files:
        args.extend(["--mocap-file", str(mocap_file)])
    for rrd_file in selected_rrd_files or []:
        args.extend(["--rrd-file", str(rrd_file)])
    yield from stream_cli_command(args)


def upload_modelscope_data(
    session_dir: str,
    repo_id: str,
    revision: str,
    use_cache: bool,
    max_workers: int,
) -> Iterator[str]:
    from robocap_rerun_tools.modelscope_publisher import default_dataset_root

    resolved_session = Path(session_path(session_dir))
    resolved_root = default_dataset_root(resolved_session)
    args = [
        "modelscope-upload",
        str(resolved_root),
        "--revision",
        revision.strip() or "master",
        "--max-workers",
        str(max(1, int(max_workers))),
    ]
    if optional_text(repo_id):
        args.extend(["--repo-id", repo_id.strip()])
    if not use_cache:
        args.append("--no-cache")
    yield from stream_cli_command(args)


def inspect_offset(
    session_dir: str, segment: str, ratio: str, offset: int, nokov_source: str
) -> Iterator[str]:
    signed_offset = normalize_offset(offset)
    args = [
        "inspect-offset",
        session_path(session_dir),
        "--ratio",
        ratio.strip() or "auto",
        "--offset",
        str(signed_offset),
    ]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if optional_text(nokov_source):
        args.extend(["--nokov-source", nokov_source.strip()])
    yield from stream_cli_command(args)


def sweep_offset(
    session_dir: str, segment: str, ratio: str, offset_min: int, offset_max: int, nokov_source: str
) -> Iterator[str]:
    signed_offset_min = normalize_offset(offset_min)
    signed_offset_max = normalize_offset(offset_max)
    args = [
        "sweep-offset",
        session_path(session_dir),
        "--ratio",
        ratio.strip() or "auto",
        "--offset-min",
        str(signed_offset_min),
        "--offset-max",
        str(signed_offset_max),
    ]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if optional_text(nokov_source):
        args.extend(["--nokov-source", nokov_source.strip()])
    yield from stream_cli_command(args)


def export_rrd(
    session_dir: str,
    segment: str,
    mode: str,
    ratio: str,
    offset: int,
    limit_robocap_frames: bool,
    robocap_start_frame: object,
    robocap_end_frame: object,
    save_path: str,
    use_proxy: bool,
    interpolate_dropped_frames: bool,
    gt_dir: str,
    selected_gt_files: list[str] | None,
    include_third_person: bool,
    third_person_video: str,
    include_robowrist: bool,
    include_mag: bool,
    include_imu: bool,
    proxy_height: int,
) -> Iterator[str]:
    resolved_session_dir = Path(session_path(session_dir))
    robowrist_streams = (
        detected_robowrist_streams(resolved_session_dir, segment) if include_robowrist else ()
    )
    effective_include_robowrist = bool(include_robowrist and robowrist_streams)
    args = ["export", str(resolved_session_dir), "--mode", mode]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if mode == "frame":
        signed_offset = normalize_offset(offset)
        args.extend(["--ratio", ratio.strip() or "auto", "--offset", str(signed_offset)])
    if limit_robocap_frames:
        start_frame = normalize_optional_frame_index(robocap_start_frame)
        end_frame = normalize_optional_frame_index(robocap_end_frame)
        if start_frame is not None:
            args.extend(["--robocap-start-frame", str(start_frame)])
        if end_frame is not None:
            args.extend(["--robocap-end-frame", str(end_frame)])
    if optional_text(save_path):
        args.extend(["--save", save_path.strip()])
    if optional_text(gt_dir):
        args.extend(["--gt-dir", gt_dir.strip()])
    for gt_file in selected_gt_files or []:
        args.extend(["--gt-file", gt_file])
    if include_third_person and optional_text(third_person_video):
        args.extend(["--gt-third-person-video", third_person_video.strip()])
    if use_proxy:
        args.append("--use-proxy")
    if interpolate_dropped_frames:
        args.append("--interpolate-dropped-frames")
    args.extend(["--retarget-model", "none"])
    if not effective_include_robowrist:
        args.append("--no-robowrist")
    if not include_mag:
        args.append("--no-mag")
    if not include_imu:
        args.append("--no-imu")
    args.extend(["--proxy-height", str(int(proxy_height))])
    result = yield from stream_cli_command(args)
    if include_robowrist and not robowrist_streams:
        yield (
            "Robowrist: no matching video or sensor streams; automatically excluded.\n\n"
            + result.rendered
        )


def language_values(language: str) -> dict[str, str]:
    return LANGUAGE_PACKS.get(language, LANGUAGE_PACKS["English"])


def set_default_offset(
    offset: object, language: str, settings_path: Path | None = None
) -> tuple[str, int, int]:
    value, path = save_default_offset(offset, settings_path)
    message = language_values(language)["default_offset_saved"].format(value=value, path=path)
    return message, value, value


def language_updates(language: str):
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc

    labels = language_values(language)
    return [
        gr.update(value=labels["title"]),
        gr.update(label=labels["language"]),
        gr.update(label=labels["dataset_root"]),
        gr.update(value=labels["scan_sessions_button"]),
        gr.update(label=labels["session"]),
        gr.update(label=labels["segment"]),
        gr.update(label=labels["output"]),
        gr.update(label=labels["inspect_mocap_ratio"]),
        gr.update(value=labels["inspect_button"]),
        gr.update(value=labels["frame_compare_scan_button"]),
        gr.update(label=labels["frame_compare_videos"]),
        gr.update(label=labels["frame_compare_start_frame"]),
        gr.update(label=labels["frame_compare_end_frame"]),
        gr.update(label=labels["frame_compare_cell_width"]),
        gr.update(label=labels["frame_compare_cell_height"]),
        gr.update(value=labels["frame_compare_generate_button"]),
        gr.update(value=labels["frame_compare_open_button"]),
        gr.update(label=labels["frame_compare_file"]),
        gr.update(label=labels["statistics_mocap_ratio"]),
        gr.update(label=labels["statistics_fill_missing"]),
        gr.update(label=labels["statistics_rebuild_all"]),
        gr.update(value=labels["statistics_button"]),
        gr.update(value=labels["statistics_mocap_metadata_help"]),
        gr.update(value=labels["statistics_mocap_metadata_refresh"]),
        gr.update(value=labels["statistics_mocap_metadata_update"]),
        gr.update(label=labels["statistics_mocap_metadata"]),
        gr.update(value=labels["statistics_batch_help"]),
        gr.update(label=labels["statistics_upload_date"]),
        gr.update(label=labels["statistics_skip_existing"]),
        gr.update(value=labels["statistics_batch_upload"]),
        gr.update(label=labels["package_output"]),
        gr.update(label=labels["package_height"]),
        gr.update(label=labels["package_crf"]),
        gr.update(value=labels["package_button"]),
        gr.update(label=labels["mode"]),
        gr.update(label=labels["ratio"]),
        gr.update(label=labels["offset"]),
        gr.update(value=labels["offset_help"]),
        gr.update(label=labels["limit_robocap_frames"]),
        gr.update(label=labels["robocap_start_frame"]),
        gr.update(label=labels["robocap_end_frame"]),
        gr.update(label=labels["save_path"]),
        gr.update(label=labels["use_proxy"]),
        gr.update(label=labels["interpolate_dropped_frames"]),
        gr.update(value=labels["scan_button"]),
        gr.update(label=labels["gt_dir"]),
        gr.update(label=labels["gt_files"]),
        gr.update(label=labels["include_third_person"]),
        gr.update(label=labels["third_person_video"]),
        gr.update(label=labels["include_robowrist"]),
        gr.update(label=labels["include_mag"]),
        gr.update(label=labels["include_imu"]),
        gr.update(label=labels["export_height"]),
        gr.update(value=labels["export_button"]),
        gr.update(value=labels["set_default_offset_button"]),
        gr.update(label=labels["ratio"]),
        gr.update(label=labels["offset"]),
        gr.update(value=labels["offset_help"]),
        gr.update(label=labels["nokov_source"]),
        gr.update(value=labels["set_default_offset_button"]),
        gr.update(value=labels["offset_button"]),
        gr.update(label=labels["offset_min"]),
        gr.update(label=labels["offset_max"]),
        gr.update(value=labels["sweep_button"]),
        gr.update(value=labels["env_check_button"]),
        gr.update(value=labels["env_git_check_button"]),
        gr.update(value=labels["env_code_update_button"]),
        gr.update(value=labels["env_install_button"]),
        gr.update(label=labels["env_output"]),
        gr.update(value=labels["report_scan_button"]),
        gr.update(value=labels["report_open_button"]),
        gr.update(label=labels["report_html_file"]),
        gr.update(value=labels["viewer_scan_button"]),
        gr.update(value=labels["viewer_open_button"]),
        gr.update(label=labels["viewer_rrd_file"]),
        gr.update(label=labels["viewer_port"]),
        gr.update(label=labels["modelscope_primitive"]),
        gr.update(label=labels["modelscope_repo_id"]),
        gr.update(label=labels["modelscope_endpoint"]),
        gr.update(label=labels["modelscope_revision"]),
        gr.update(label=labels["modelscope_token"]),
        gr.update(value=modelscope_status(language)),
        gr.update(value=labels["modelscope_save_token"]),
        gr.update(value=labels["modelscope_clear_token"]),
        gr.update(value=labels["modelscope_check_token"]),
        gr.update(label=labels["modelscope_aligned_intersection"]),
        gr.update(label=labels["modelscope_intersection_ratio"]),
        gr.update(label=labels["modelscope_intersection_offset"]),
        gr.update(value=labels["modelscope_intersection_help"]),
        gr.update(label=labels["modelscope_refresh_inspection"]),
        gr.update(value=labels["modelscope_scan_mocap"]),
        gr.update(label=labels["modelscope_mocap_files"]),
        gr.update(value=labels["modelscope_scan_rrd"]),
        gr.update(label=labels["modelscope_rrd_files"]),
        gr.update(label=labels["modelscope_use_cache"]),
        gr.update(label=labels["modelscope_max_workers"]),
        gr.update(value=labels["modelscope_stage"]),
        gr.update(value=labels["modelscope_upload"]),
        gr.update(value=labels["doc"]),
    ]


def build_app():
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError("Web UI requires Gradio. Install it with: uv sync --extra web") from exc

    labels = language_values("中文")
    initial_upload_date = current_modelscope_upload_date()
    default_offset = load_default_offset()
    initial_dataset_root, initial_session_choices, initial_session = load_session_browser_settings()
    initial_modelscope_primitive = infer_modelscope_primitive(initial_session) or "P01"
    try:
        from robocap_rerun_tools.modelscope_publisher import load_modelscope_settings

        initial_modelscope_settings = load_modelscope_settings()
        initial_modelscope_endpoint = initial_modelscope_settings.endpoint
        initial_modelscope_repo_id = initial_modelscope_settings.repo_id or ""
    except (OSError, ValueError):
        initial_modelscope_endpoint = "https://modelscope.cn"
        initial_modelscope_repo_id = ""
    blocks_options: dict[str, object] = {"title": "Robocap Rerun Tools"}
    if "css" not in inspect.signature(gr.Blocks.launch).parameters:
        blocks_options["css"] = WEB_CSS
    with gr.Blocks(**blocks_options) as app:
        title = gr.Markdown(labels["title"])
        with gr.Row():
            language = gr.Radio(
                label=labels["language"], choices=["中文", "English"], value="中文", scale=1
            )
            dataset_root = gr.Textbox(
                label=labels["dataset_root"],
                value=initial_dataset_root,
                placeholder=r"Z:\DATASETS\Frodobots\nokov",
                scale=4,
            )
            scan_sessions_button = gr.Button(labels["scan_sessions_button"], scale=1)
        with gr.Row():
            session_dir = gr.Dropdown(
                label=labels["session"],
                choices=initial_session_choices,
                value=initial_session,
                allow_custom_value=True,
                filterable=True,
                scale=4,
            )
            segment = gr.Textbox(label=labels["segment"], value="segment1", scale=1)
        output = gr.Textbox(label=labels["output"], lines=16)

        with gr.Tab("检查 / Inspect"):
            with gr.Row():
                inspect_mocap_ratio = gr.Radio(
                    label=labels["inspect_mocap_ratio"],
                    choices=[8, 4],
                    value=8,
                    scale=2,
                )
                inspect_button = gr.Button(labels["inspect_button"], variant="primary", scale=1)
            inspect_event = inspect_button.click(
                inspect_session,
                inputs=[session_dir, segment, inspect_mocap_ratio],
                outputs=output,
            )

        with gr.Tab("检查报告 / Reports"):
            report_scan_button = gr.Button(labels["report_scan_button"])
            report_html_file = gr.Dropdown(
                label=labels["report_html_file"], choices=[], allow_custom_value=True
            )
            report_open_button = gr.Button(labels["report_open_button"], variant="primary")
            report_scan_button.click(
                scan_timestamp_reports,
                inputs=[session_dir],
                outputs=[output, report_html_file],
            )
            report_open_button.click(
                open_timestamp_report, inputs=[report_html_file], outputs=output
            )
            inspect_event.then(
                timestamp_report_choices, inputs=[session_dir], outputs=[report_html_file]
            )

        with gr.Tab("帧对比 / Frame Comparison"):
            frame_compare_scan_button = gr.Button(labels["frame_compare_scan_button"])
            frame_compare_videos = gr.CheckboxGroup(
                label=labels["frame_compare_videos"], choices=[], value=[]
            )
            with gr.Row():
                frame_compare_start_frame = gr.Number(
                    label=labels["frame_compare_start_frame"], value=0, precision=0
                )
                frame_compare_end_frame = gr.Number(
                    label=labels["frame_compare_end_frame"], value=59, precision=0
                )
                frame_compare_cell_width = gr.Number(
                    label=labels["frame_compare_cell_width"], value=960, precision=0
                )
                frame_compare_cell_height = gr.Number(
                    label=labels["frame_compare_cell_height"], value=540, precision=0
                )
            frame_compare_generate_button = gr.Button(
                labels["frame_compare_generate_button"], variant="primary"
            )
            frame_compare_output_path = gr.Textbox(value="", visible=False, container=False)
            frame_compare_open_button = gr.Button(
                labels["frame_compare_open_button"], interactive=False
            )
            frame_compare_file = gr.File(label=labels["frame_compare_file"])
            frame_compare_scan_button.click(
                scan_frame_comparison_videos,
                inputs=[session_dir, language],
                outputs=[output, frame_compare_videos],
            )
            frame_compare_generate_button.click(
                generate_frame_comparison,
                inputs=[
                    session_dir,
                    frame_compare_videos,
                    frame_compare_start_frame,
                    frame_compare_end_frame,
                    frame_compare_cell_width,
                    frame_compare_cell_height,
                    language,
                ],
                outputs=[
                    output,
                    frame_compare_file,
                    frame_compare_output_path,
                    frame_compare_open_button,
                ],
            )
            frame_compare_open_button.click(
                open_frame_comparison_image,
                inputs=[session_dir, frame_compare_output_path, language],
                outputs=output,
            )

        with gr.Tab("统计 / Statistics"):
            with gr.Row():
                statistics_mocap_ratio = gr.Radio(
                    label=labels["statistics_mocap_ratio"],
                    choices=[8, 4],
                    value=8,
                    scale=2,
                )
                statistics_fill_missing = gr.Checkbox(
                    label=labels["statistics_fill_missing"], value=True, scale=2
                )
                statistics_rebuild_all = gr.Checkbox(
                    label=labels["statistics_rebuild_all"], value=False, scale=2
                )
                statistics_button = gr.Button(
                    labels["statistics_button"], variant="primary", scale=1
                )
            statistics_result = gr.Markdown(elem_id="statistics-result")
            statistics_event = statistics_button.click(
                calculate_dataset_statistics,
                inputs=[
                    dataset_root,
                    statistics_mocap_ratio,
                    statistics_fill_missing,
                    language,
                    statistics_rebuild_all,
                ],
                outputs=[output, statistics_result],
            )
            statistics_mocap_metadata_help = gr.Markdown(
                labels["statistics_mocap_metadata_help"]
            )
            with gr.Row():
                statistics_mocap_metadata_refresh = gr.Button(
                    labels["statistics_mocap_metadata_refresh"]
                )
                statistics_mocap_metadata_update = gr.Button(
                    labels["statistics_mocap_metadata_update"],
                    variant="primary",
                )
            statistics_mocap_metadata = gr.Dataframe(
                headers=STATISTICS_MOCAP_METADATA_HEADERS,
                datatype=["bool", "str", "str", "str", "number", "str", "number", "str"],
                value=[],
                type="array",
                interactive=True,
                label=labels["statistics_mocap_metadata"],
            )
            statistics_event.then(
                statistics_mocap_metadata_rows,
                inputs=[dataset_root],
                outputs=[statistics_mocap_metadata],
            )
            statistics_mocap_metadata_refresh.click(
                scan_statistics_mocap_metadata,
                inputs=[dataset_root, language],
                outputs=[output, statistics_mocap_metadata],
            )
            statistics_batch_help = gr.Markdown(labels["statistics_batch_help"])
            with gr.Row():
                statistics_upload_date = gr.Textbox(
                    label=labels["statistics_upload_date"],
                    value=initial_upload_date,
                    placeholder="YYYYMMDD",
                    scale=2,
                )
                statistics_skip_existing = gr.Checkbox(
                    label=labels["statistics_skip_existing"],
                    value=True,
                    scale=2,
                )
                statistics_batch_upload = gr.Button(
                    labels["statistics_batch_upload"],
                    scale=1,
                )
            statistics_batch_upload.click(
                bulk_upload_clean_modelscope_sessions,
                inputs=[
                    dataset_root,
                    statistics_mocap_ratio,
                    statistics_fill_missing,
                    language,
                    statistics_upload_date,
                    statistics_skip_existing,
                    statistics_rebuild_all,
                ],
                outputs=output,
            )

        with gr.Tab("打包 / Package"):
            with gr.Row():
                package_output = gr.Textbox(
                    label=labels["package_output"], placeholder=r"D:\share\session48_segment1.zip"
                )
                package_height = gr.Number(label=labels["package_height"], value=540, precision=0)
                package_crf = gr.Number(label=labels["package_crf"], value=28, precision=0)
            package_button = gr.Button(labels["package_button"], variant="primary")
            package_button.click(
                package_data,
                inputs=[session_dir, segment, package_output, package_height, package_crf],
                outputs=output,
            )

        with gr.Tab("ModelScope"):
            with gr.Row():
                modelscope_primitive = gr.Dropdown(
                    label=labels["modelscope_primitive"],
                    choices=[f"P{index:02d}" for index in range(1, 30)],
                    value=initial_modelscope_primitive,
                    allow_custom_value=True,
                    scale=1,
                )
            with gr.Row():
                modelscope_repo_id = gr.Textbox(
                    label=labels["modelscope_repo_id"],
                    placeholder="owner/egomocap",
                    value=initial_modelscope_repo_id,
                    scale=3,
                )
                modelscope_revision = gr.Textbox(
                    label=labels["modelscope_revision"], value="master", scale=1
                )
            with gr.Row():
                modelscope_endpoint = gr.Textbox(
                    label=labels["modelscope_endpoint"],
                    value=initial_modelscope_endpoint,
                    scale=2,
                )
                modelscope_token = gr.Textbox(
                    label=labels["modelscope_token"], type="password", value="", scale=2
                )
            modelscope_token_status = gr.Markdown(modelscope_status("中文"))
            with gr.Row():
                modelscope_save_token = gr.Button(labels["modelscope_save_token"])
                modelscope_clear_token = gr.Button(labels["modelscope_clear_token"])
                modelscope_check_token = gr.Button(labels["modelscope_check_token"])
            modelscope_save_token.click(
                save_modelscope_web_settings,
                inputs=[modelscope_token, modelscope_endpoint, modelscope_repo_id, language],
                outputs=[output, modelscope_token_status, modelscope_token],
            )
            modelscope_clear_token.click(
                clear_modelscope_web_token,
                inputs=[language],
                outputs=[output, modelscope_token_status, modelscope_token],
            )
            modelscope_check_token.click(
                check_modelscope_web_auth,
                inputs=[modelscope_token, modelscope_endpoint, language],
                outputs=output,
            )
            with gr.Row():
                modelscope_aligned_intersection = gr.Checkbox(
                    label=labels["modelscope_aligned_intersection"], value=False
                )
                modelscope_intersection_ratio = gr.Textbox(
                    label=labels["modelscope_intersection_ratio"], value="auto"
                )
                modelscope_intersection_offset = gr.Number(
                    label=labels["modelscope_intersection_offset"],
                    value=default_offset,
                    precision=0,
                )
            modelscope_intersection_help = gr.Markdown(labels["modelscope_intersection_help"])
            with gr.Row():
                modelscope_refresh_inspection = gr.Checkbox(
                    label=labels["modelscope_refresh_inspection"], value=True
                )
                modelscope_use_cache = gr.Checkbox(label=labels["modelscope_use_cache"], value=True)
                modelscope_max_workers = gr.Number(
                    label=labels["modelscope_max_workers"], value=4, precision=0
                )
            with gr.Row():
                modelscope_scan_mocap = gr.Button(labels["modelscope_scan_mocap"])
                modelscope_scan_rrd = gr.Button(labels["modelscope_scan_rrd"])
            modelscope_mocap_files = gr.CheckboxGroup(
                label=labels["modelscope_mocap_files"], choices=[], value=[]
            )
            modelscope_rrd_files = gr.CheckboxGroup(
                label=labels["modelscope_rrd_files"], choices=[], value=[]
            )
            modelscope_scan_mocap.click(
                scan_modelscope_mocap_files,
                inputs=[session_dir],
                outputs=[output, modelscope_mocap_files],
            )
            modelscope_scan_rrd.click(
                scan_modelscope_rrd_files,
                inputs=[session_dir, segment],
                outputs=[output, modelscope_rrd_files],
            )
            with gr.Row():
                modelscope_stage = gr.Button(labels["modelscope_stage"], variant="primary")
                modelscope_upload = gr.Button(labels["modelscope_upload"])
            modelscope_stage.click(
                stage_modelscope_data,
                inputs=[
                    session_dir,
                    segment,
                    modelscope_primitive,
                    modelscope_refresh_inspection,
                    modelscope_mocap_files,
                    modelscope_rrd_files,
                    modelscope_aligned_intersection,
                    modelscope_intersection_ratio,
                    modelscope_intersection_offset,
                    inspect_mocap_ratio,
                ],
                outputs=output,
            )
            modelscope_upload.click(
                upload_modelscope_data,
                inputs=[
                    session_dir,
                    modelscope_repo_id,
                    modelscope_revision,
                    modelscope_use_cache,
                    modelscope_max_workers,
                ],
                outputs=output,
            )
            statistics_mocap_metadata_update.click(
                batch_update_remote_mocap_metadata,
                inputs=[
                    dataset_root,
                    statistics_mocap_metadata,
                    modelscope_repo_id,
                    modelscope_revision,
                    modelscope_use_cache,
                    modelscope_max_workers,
                    language,
                ],
                outputs=output,
            )

        with gr.Tab("导出 RRD / Export"):
            scan_button = gr.Button(labels["scan_button"])
            gt_dir = gr.Textbox(label=labels["gt_dir"], placeholder=r"Z:\...\mocap_take01")
            gt_files = gr.CheckboxGroup(label=labels["gt_files"], choices=[], value=[])
            with gr.Row():
                mode = gr.Radio(label=labels["mode"], choices=["time", "frame"], value="frame")
                ratio = gr.Textbox(label=labels["ratio"], value="auto")
                offset = gr.Number(label=labels["offset"], value=default_offset, precision=0)
            export_offset_help = gr.Markdown(labels["offset_help"])
            with gr.Row():
                limit_robocap_frames = gr.Checkbox(
                    label=labels["limit_robocap_frames"], value=False
                )
                robocap_start_frame = gr.Number(
                    label=labels["robocap_start_frame"], value=0, precision=0
                )
                robocap_end_frame = gr.Number(
                    label=labels["robocap_end_frame"], value=0, precision=0
                )
            export_default_offset_button = gr.Button(labels["set_default_offset_button"])
            save_path = gr.Textbox(
                label=labels["save_path"], placeholder=r"D:\share\session48_frame_offset5.rrd"
            )
            with gr.Row():
                use_proxy = gr.Checkbox(label=labels["use_proxy"], value=True)
                interpolate_dropped_frames = gr.Checkbox(
                    label=labels["interpolate_dropped_frames"], value=False
                )
                export_height = gr.Number(label=labels["export_height"], value=540, precision=0)
            with gr.Row():
                include_mag = gr.Checkbox(label=labels["include_mag"], value=True)
                include_imu = gr.Checkbox(label=labels["include_imu"], value=True)
                include_robowrist = gr.Checkbox(label=labels["include_robowrist"], value=True)
                include_third_person = gr.Checkbox(label=labels["include_third_person"], value=True)
            with gr.Row():
                third_person_video = gr.Textbox(
                    label=labels["third_person_video"],
                    placeholder=r"Z:\...\mocap_take01\demo-1.mp4",
                )
            scan_button.click(
                scan_files,
                inputs=[session_dir, segment, gt_dir, include_robowrist],
                outputs=[output, gt_files, gt_dir, third_person_video, include_robowrist],
            )
            export_button = gr.Button(labels["export_button"], variant="primary")
            export_button.click(
                export_rrd,
                inputs=[
                    session_dir,
                    segment,
                    mode,
                    ratio,
                    offset,
                    limit_robocap_frames,
                    robocap_start_frame,
                    robocap_end_frame,
                    save_path,
                    use_proxy,
                    interpolate_dropped_frames,
                    gt_dir,
                    gt_files,
                    include_third_person,
                    third_person_video,
                    include_robowrist,
                    include_mag,
                    include_imu,
                    export_height,
                ],
                outputs=output,
            )
            ratio.change(
                rrd_alignment_defaults,
                inputs=[ratio, offset],
                outputs=[modelscope_intersection_ratio, modelscope_intersection_offset],
            )
            offset.change(
                rrd_alignment_defaults,
                inputs=[ratio, offset],
                outputs=[modelscope_intersection_ratio, modelscope_intersection_offset],
            )

        with gr.Tab("Offset"):
            with gr.Row():
                offset_ratio = gr.Textbox(label=labels["ratio"], value="auto")
                single_offset = gr.Number(label=labels["offset"], value=default_offset, precision=0)
                nokov_source = gr.Textbox(
                    label=labels["nokov_source"], placeholder=r"Z:\...\mocap\test2-hand.bvh"
                )
            inspect_offset_help = gr.Markdown(labels["offset_help"])
            with gr.Row():
                offset_button = gr.Button(labels["offset_button"], variant="primary")
                inspect_default_offset_button = gr.Button(labels["set_default_offset_button"])
            offset_button.click(
                inspect_offset,
                inputs=[session_dir, segment, offset_ratio, single_offset, nokov_source],
                outputs=output,
            )
            export_default_offset_button.click(
                set_default_offset,
                inputs=[offset, language],
                outputs=[output, offset, single_offset],
            )
            inspect_default_offset_button.click(
                set_default_offset,
                inputs=[single_offset, language],
                outputs=[output, offset, single_offset],
            )
            with gr.Row():
                offset_min = gr.Number(label=labels["offset_min"], value=-10, precision=0)
                offset_max = gr.Number(label=labels["offset_max"], value=10, precision=0)
            sweep_button = gr.Button(labels["sweep_button"])
            sweep_button.click(
                sweep_offset,
                inputs=[session_dir, segment, offset_ratio, offset_min, offset_max, nokov_source],
                outputs=output,
            )

        with gr.Tab("查看 Rerun / Viewer"):
            viewer_scan_button = gr.Button(labels["viewer_scan_button"])
            with gr.Row():
                viewer_rrd_file = gr.Dropdown(
                    label=labels["viewer_rrd_file"], choices=[], allow_custom_value=True, scale=4
                )
                viewer_port = gr.Number(label=labels["viewer_port"], value=0, precision=0, scale=1)
            viewer_open_button = gr.Button(labels["viewer_open_button"], variant="primary")
            viewer_scan_button.click(
                scan_rrd_files, inputs=[session_dir], outputs=[output, viewer_rrd_file]
            )
            viewer_open_button.click(
                open_rerun_webviewer,
                inputs=[viewer_rrd_file, viewer_port],
                outputs=[output, viewer_port],
            )

        with gr.Tab("环境 / Environment"):
            with gr.Row():
                env_check_button = gr.Button(labels["env_check_button"], variant="primary")
                env_git_check_button = gr.Button(labels["env_git_check_button"])
            with gr.Row():
                env_code_update_button = gr.Button(labels["env_code_update_button"])
                env_install_button = gr.Button(labels["env_install_button"])
            env_output = gr.Textbox(label=labels["env_output"], lines=22)
            env_check_button.click(check_environment, outputs=env_output)
            env_git_check_button.click(check_code_updates, outputs=env_output)
            env_code_update_button.click(update_code_and_restart, outputs=env_output)
            env_install_button.click(install_or_update_dependencies, outputs=env_output)

        with gr.Tab("文档 / Docs"):
            docs = gr.Markdown(labels["doc"])

        session_dependent_outputs = [
            gt_dir,
            gt_files,
            third_person_video,
            report_html_file,
            viewer_rrd_file,
            modelscope_mocap_files,
            modelscope_rrd_files,
            nokov_source,
            include_robowrist,
            modelscope_primitive,
            frame_compare_videos,
            frame_compare_file,
            frame_compare_output_path,
            frame_compare_open_button,
        ]
        session_scan_event = scan_sessions_button.click(
            scan_dataset_sessions,
            inputs=[dataset_root, session_dir, language],
            outputs=[output, session_dir],
        )
        session_scan_event.then(
            select_session,
            inputs=[dataset_root, session_dir],
            outputs=session_dependent_outputs,
        )
        session_dir.input(
            select_session,
            inputs=[dataset_root, session_dir],
            outputs=session_dependent_outputs,
        )

        language.change(
            language_updates,
            inputs=[language],
            outputs=[
                title,
                language,
                dataset_root,
                scan_sessions_button,
                session_dir,
                segment,
                output,
                inspect_mocap_ratio,
                inspect_button,
                frame_compare_scan_button,
                frame_compare_videos,
                frame_compare_start_frame,
                frame_compare_end_frame,
                frame_compare_cell_width,
                frame_compare_cell_height,
                frame_compare_generate_button,
                frame_compare_open_button,
                frame_compare_file,
                statistics_mocap_ratio,
                statistics_fill_missing,
                statistics_rebuild_all,
                statistics_button,
                statistics_mocap_metadata_help,
                statistics_mocap_metadata_refresh,
                statistics_mocap_metadata_update,
                statistics_mocap_metadata,
                statistics_batch_help,
                statistics_upload_date,
                statistics_skip_existing,
                statistics_batch_upload,
                package_output,
                package_height,
                package_crf,
                package_button,
                mode,
                ratio,
                offset,
                export_offset_help,
                limit_robocap_frames,
                robocap_start_frame,
                robocap_end_frame,
                save_path,
                use_proxy,
                interpolate_dropped_frames,
                scan_button,
                gt_dir,
                gt_files,
                include_third_person,
                third_person_video,
                include_robowrist,
                include_mag,
                include_imu,
                export_height,
                export_button,
                export_default_offset_button,
                offset_ratio,
                single_offset,
                inspect_offset_help,
                nokov_source,
                inspect_default_offset_button,
                offset_button,
                offset_min,
                offset_max,
                sweep_button,
                env_check_button,
                env_git_check_button,
                env_code_update_button,
                env_install_button,
                env_output,
                report_scan_button,
                report_open_button,
                report_html_file,
                viewer_scan_button,
                viewer_open_button,
                viewer_rrd_file,
                viewer_port,
                modelscope_primitive,
                modelscope_repo_id,
                modelscope_endpoint,
                modelscope_revision,
                modelscope_token,
                modelscope_token_status,
                modelscope_save_token,
                modelscope_clear_token,
                modelscope_check_token,
                modelscope_aligned_intersection,
                modelscope_intersection_ratio,
                modelscope_intersection_offset,
                modelscope_intersection_help,
                modelscope_refresh_inspection,
                modelscope_scan_mocap,
                modelscope_mocap_files,
                modelscope_scan_rrd,
                modelscope_rrd_files,
                modelscope_use_cache,
                modelscope_max_workers,
                modelscope_stage,
                modelscope_upload,
                docs,
            ],
        )

    return app


def main(args: argparse.Namespace) -> int:
    from robocap_rerun_tools.modelscope_publisher import ensure_env_file

    ensure_localhost_no_proxy()
    ensure_env_file()
    app = build_app()
    launch_options: dict[str, object] = {
        "server_name": args.host,
        "server_port": resolve_web_server_port(args.port),
        "inbrowser": args.open,
    }
    if "css" in inspect.signature(app.launch).parameters:
        launch_options["css"] = WEB_CSS
    app.launch(**launch_options)
    return 0
