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
from pathlib import Path


EN_DOC = """# Robocap Rerun Tools

This is a local browser UI for Robocap/NOKOV inspection, data packaging, RRD export, and offset checks.

## Basic Workflow

1. Enter a session directory, for example `C:\\Users\\Administrator\\Desktop\\20260803_032401_session29`.
2. Enter the segment name, usually `segment1`.
3. Run `Inspect` first to check FPS, frame counts, and abnormal intervals.
4. Use `Package Data` to create a zip for sharing. Videos are compressed by default.
5. Use `Export RRD` to create time-aligned or frame-aligned Rerun files.
6. Use `Offset` when you need to inspect or sweep a video-to-NOKOV frame offset.

The export controls can independently include or exclude MAG, IMU, robowrist, and third-person video data.
Only GT formats that are present create tabs. BVH/TRC/CSV/XRS each have separate skeleton and mesh views,
while rigid bodies from one CSV/XRS source stay in one shared 3D world. NOKOV millimetres are converted to
metres, and the file's `BoneAxis` selects the matching up axis. MANO retargeting fits hand scale, palm
orientation, and articulated finger joints on every frame. Select `none` as the retarget model when no
mesh is wanted; absent skeleton, mesh, and third-person sources do not create placeholder views.
If Robocap MAG and IMU are both absent, the complete sensor row is omitted.

## Alignment

Frame alignment uses:

```text
video frame N -> NOKOV frame round((N + offset) * ratio)
```

The default is `ratio=auto`. It reads `frame_rate_report.md`, averages all valid GT FPS values and
all Robocap video FPS values separately, rounds both means to the nearest multiple of 10, then uses
`rounded GT FPS / rounded Robocap FPS`. Enter a number such as `8` to override it.
Offset is measured in Robocap video frames. At ratio 8, an offset of 5 is equivalent to 40 GT frames.

Use `Set as default` beside either Offset field to persist that frame offset and apply it to both
the Export and Offset tabs. The saved value is restored the next time the Web UI starts.
"""


ZH_DOC = """# Robocap Rerun Tools 中文说明

这是一个本地网页工具，用于 Robocap/NOKOV 数据检查、数据打包、RRD 导出和 offset 检查。

## 基本流程

1. 输入 session 目录，例如 `C:\\Users\\Administrator\\Desktop\\20260803_032401_session29`。
2. 输入 segment，通常是 `segment1`。
3. 先运行“检查”，查看视频 FPS、帧数和异常帧间隔。
4. 用“打包数据”生成 zip 给别人使用。默认会压缩视频。
5. 用“导出 RRD”生成时间对齐或帧对齐的 Rerun 文件。
6. 如果需要检查视频帧和 NOKOV 帧的偏移关系，用“Offset 检查”。

导出时可以分别勾选是否包含 MAG、IMU、robowrist 和第三人称视频数据。
只有实际存在的 GT 格式才会生成 tab。BVH/TRC/CSV/XRS 分别有骨骼和 mesh 视图；同一 CSV/XRS
中的多个刚体保留在同一个 3D 世界坐标系。NOKOV 毫米坐标会转换成米，并根据文件中的 `BoneAxis`
选择向上轴。MANO 重定向会逐帧拟合手部尺度、掌部朝向和各手指关节姿态。
不需要 mesh 时，在“重定向模型”中选择 `none`；不存在的骨骼、mesh 或第三人称源不会创建占位窗口。
Robocap MAG 和 IMU 都不存在时，整行传感器窗口也会直接省略。

## 对齐公式

帧对齐使用：

```text
video frame N -> NOKOV frame round((N + offset) * ratio)
```

默认使用 `ratio=auto`：读取 `frame_rate_report.md`，分别计算有效 GT FPS 和 Robocap 视频 FPS
的均值，各自取最近的 10 倍数，再用“GT 取整值 / Robocap 取整值”得到 ratio。输入 `8` 等数字可覆盖自动值。
Offset 的单位是 Robocap 视频帧；ratio 为 8 时，offset 5 等价于 GT 的 40 帧。

点击任一 Offset 输入框旁的“设为默认值”，会持久保存当前帧偏移量，并同步到“导出 RRD”和“Offset”页。
下次启动 Web UI 时会自动恢复该值。
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
        "offset": "Offset (Robocap frames)",
        "save_path": "Save path",
        "mano_model_dir": "MANO model dir",
        "use_proxy": "Use compressed proxy video",
        "display": "Display layout",
        "scan_button": "Scan files",
        "gt_dir": "GT/NOKOV export dir",
        "gt_files": "GT files to include",
        "retarget_model": "Retarget model",
        "third_person_video": "Third-person video",
        "include_third_person": "Include third-person video",
        "include_robowrist": "Include robowrist data",
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
        "env_update_check_button": "Check remote version",
        "env_install_button": "Install/update dependencies",
        "env_output": "Environment output",
        "viewer_scan_button": "Scan RRD files",
        "viewer_open_button": "Open web viewer",
        "viewer_rrd_file": "RRD file",
        "viewer_port": "Web viewer port (0 = auto)",
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
        "offset": "Offset（Robocap 帧）",
        "save_path": "RRD 保存路径",
        "mano_model_dir": "MANO 模型目录",
        "use_proxy": "使用压缩视频",
        "display": "展示版布局",
        "scan_button": "扫描文件",
        "gt_dir": "GT/NOKOV 导出目录",
        "gt_files": "参与导出的 GT 文件",
        "retarget_model": "重定向模型",
        "third_person_video": "第三人称视频",
        "include_third_person": "包含第三人称视频",
        "include_robowrist": "包含 robowrist 数据",
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
        "env_update_check_button": "检查远端版本",
        "env_install_button": "安装/更新依赖",
        "env_output": "环境输出",
        "viewer_scan_button": "扫描 RRD 文件",
        "viewer_open_button": "打开 Web Viewer",
        "viewer_rrd_file": "RRD 文件",
        "viewer_port": "Web Viewer 端口（0 = 自动）",
        "doc": ZH_DOC,
    },
}


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OFFSET = 5
OFFSET_UNIT = "robocap_video_frames"
LEGACY_OFFSET_RATIO = 8
WEB_SETTINGS_ENV = "ROBOCAP_RERUN_WEB_SETTINGS"


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
        raise ValueError("Offset must be an integer frame count.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Offset must be an integer frame count.") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError("Offset must be an integer frame count.")
    return int(number)


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


def run_cli(args: list[str]) -> str:
    command = [sys.executable, "-m", "robocap_rerun_tools.cli", *args]
    returncode, output = run_process(command)
    if returncode != 0:
        suffix = f"\nCommand failed with exit code {returncode}."
        return f"{output}{suffix}" if output else suffix.strip()
    return output or "Done."


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def first_line(command: list[str], cwd: Path | None = None) -> str:
    returncode, output = run_process(command, cwd=cwd)
    line = output.splitlines()[0] if output else ""
    return line if returncode == 0 else f"failed: {line or returncode}"


def git_output(args: list[str]) -> str:
    returncode, output = run_process(["git", *args], cwd=PROJECT_ROOT)
    if returncode != 0:
        return f"{output}\nCommand failed with exit code {returncode}.".strip()
    return output or "Done."


def git_status_short() -> tuple[int, str]:
    return run_process(["git", "status", "--short"], cwd=PROJECT_ROOT)


def check_environment() -> str:
    packages = ("robocap-rerun-tools", "numpy", "rerun-sdk", "scipy", "gradio")
    lines = [
        "# Environment check",
        "",
        f"- project_root: `{PROJECT_ROOT}`",
        f"- python: `{sys.executable}`",
        f"- python_version: `{platform.python_version()}`",
        f"- platform: `{platform.platform()}`",
        f"- virtual_env: `{os.environ.get('VIRTUAL_ENV', '') or 'not set'}`",
        f"- uv: `{shutil.which('uv') or 'not found'}`",
        f"- git: `{shutil.which('git') or 'not found'}`",
        f"- ffmpeg: `{shutil.which('ffmpeg') or 'not found'}`",
        f"- ffprobe: `{shutil.which('ffprobe') or 'not found'}`",
        "",
        "## Tool versions",
        "",
        f"- uv: `{first_line(['uv', '--version']) if shutil.which('uv') else 'not found'}`",
        f"- ffmpeg: `{first_line(['ffmpeg', '-version']) if shutil.which('ffmpeg') else 'not found'}`",
        f"- ffprobe: `{first_line(['ffprobe', '-version']) if shutil.which('ffprobe') else 'not found'}`",
        "",
        "## Python packages",
        "",
    ]
    lines.extend(f"- {name}: `{package_version(name)}`" for name in packages)
    status_returncode, status_output = git_status_short()
    status_text = status_output if status_returncode == 0 and status_output.strip() else "clean"
    if status_returncode != 0:
        status_text = f"{status_output}\nCommand failed with exit code {status_returncode}.".strip()
    lines.extend(
        [
            "",
            "## Git",
            "",
            f"- branch: `{git_output(['branch', '--show-current'])}`",
            f"- commit: `{git_output(['log', '-1', '--oneline'])}`",
            f"- remote: `{git_output(['remote', '-v'])}`",
            "",
            "```text",
            status_text,
            "```",
        ]
    )
    return "\n".join(lines)


def check_remote_version() -> str:
    fetch_result = git_output(["fetch", "--prune"])
    local = git_output(["rev-parse", "--short", "HEAD"])
    upstream = git_output(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    remote = git_output(["rev-parse", "--short", "@{u}"])
    ahead_behind = git_output(["rev-list", "--left-right", "--count", "HEAD...@{u}"])
    return "\n".join(
        [
            "# Remote version check",
            "",
            f"- local: `{local}`",
            f"- upstream: `{upstream}`",
            f"- remote: `{remote}`",
            f"- ahead_behind: `{ahead_behind}` (`local_ahead remote_ahead`)",
            "",
            "## Fetch",
            "",
            "```text",
            fetch_result,
            "```",
        ]
    )


def install_or_update_dependencies() -> str:
    return launch_update_window()


def launch_update_window() -> str:
    script = PROJECT_ROOT / "scripts" / "web_update_and_restart.bat"
    if not script.exists():
        return f"Update script not found: {script}"
    command = [
        "cmd.exe",
        "/c",
        "start",
        "Robocap Rerun Tools Update",
        str(script),
        str(os.getpid()),
    ]
    try:
        subprocess.Popen(command, cwd=PROJECT_ROOT)
    except OSError as exc:
        return f"Failed to launch update window: {exc}"
    return (
        "Opened a separate cmd window to update dependencies.\n\n"
        "That window will close this running web process, print update logs, and restart with start_web.bat."
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


def scan_files(session_dir: str, gt_dir_value: str) -> tuple[str, object, str, str]:
    path = Path(session_path(session_dir))
    gt_dir = (
        Path(gt_dir_value.strip().strip('"'))
        if optional_text(gt_dir_value)
        else default_gt_dir(path)
    )
    if gt_dir is None or not gt_dir.exists():
        return (
            "No single GT/NOKOV data directory found. Fill GT/NOKOV export dir manually.",
            [],
            "",
            "",
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
        ]
    )
    try:
        import gradio as gr
    except ImportError as exc:
        raise RuntimeError(
            'Web UI requires Gradio. Install it with: uv pip install -e ".[web]"'
        ) from exc
    return summary, gr.update(choices=choices, value=choices), str(gt_dir), third_person


def scan_rrd_files(session_dir: str) -> tuple[str, object]:
    path = Path(session_path(session_dir))
    rrd_files = sorted(
        file
        for file in path.rglob("*.rrd")
        if file.is_file() and ".venv" not in file.relative_to(path).parts
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
    args = ["inspect", session_path(session_dir)]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    return run_cli(args)


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


def inspect_offset(
    session_dir: str, segment: str, ratio: str, offset: int, nokov_source: str
) -> str:
    args = [
        "inspect-offset",
        session_path(session_dir),
        "--ratio",
        ratio.strip() or "auto",
        "--offset",
        str(int(offset)),
    ]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if optional_text(nokov_source):
        args.extend(["--nokov-source", nokov_source.strip()])
    return run_cli(args)


def sweep_offset(
    session_dir: str, segment: str, ratio: str, offset_min: int, offset_max: int, nokov_source: str
) -> str:
    args = [
        "sweep-offset",
        session_path(session_dir),
        "--ratio",
        ratio.strip() or "auto",
        "--offset-min",
        str(int(offset_min)),
        "--offset-max",
        str(int(offset_max)),
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
    save_path: str,
    use_proxy: bool,
    display: bool,
    gt_dir: str,
    selected_gt_files: list[str] | None,
    retarget_model: str,
    include_third_person: bool,
    third_person_video: str,
    include_robowrist: bool,
    include_mag: bool,
    include_imu: bool,
    mano_model_dir: str,
    proxy_height: int,
) -> str:
    args = ["export", session_path(session_dir), "--mode", mode]
    if optional_text(segment):
        args.extend(["--segment", segment.strip()])
    if mode == "frame":
        args.extend(["--ratio", ratio.strip() or "auto", "--offset", str(int(offset))])
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
    args.extend(["--retarget-model", retarget_model])
    if not include_robowrist:
        args.append("--no-robowrist")
    if not include_mag:
        args.append("--no-mag")
    if not include_imu:
        args.append("--no-imu")
    if optional_text(mano_model_dir):
        args.extend(["--mano-model-dir", mano_model_dir.strip()])
    args.extend(["--proxy-height", str(int(proxy_height))])
    return run_cli(args)


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
        gr.update(label=labels["save_path"]),
        gr.update(label=labels["mano_model_dir"]),
        gr.update(label=labels["use_proxy"]),
        gr.update(label=labels["display"]),
        gr.update(value=labels["scan_button"]),
        gr.update(label=labels["gt_dir"]),
        gr.update(label=labels["gt_files"]),
        gr.update(label=labels["retarget_model"]),
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
        gr.update(label=labels["nokov_source"]),
        gr.update(value=labels["set_default_offset_button"]),
        gr.update(value=labels["offset_button"]),
        gr.update(label=labels["offset_min"]),
        gr.update(label=labels["offset_max"]),
        gr.update(value=labels["sweep_button"]),
        gr.update(value=labels["env_check_button"]),
        gr.update(value=labels["env_update_check_button"]),
        gr.update(value=labels["env_install_button"]),
        gr.update(label=labels["env_output"]),
        gr.update(value=labels["viewer_scan_button"]),
        gr.update(value=labels["viewer_open_button"]),
        gr.update(label=labels["viewer_rrd_file"]),
        gr.update(label=labels["viewer_port"]),
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
            inspect_button.click(inspect_session, inputs=[session_dir, segment], outputs=output)

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

        with gr.Tab("导出 RRD / Export"):
            scan_button = gr.Button(labels["scan_button"])
            with gr.Row():
                gt_dir = gr.Textbox(label=labels["gt_dir"], placeholder=r"Z:\...\nokov")
                retarget_model = gr.Radio(
                    label=labels["retarget_model"],
                    choices=["mano", "none", "smpl", "smplh"],
                    value="mano",
                )
            gt_files = gr.CheckboxGroup(label=labels["gt_files"], choices=[], value=[])
            with gr.Row():
                mode = gr.Radio(label=labels["mode"], choices=["time", "frame"], value="frame")
                ratio = gr.Textbox(label=labels["ratio"], value="auto")
                offset = gr.Number(label=labels["offset"], value=default_offset, precision=0)
            export_default_offset_button = gr.Button(labels["set_default_offset_button"])
            with gr.Row():
                save_path = gr.Textbox(
                    label=labels["save_path"], placeholder=r"D:\share\session48_frame_offset5.rrd"
                )
                mano_model_dir = gr.Textbox(
                    label=labels["mano_model_dir"], value=r"Z:\MODELS\hand_models\mano"
                )
            with gr.Row():
                use_proxy = gr.Checkbox(label=labels["use_proxy"], value=True)
                display = gr.Checkbox(label=labels["display"], value=False)
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
                inputs=[session_dir, gt_dir],
                outputs=[output, gt_files, gt_dir, third_person_video],
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
                    save_path,
                    use_proxy,
                    display,
                    gt_dir,
                    gt_files,
                    retarget_model,
                    include_third_person,
                    third_person_video,
                    include_robowrist,
                    include_mag,
                    include_imu,
                    mano_model_dir,
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
                offset_min = gr.Number(label=labels["offset_min"], value=0, precision=0)
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
                env_update_check_button = gr.Button(labels["env_update_check_button"])
                env_install_button = gr.Button(labels["env_install_button"])
            env_output = gr.Textbox(label=labels["env_output"], lines=22)
            env_check_button.click(check_environment, outputs=env_output)
            env_update_check_button.click(check_remote_version, outputs=env_output)
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
                save_path,
                mano_model_dir,
                use_proxy,
                display,
                scan_button,
                gt_dir,
                gt_files,
                retarget_model,
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
                nokov_source,
                inspect_default_offset_button,
                offset_button,
                offset_min,
                offset_max,
                sweep_button,
                env_check_button,
                env_update_check_button,
                env_install_button,
                env_output,
                viewer_scan_button,
                viewer_open_button,
                viewer_rrd_file,
                viewer_port,
                docs,
            ],
        )

    return app


def main(args: argparse.Namespace) -> int:
    app = build_app()
    app.launch(server_name=args.host, server_port=args.port, inbrowser=args.open)
    return 0
