from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

EN_DOC = """# Robocap Rerun Tools

This is a local browser UI for Robocap/NOKOV inspection, data packaging, RRD export, and offset checks.

## Basic Workflow

1. Enter a session directory, for example `C:\\Users\\Administrator\\Desktop\\20260803_032401_session29`.
2. Enter the segment name, usually `segment1`.
3. Run `Inspect` first to check FPS, frame/sample counts, and abnormal intervals.
4. Use `Package Data` to create a zip for sharing. Videos are compressed by default.
5. Use `Export RRD` to create time-aligned or frame-aligned Rerun files.
6. Use `Offset` when you need to inspect or sweep a video-to-NOKOV frame offset.

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

The ModelScope tab stages one recording under `PXX/<session_id>/`. Compressed video is the default,
and the standalone timestamp inspection HTML is required and copied into the same session directory.
The generated dataset root also contains `metadata.jsonl` and a DatasetHub-compatible `README.md`.

Session contents are classified as core (six Robocap camera videos), motion capture (all available
NOKOV body/rigid-body exports), optional (third-person video, IMU/MAG, Robowrist, and selected RRD),
or generated (`manifest.json` and the inspection HTML). Calibration files are stored separately;
the session manifest and `metadata.jsonl` contain only the main and related device IDs.

`MODELSCOPE_API_TOKEN` and `MODELSCOPE_ENDPOINT` are stored in the repository-local `.env` file.
The token field never displays the saved value; leaving it blank preserves the current token.
Use `Prepare session` before `Upload prepared dataset`. Upload sends every session referenced by
`metadata.jsonl` and uses the official `modelscope-hub`
client and its resumable upload cache by default.
"""


ZH_DOC = """# Robocap Rerun Tools 中文说明

这是一个本地网页工具，用于 Robocap/NOKOV 数据检查、数据打包、RRD 导出和 offset 检查。

## 基本流程

1. 输入 session 目录，例如 `C:\\Users\\Administrator\\Desktop\\20260803_032401_session29`。
2. 输入 segment，通常是 `segment1`。
3. 先运行“检查”，查看 FPS、帧/样本数和异常间隔。
4. 用“打包数据”生成 zip 给别人使用。默认会压缩视频。
5. 用“导出 RRD”生成时间对齐或帧对齐的 Rerun 文件。
6. 如果需要检查视频帧和 NOKOV 帧的偏移关系，用“Offset 检查”。

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
“扫描文件”会检测标准 robowrist 目录和数据流；没有检测到时会自动取消并禁用 robowrist 选项，
不会继续显示一个实际无数据可导出的开启状态。
只有实际存在的 GT 格式才会生成视图。BVH/TRC/CSV/XRS 骨骼视图在左下角从左到右并列；同一 CSV/XRS
中的多个刚体保留在同一个 3D 世界坐标系。NOKOV 毫米坐标会转换成米，并根据文件中的 `BoneAxis`
选择向上轴。Web 导出固定记录骨骼和刚体，不执行模型重定向；不存在的骨骼或第三人称源不会创建占位窗口。
中间传感器区域整体是一个单列多行 Grid：内部第 1 行是完整的 Robocap sensors，后续按实际数据添加
左、右 wrist MAG/IMU 行，不存在的行直接省略。Robocap MAG、IMU 和所选 wrist 数据都不存在时，
整个中间区域直接省略。

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

“ModelScope”页把一套数据准备到 `PXX/<session_id>/`。默认压缩视频，并强制要求已有独立的时间戳
检查 HTML；检查报告会复制到同一个 session 目录。数据集根目录同时生成 `metadata.jsonl` 和符合
DatasetHub 读取格式的 `README.md`。

Session 内容分为：核心六路 Robocap 视频、全部可用的 NOKOV 骨骼/刚体导出、可选的第三人称视频、
IMU/MAG、Robowrist 与所选 RRD，以及自动生成的 `manifest.json` 和检查 HTML。标定文件单独存放，
Session 的 manifest 与 `metadata.jsonl` 只记录主设备和关联设备的 device ID。

`MODELSCOPE_API_TOKEN` 与 `MODELSCOPE_ENDPOINT` 保存在仓库根目录的 `.env`。网页不会回显已保存
token 的内容；token 输入框留空时保留原值。先执行“准备 Session”，再执行“上传已准备数据集”。
上传会包含 `metadata.jsonl` 引用的全部 session，并使用官方 `modelscope-hub`，默认开启可恢复上传缓存。
"""


LANGUAGE_PACKS = {
    "English": {
        "title": "# Robocap Rerun Tools",
        "language": "Language",
        "session": "Session directory",
        "segment": "Segment",
        "output": "Output",
        "inspect_button": "Inspect",
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
        "display": "Display layout",
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
        "modelscope_help": (
            "**`PXX/<session_id>/` contents**\n\n"
            "- Core: six Robocap camera videos.\n"
            "- Motion capture: every available NOKOV body and rigid-body export.\n"
            "- Optional: third-person video, IMU/MAG, Robowrist, and selected RRD files.\n"
            "- Generated: `manifest.json` and timestamp inspection HTML.\n"
            "- Calibration is external; only main/related device IDs are recorded.\n\n"
            "The staging root is the Session directory's sibling `_modelscope_dataset`."
        ),
        "modelscope_primitive": "Action primitive (PXX)",
        "modelscope_repo_id": "ModelScope dataset repo (owner/name)",
        "modelscope_endpoint": "ModelScope endpoint",
        "modelscope_revision": "Revision",
        "modelscope_token": "ModelScope token (blank keeps saved value)",
        "modelscope_token_status": "Token status",
        "modelscope_save_token": "Save token and endpoint",
        "modelscope_clear_token": "Clear saved token",
        "modelscope_check_token": "Check authentication",
        "modelscope_refresh_inspection": "Regenerate inspection HTML before preparing",
        "modelscope_raw_video": "Keep original video",
        "modelscope_include_rrd": "Include RRD files",
        "modelscope_proxy_height": "Compressed video height",
        "modelscope_proxy_crf": "Compressed video CRF",
        "modelscope_create_repo": "Create repository if missing",
        "modelscope_visibility": "New repository visibility",
        "modelscope_license": "New repository license",
        "modelscope_use_cache": "Use resumable upload cache",
        "modelscope_max_workers": "Upload workers",
        "modelscope_stage": "Prepare session",
        "modelscope_upload": "Upload prepared dataset",
        "doc": EN_DOC,
    },
    "中文": {
        "title": "# Robocap Rerun Tools 中文界面",
        "language": "语言",
        "session": "Session 目录",
        "segment": "Segment",
        "output": "输出",
        "inspect_button": "检查",
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
        "display": "展示版布局",
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
        "modelscope_help": (
            "**`PXX/<session_id>/` 文件结构**\n\n"
            "- 核心：六路 Robocap 相机视频。\n"
            "- 动捕：全部可用的 NOKOV 骨骼与刚体导出文件。\n"
            "- 可选：第三人称视频、IMU/MAG、Robowrist、勾选后的 RRD。\n"
            "- 自动生成：`manifest.json` 与时间戳检查 HTML。\n"
            "- 标定数据在外部目录；这里只记录主设备及关联设备的 device ID。\n\n"
            "数据集根目录自动使用 Session 同级的 `_modelscope_dataset`。"
        ),
        "modelscope_primitive": "动作基元（PXX）",
        "modelscope_repo_id": "ModelScope 数据集仓库（owner/name）",
        "modelscope_endpoint": "ModelScope 站点",
        "modelscope_revision": "分支 / Revision",
        "modelscope_token": "ModelScope Token（留空保留已保存值）",
        "modelscope_token_status": "Token 状态",
        "modelscope_save_token": "保存 Token 与站点",
        "modelscope_clear_token": "清除已保存 Token",
        "modelscope_check_token": "检查身份",
        "modelscope_refresh_inspection": "准备前重新生成检查 HTML",
        "modelscope_raw_video": "保留原始视频",
        "modelscope_include_rrd": "包含 RRD 文件",
        "modelscope_proxy_height": "压缩视频高度",
        "modelscope_proxy_crf": "压缩视频 CRF",
        "modelscope_create_repo": "仓库不存在时创建",
        "modelscope_visibility": "新仓库可见性",
        "modelscope_license": "新仓库 License",
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


def run_cli_result(args: list[str]) -> tuple[int, str]:
    command = [sys.executable, "-m", "robocap_rerun_tools.cli", *args]
    return run_process(command)


def format_cli_result(returncode: int, output: str) -> str:
    if returncode != 0:
        suffix = f"\nCommand failed with exit code {returncode}."
        return f"{output}{suffix}" if output else suffix.strip()
    return output or "Done."


def run_cli(args: list[str]) -> str:
    returncode, output = run_cli_result(args)
    return format_cli_result(returncode, output)


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
            lines.append("- update_status: `unavailable`")
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


def optional_text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


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
        raise RuntimeError(
            'Web UI requires Gradio. Install it with: uv pip install -e ".[web]"'
        ) from exc
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
        raise RuntimeError(
            'Web UI requires Gradio. Install it with: uv pip install -e ".[web]"'
        ) from exc
    return summary, gr.update(choices=choices, value=choices[0] if choices else None)


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
        raise RuntimeError(
            'Web UI requires Gradio. Install it with: uv pip install -e ".[web]"'
        ) from exc
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


def inspect_session(session_dir: str, segment: str) -> str:
    resolved_session = Path(session_path(session_dir))
    resolved_segment = optional_text(segment)
    args = ["inspect", str(resolved_session)]
    if resolved_segment:
        args.extend(["--segment", resolved_segment])
    returncode, command_output = run_cli_result(args)
    output = format_cli_result(returncode, command_output)
    if returncode != 0:
        return output

    report_path = timestamp_report_path(resolved_session, resolved_segment)
    if not report_path.is_file():
        return (
            f"{output}\n\nInspection command succeeded, but the HTML report was not found:\n"
            f"`{report_path}`"
        )
    return f"{output}\n\nTimestamp anomaly HTML: `{report_path}`"


def package_data(
    session_dir: str, segment: str, output_zip: str, proxy_height: int, proxy_crf: int
) -> str:
    args = ["package-data", session_path(session_dir)]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if optional_text(output_zip):
        args.extend(["--output", output_zip.strip()])
    args.extend(["--proxy-height", str(int(proxy_height)), "--proxy-crf", str(int(proxy_crf))])
    return run_cli(args)


def format_modelscope_status(settings: object, language: str) -> str:
    configured = bool(getattr(settings, "token", None))
    source = str(getattr(settings, "token_source", "missing"))
    endpoint = str(getattr(settings, "endpoint", ""))
    env_path = str(getattr(settings, "env_path", ""))
    if language == "中文":
        state = "已配置" if configured else "未配置"
        return f"**Token：{state}** · 来源：`{source}` · 站点：`{endpoint}` · `.env`：`{env_path}`"
    state = "configured" if configured else "not configured"
    return (
        f"**Token: {state}** · source: `{source}` · endpoint: `{endpoint}` · `.env`: `{env_path}`"
    )


def modelscope_status(language: str = "中文") -> str:
    from robocap_rerun_tools.modelscope_publisher import load_modelscope_settings

    try:
        return format_modelscope_status(load_modelscope_settings(), language)
    except (OSError, ValueError) as exc:
        return f"ModelScope configuration error: {exc}"


def save_modelscope_web_settings(token: str, endpoint: str, language: str) -> tuple[str, str, str]:
    from robocap_rerun_tools.modelscope_publisher import save_modelscope_settings

    supplied_token = bool((token or "").strip())
    try:
        settings = save_modelscope_settings(token, endpoint)
    except (OSError, ValueError) as exc:
        return f"ModelScope configuration error: {exc}", modelscope_status(language), ""
    if language == "中文":
        action = "Token 与站点已保存" if supplied_token else "站点已保存；Token 保持不变"
    else:
        action = "Token and endpoint saved" if supplied_token else "Endpoint saved; token unchanged"
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
    raw_video: bool,
    include_rrd: bool,
    proxy_height: int,
    proxy_crf: int,
) -> str:
    args = [
        "modelscope-stage",
        session_path(session_dir),
        "--primitive-id",
        primitive_id,
        "--proxy-height",
        str(int(proxy_height)),
        "--proxy-crf",
        str(int(proxy_crf)),
    ]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if refresh_inspection:
        args.append("--refresh-inspection")
    if raw_video:
        args.append("--raw-video")
    if include_rrd:
        args.append("--include-rrd")
    return run_cli(args)


def upload_modelscope_data(
    session_dir: str,
    repo_id: str,
    revision: str,
    create_if_missing: bool,
    visibility: str,
    license_name: str,
    use_cache: bool,
    max_workers: int,
) -> str:
    from robocap_rerun_tools.modelscope_publisher import default_dataset_root

    resolved_session = Path(session_path(session_dir))
    resolved_root = default_dataset_root(resolved_session)
    args = [
        "modelscope-upload",
        str(resolved_root),
        "--repo-id",
        repo_id.strip(),
        "--revision",
        revision.strip() or "master",
        "--visibility",
        visibility,
        "--max-workers",
        str(max(1, int(max_workers))),
    ]
    if create_if_missing:
        args.append("--create-if-missing")
    if optional_text(license_name):
        args.extend(["--license", license_name.strip()])
    if not use_cache:
        args.append("--no-cache")
    return run_cli(args)


def inspect_offset(
    session_dir: str, segment: str, ratio: str, offset: int, nokov_source: str
) -> str:
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
    return run_cli(args)


def sweep_offset(
    session_dir: str, segment: str, ratio: str, offset_min: int, offset_max: int, nokov_source: str
) -> str:
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
    return run_cli(args)


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
    display: bool,
    interpolate_dropped_frames: bool,
    gt_dir: str,
    selected_gt_files: list[str] | None,
    include_third_person: bool,
    third_person_video: str,
    include_robowrist: bool,
    include_mag: bool,
    include_imu: bool,
    proxy_height: int,
) -> str:
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
    if display:
        args.append("--display")
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
    result = run_cli(args)
    if include_robowrist and not robowrist_streams:
        return (
            "Robowrist: no matching video or sensor streams; automatically excluded.\n\n" + result
        )
    return result


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
        raise RuntimeError(
            'Web UI requires Gradio. Install it with: uv pip install -e ".[web]"'
        ) from exc

    labels = language_values(language)
    return [
        gr.update(value=labels["title"]),
        gr.update(label=labels["language"]),
        gr.update(label=labels["session"]),
        gr.update(label=labels["segment"]),
        gr.update(label=labels["output"]),
        gr.update(value=labels["inspect_button"]),
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
        gr.update(label=labels["display"]),
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
        gr.update(value=labels["modelscope_help"]),
        gr.update(label=labels["modelscope_primitive"]),
        gr.update(label=labels["modelscope_repo_id"]),
        gr.update(label=labels["modelscope_endpoint"]),
        gr.update(label=labels["modelscope_revision"]),
        gr.update(label=labels["modelscope_token"]),
        gr.update(value=modelscope_status(language)),
        gr.update(value=labels["modelscope_save_token"]),
        gr.update(value=labels["modelscope_clear_token"]),
        gr.update(value=labels["modelscope_check_token"]),
        gr.update(label=labels["modelscope_refresh_inspection"]),
        gr.update(label=labels["modelscope_raw_video"]),
        gr.update(label=labels["modelscope_include_rrd"]),
        gr.update(label=labels["modelscope_proxy_height"]),
        gr.update(label=labels["modelscope_proxy_crf"]),
        gr.update(label=labels["modelscope_create_repo"]),
        gr.update(label=labels["modelscope_visibility"]),
        gr.update(label=labels["modelscope_license"]),
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
        raise RuntimeError(
            'Web UI requires Gradio. Install it with: uv pip install -e ".[web]"'
        ) from exc

    labels = language_values("中文")
    default_offset = load_default_offset()
    try:
        from robocap_rerun_tools.modelscope_publisher import load_modelscope_settings

        initial_modelscope_endpoint = load_modelscope_settings().endpoint
    except (OSError, ValueError):
        initial_modelscope_endpoint = "https://modelscope.cn"
    with gr.Blocks(title="Robocap Rerun Tools") as app:
        title = gr.Markdown(labels["title"])
        with gr.Row():
            language = gr.Radio(
                label=labels["language"], choices=["中文", "English"], value="中文", scale=1
            )
            session_dir = gr.Textbox(
                label=labels["session"],
                placeholder=r"Z:\DATASETS\Frodobots\nokov\20260707_083023_session48",
                scale=3,
            )
            segment = gr.Textbox(label=labels["segment"], value="segment1", scale=1)
        output = gr.Textbox(label=labels["output"], lines=16)

        with gr.Tab("检查 / Inspect"):
            inspect_button = gr.Button(labels["inspect_button"], variant="primary")
            inspect_event = inspect_button.click(
                inspect_session, inputs=[session_dir, segment], outputs=output
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
            modelscope_help = gr.Markdown(labels["modelscope_help"])
            with gr.Row():
                modelscope_primitive = gr.Dropdown(
                    label=labels["modelscope_primitive"],
                    choices=[f"P{index:02d}" for index in range(1, 30)],
                    value="P01",
                    allow_custom_value=True,
                    scale=1,
                )
            with gr.Row():
                modelscope_repo_id = gr.Textbox(
                    label=labels["modelscope_repo_id"], placeholder="owner/egomocap", scale=3
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
                inputs=[modelscope_token, modelscope_endpoint, language],
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
                modelscope_refresh_inspection = gr.Checkbox(
                    label=labels["modelscope_refresh_inspection"], value=True
                )
                modelscope_raw_video = gr.Checkbox(
                    label=labels["modelscope_raw_video"], value=False
                )
                modelscope_include_rrd = gr.Checkbox(
                    label=labels["modelscope_include_rrd"], value=False
                )
            with gr.Row():
                modelscope_proxy_height = gr.Number(
                    label=labels["modelscope_proxy_height"], value=540, precision=0
                )
                modelscope_proxy_crf = gr.Number(
                    label=labels["modelscope_proxy_crf"], value=28, precision=0
                )
                modelscope_max_workers = gr.Number(
                    label=labels["modelscope_max_workers"], value=4, precision=0
                )
            with gr.Row():
                modelscope_create_repo = gr.Checkbox(
                    label=labels["modelscope_create_repo"], value=False
                )
                modelscope_visibility = gr.Dropdown(
                    label=labels["modelscope_visibility"],
                    choices=["private", "internal", "public"],
                    value="private",
                )
                modelscope_license = gr.Textbox(
                    label=labels["modelscope_license"], placeholder="cc-by-4.0"
                )
                modelscope_use_cache = gr.Checkbox(label=labels["modelscope_use_cache"], value=True)
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
                    modelscope_raw_video,
                    modelscope_include_rrd,
                    modelscope_proxy_height,
                    modelscope_proxy_crf,
                ],
                outputs=output,
            )
            modelscope_upload.click(
                upload_modelscope_data,
                inputs=[
                    session_dir,
                    modelscope_repo_id,
                    modelscope_revision,
                    modelscope_create_repo,
                    modelscope_visibility,
                    modelscope_license,
                    modelscope_use_cache,
                    modelscope_max_workers,
                ],
                outputs=output,
            )

        with gr.Tab("导出 RRD / Export"):
            scan_button = gr.Button(labels["scan_button"])
            gt_dir = gr.Textbox(label=labels["gt_dir"], placeholder=r"Z:\...\nokov")
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
                display = gr.Checkbox(label=labels["display"], value=False)
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
                    label=labels["third_person_video"], placeholder=r"Z:\...\nokov\demo-1.mp4"
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
                    display,
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

        with gr.Tab("Offset"):
            with gr.Row():
                offset_ratio = gr.Textbox(label=labels["ratio"], value="auto")
                single_offset = gr.Number(label=labels["offset"], value=default_offset, precision=0)
                nokov_source = gr.Textbox(
                    label=labels["nokov_source"], placeholder=r"Z:\...\test1\test2-hand.bvh"
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

        language.change(
            language_updates,
            inputs=[language],
            outputs=[
                title,
                language,
                session_dir,
                segment,
                output,
                inspect_button,
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
                display,
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
                modelscope_help,
                modelscope_primitive,
                modelscope_repo_id,
                modelscope_endpoint,
                modelscope_revision,
                modelscope_token,
                modelscope_token_status,
                modelscope_save_token,
                modelscope_clear_token,
                modelscope_check_token,
                modelscope_refresh_inspection,
                modelscope_raw_video,
                modelscope_include_rrd,
                modelscope_proxy_height,
                modelscope_proxy_crf,
                modelscope_create_repo,
                modelscope_visibility,
                modelscope_license,
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
    app.launch(server_name=args.host, server_port=args.port, inbrowser=args.open)
    return 0
