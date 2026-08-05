from __future__ import annotations

"""
将 robocap + robowrist 数据集导出为双手 Rerun 录制文件的独立脚本。

这个脚本可以单独分享使用。给它一个 session 目录，再按需指定 `segment1`、
`segment2` 等段名，它会自动发现视频和传感器文件，构建固定布局的
Rerun Blueprint，并输出一个 `.rrd` 文件和可复用的压缩代理视频。

一、数据目录约定
----------------
session 目录应当大致满足下面这种结构：

    <session-dir>/
      robocap_segment1_video_left.mp4
      robocap_segment1_imu_left.db
      robocap_segment1_mag_middle.db
      robowrist_<left-id>_left/
        robowrist_segment1_video_left_down.mp4
        robowrist_segment1_imu_left.db
        robowrist_segment1_mag_left.db
      robowrist_<right-id>_right/
        robowrist_segment1_video_right_down.mp4
        robowrist_segment1_imu_right.db
        robowrist_segment1_mag_right.db

同一个 session 下可以同时存在 `segment1`、`segment2` 等多段数据。
可以用 `--segment segment2` 显式指定要导出的段；不指定时，脚本会尝试
自动发现一个可用的 segment。

二、产物目录
------------
所有输出按 segment 分组，统一写到：

    <session-dir>/_artifacts/<segment>/

目录结构如下：

    inspection/
      <session-name>_<segment>_dual_hands.rrd
    proxy/
      *.mp4

其中：
- `inspection/` 保存最终的 `.rrd`
- `proxy/` 保存 `--use-proxy` 模式下生成或复用的压缩 MP4

三、常用命令
------------
1. 只检查布局和文件发现结果，不写任何产物：

       conda run -n rerun python rerun/robocap_rerun_dual_hands.py \
         --session-dir datasets/self/20260508_052814_session2 \
         --segment segment2 \
         --inspect --layout-only

2. 生成 `.rrd`，并使用压缩代理视频：

       conda run -n rerun python rerun/robocap_rerun_dual_hands.py \
         --session-dir datasets/self/20260508_052814_session2 \
         --segment segment2 \
         --use-proxy

3. 生成后直接打开本地 Rerun Viewer：

       conda run -n rerun python rerun/robocap_rerun_dual_hands.py \
         --session-dir datasets/self/20260508_052814_session2 \
         --segment segment2 \
         --use-proxy --spawn

4. 将输出写到自定义路径：

       conda run -n rerun python rerun/robocap_rerun_dual_hands.py \
         --session-dir datasets/self/20260508_052814_session2 \
         --segment segment2 \
         --use-proxy \
         --save datasets/self/20260508_052814_session2/custom_segment2.rrd

四、依赖处理
------------
脚本启动时会自动检查直接依赖的第三方库：
- `numpy`
- `rerun`（通过 `rerun-sdk` 安装）

如果缺失，会尝试使用当前 Python 解释器自动执行：

    python -m pip install numpy rerun-sdk

五、行为说明
------------
- 传感器时间戳严格按数据库原始值输出，不做隐藏修正。
- 缺失流不会导致整体失败，对应槽位会显示为 `no data` 文本视图。
- 视频时间轴使用 MP4 帧时间戳，加上 MP4 metadata 中 `comment` 字段作为
  采集起点。
- 已存在且可被 Rerun 读取的代理视频会被复用；损坏或未完成的代理会自动重建。
"""

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import pickle
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

if __package__:
    from .alignment import FrameAlignment, round_positive_ratio
else:
    from alignment import FrameAlignment, round_positive_ratio


def ensure_third_party_packages() -> None:
    """检测脚本直接依赖的第三方库，缺失时自动安装。"""
    required_packages = {
        "numpy": "numpy",
        "rerun": "rerun-sdk",
    }
    missing_packages = [
        pip_name
        for module_name, pip_name in required_packages.items()
        if importlib.util.find_spec(module_name) is None
    ]
    if not missing_packages:
        return

    print(f"[bootstrap] 缺失第三方库，开始安装: {', '.join(missing_packages)}")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", *missing_packages],
        check=True,
    )


# 先完成依赖自举，再导入第三方库。
ensure_third_party_packages()

import numpy as np
import rerun as rr
import rerun.blueprint as rrb


VIDEO_SLOT_ORDER = (
    "left",
    "left_eye",
    "left_front",
    "left_wrist_down",
    "right",
    "right_eye",
    "right_front",
    "right_wrist_down",
)

EXPORT_CONFIG_SCHEMA = "source-script-frame-timeline-v1"

SIGNAL_SLOT_ORDER = (
    "middle_mag",
    "left_robocap_acc",
    "left_robocap_gyro",
    "right_robocap_acc",
    "right_robocap_gyro",
    "left_wrist_mag",
    "left_wrist_acc",
    "left_wrist_gyro",
    "right_wrist_mag",
    "right_wrist_acc",
    "right_wrist_gyro",
)

DEFAULT_SKELETON_PARENTS = (
    -1,
    0,
    1,
    2,
    0,
    4,
    5,
    0,
    7,
    8,
    9,
    8,
    11,
    12,
    8,
    14,
    15,
    16,
    15,
    18,
    19,
    20,
    15,
    22,
    23,
)

GT_SKELETON_ENTITY = "gt/skeleton"
GT_MESH_ENTITY = "gt/mesh"
GT_TRACKS_ENTITY = "gt/tracks"
GT_THIRD_PERSON_VIDEO_ENTITY = "gt/third_person_video"
GT_NOTE_ENTITY = "notes/gt"
GT_FILE_SUFFIXES = frozenset({".bvh", ".trc", ".csv", ".xrs"})
GT_DIRECTORY_IGNORES = frozenset({"_artifacts", ".venv"})

VIDEO_PATTERNS = {
    "left": "robocap_{segment}_video_left.mp4",
    "left_eye": "robocap_{segment}_video_left_eye.mp4",
    "left_front": "robocap_{segment}_video_left_front.mp4",
    "right": "robocap_{segment}_video_right.mp4",
    "right_eye": "robocap_{segment}_video_right_eye.mp4",
    "right_front": "robocap_{segment}_video_right_front.mp4",
    "left_wrist_down": "robowrist_*_left/robowrist_{segment}_video_left_down.mp4",
    "right_wrist_down": "robowrist_*_right/robowrist_{segment}_video_right_down.mp4",
}

SIGNAL_SPECS = {
    "left_robocap_acc": ("robocap_{segment}_imu_left.db", "acc_data", ("x", "y", "z")),
    "left_robocap_gyro": ("robocap_{segment}_imu_left.db", "gyro_data", ("x", "y", "z")),
    "right_robocap_acc": ("robocap_{segment}_imu_right.db", "acc_data", ("x", "y", "z")),
    "right_robocap_gyro": ("robocap_{segment}_imu_right.db", "gyro_data", ("x", "y", "z")),
    "middle_mag": ("robocap_{segment}_mag_middle.db", "mag_data", ("mag_x", "mag_y", "mag_z")),
    "left_wrist_mag": (
        "robowrist_*_left/robowrist_{segment}_mag_left.db",
        "mag_data",
        ("mag_x", "mag_y", "mag_z"),
    ),
    "left_wrist_acc": (
        "robowrist_*_left/robowrist_{segment}_imu_left.db",
        "acc_data",
        ("x", "y", "z"),
    ),
    "left_wrist_gyro": (
        "robowrist_*_left/robowrist_{segment}_imu_left.db",
        "gyro_data",
        ("x", "y", "z"),
    ),
    "right_wrist_mag": (
        "robowrist_*_right/robowrist_{segment}_mag_right.db",
        "mag_data",
        ("mag_x", "mag_y", "mag_z"),
    ),
    "right_wrist_acc": (
        "robowrist_*_right/robowrist_{segment}_imu_right.db",
        "acc_data",
        ("x", "y", "z"),
    ),
    "right_wrist_gyro": (
        "robowrist_*_right/robowrist_{segment}_imu_right.db",
        "gyro_data",
        ("x", "y", "z"),
    ),
}

AXIS_COLORS = {
    "x": [230, 75, 66],
    "y": [58, 160, 92],
    "z": [63, 116, 215],
    "mag_x": [230, 75, 66],
    "mag_y": [58, 160, 92],
    "mag_z": [63, 116, 215],
}


@dataclass(frozen=True)
class VideoSpec:
    label: str
    entity: str
    relative_path: str


@dataclass(frozen=True)
class SignalSpec:
    label: str
    origin: str
    relative_path: str
    table: str
    columns: tuple[str, str, str]


@dataclass(frozen=True)
class NoteSpec:
    label: str
    origin: str
    text: str


@dataclass(frozen=True)
class SessionConfig:
    segment_name: str
    videos: dict[str, VideoSpec]
    signals: dict[str, SignalSpec]
    notes: dict[str, NoteSpec]


@dataclass(frozen=True)
class ArtifactPaths:
    root_dir: Path
    proxy_dir: Path
    inspection_dir: Path
    rrd_path: Path


@dataclass(frozen=True)
class TimeWindow:
    start_ns: int
    end_ns: int


def capture_timestamps_to_aligned_frames(
    timestamps_ns: np.ndarray,
    reference_timestamps_ns: np.ndarray,
    ratio: float,
) -> np.ndarray:
    timestamps_ns = np.asarray(timestamps_ns, dtype=np.int64)
    reference_timestamps_ns = np.asarray(reference_timestamps_ns, dtype=np.int64)
    if len(timestamps_ns) == 0:
        return np.asarray([], dtype=np.int64)
    if len(reference_timestamps_ns) == 0:
        raise ValueError("Frame timeline requires reference video timestamps.")
    if len(reference_timestamps_ns) == 1:
        return np.zeros(len(timestamps_ns), dtype=np.int64)
    if np.any(np.diff(reference_timestamps_ns) <= 0):
        raise ValueError("Reference video timestamps must be strictly increasing.")

    right = np.searchsorted(reference_timestamps_ns, timestamps_ns, side="right")
    left = np.clip(right - 1, 0, len(reference_timestamps_ns) - 2)
    next_index = left + 1
    span_ns = reference_timestamps_ns[next_index] - reference_timestamps_ns[left]
    fraction = (timestamps_ns - reference_timestamps_ns[left]) / span_ns
    video_frame_position = left.astype(np.float64) + fraction
    return np.rint(video_frame_position * ratio).astype(np.int64)


@dataclass(frozen=True)
class TimelineContext:
    alignment_mode: str
    reference_timestamps_ns: np.ndarray | None = None
    frame_alignment: FrameAlignment | None = None

    def __post_init__(self) -> None:
        if self.alignment_mode not in {"time", "frame"}:
            raise ValueError(f"Unsupported alignment mode: {self.alignment_mode}")
        if self.alignment_mode == "frame" and (
            self.reference_timestamps_ns is None or self.frame_alignment is None
        ):
            raise ValueError("Frame alignment requires reference timestamps and a frame ratio.")

    @property
    def primary_timeline(self) -> str:
        return "frame" if self.alignment_mode == "frame" else "capture_time"

    def frames_from_capture_time(self, timestamps_ns: np.ndarray) -> np.ndarray:
        if self.reference_timestamps_ns is None or self.frame_alignment is None:
            raise ValueError("Frame timeline is not configured.")
        return capture_timestamps_to_aligned_frames(
            timestamps_ns,
            self.reference_timestamps_ns,
            self.frame_alignment.ratio,
        )

    def gt_frame(self, source_gt_frame: int) -> int:
        if self.frame_alignment is None:
            raise ValueError("GT frame mapping is only available in frame alignment mode.")
        return source_gt_frame - self.frame_alignment.gt_frame_offset

    def indexes(
        self,
        capture_timestamps_ns: np.ndarray,
        frame_indices: np.ndarray | None = None,
    ) -> list[rr.TimeColumn]:
        capture_timestamps_ns = np.asarray(capture_timestamps_ns, dtype=np.int64)
        indexes = [
            rr.TimeColumn("capture_time", duration=capture_timestamps_ns.astype(np.float64) * 1e-9)
        ]
        if self.alignment_mode == "frame":
            if frame_indices is None:
                frame_indices = self.frames_from_capture_time(capture_timestamps_ns)
            frame_indices = np.asarray(frame_indices, dtype=np.int64)
            if len(frame_indices) != len(capture_timestamps_ns):
                raise ValueError("Frame indexes and capture timestamps must have equal lengths.")
            indexes.insert(0, rr.TimeColumn("frame", sequence=frame_indices))
        return indexes

    def set_time(self, timestamp_ns: int, frame_index: int | None = None) -> None:
        if hasattr(rr, "set_time"):
            rr.set_time("capture_time", duration=float(timestamp_ns) * 1e-9)
        else:
            rr.set_time_nanos("capture_time", int(timestamp_ns))
        if self.alignment_mode == "frame":
            if frame_index is None:
                frame_index = int(
                    self.frames_from_capture_time(np.asarray([timestamp_ns], dtype=np.int64))[0]
                )
            rr.set_time("frame", sequence=int(frame_index))


RobocapFrameRange = tuple[int, int]


@dataclass(frozen=True)
class ExportNameParameters:
    alignment_mode: str
    frame_ratio: float | None
    video_frame_offset: int
    reference_video: str
    frame_range: RobocapFrameRange | None
    retarget_model: str
    use_proxy: bool
    proxy_height: int
    proxy_crf: int
    proxy_bitrate: str
    ffmpeg: str
    blueprint_preset: str
    max_sensor_points: int
    trim_to_common_time: bool
    align_gt_to_robocap: bool
    gt_coordinate_scale: float
    bvh_coordinate_scale: float
    gt_time_offset_ns: int
    gt_max_frames: int | None
    include_robowrist: bool
    include_mag: bool
    include_imu: bool
    gt_dir: str | None
    gt_input_files: tuple[str, ...]
    gt_skeleton: str | None
    gt_mesh: str | None
    third_person_input: str | None
    mano_model_dir: str
    gt_sources: tuple[str, ...]
    third_person_video: bool


@dataclass(frozen=True)
class GTSkeletonTrack:
    timestamps_ns: np.ndarray
    joints: np.ndarray
    joint_names: tuple[str, ...]
    parents: tuple[int, ...]


@dataclass(frozen=True)
class GTMeshTrack:
    timestamps_ns: np.ndarray
    vertices: np.ndarray
    faces: np.ndarray


@dataclass(frozen=True)
class GTMarkerTrack:
    label: str
    entity: str
    source: str
    timestamps_ns: np.ndarray
    positions: np.ndarray
    marker_names: tuple[str, ...]
    connections: tuple[tuple[int, int], ...] = ()
    colors: tuple[int, int, int] = (255, 210, 80)
    radius: float = 0.018


@dataclass(frozen=True)
class GTManoMeshTrack:
    label: str
    entity: str
    source: str
    timestamps_ns: np.ndarray
    vertices: np.ndarray
    faces: np.ndarray
    color: tuple[float, float, float] = (0.78, 0.78, 0.82)


@dataclass(frozen=True)
class BvhJoint:
    name: str
    parent: int
    offset: np.ndarray
    channels: tuple[str, ...]


@dataclass(frozen=True)
class ManoTemplate:
    vertices: np.ndarray
    faces: np.ndarray
    joints: np.ndarray
    parents: np.ndarray
    weights: np.ndarray


@dataclass(frozen=True)
class GTConfig:
    skeleton: GTSkeletonTrack | None
    mesh: GTMeshTrack | None
    marker_tracks: tuple[GTMarkerTrack, ...]
    mano_mesh_tracks: tuple[GTManoMeshTrack, ...]
    third_person_video: Path | None
    third_person_start_ns: int | None = None
    time_offset_ns: int = 0
    note: str | None = None
    view_up_axis: str | None = None


@dataclass(frozen=True)
class GTFileSet:
    label: str
    side: str | None
    bvh: Path | None = None
    trc: Path | None = None
    csv: Path | None = None
    xrs: Path | None = None
    colors: tuple[int, int, int] = (255, 210, 80)
    radius: float = 0.014
    connect_hands: bool = False


def find_first_relative_path(session_dir: Path, pattern: str) -> str | None:
    matches = sorted(session_dir.glob(pattern))
    if not matches:
        return None
    return str(matches[0].relative_to(session_dir)).replace("\\", "/")


def run_json(cmd: list[str]) -> dict:
    completed = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return json.loads(completed.stdout)


def probe_video(path: Path) -> dict:
    return run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(path),
        ]
    )


def metadata_comment_us(path: Path) -> int:
    comment = probe_video(path).get("format", {}).get("tags", {}).get("comment")
    if comment is None:
        raise ValueError(f"missing MP4 metadata comment: {path}")
    return int(comment)


def ffmpeg_has_encoder(ffmpeg: str, encoder: str) -> bool:
    try:
        encoders = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], check=True, text=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    return encoder in encoders


def choose_ffmpeg(requested: str | None) -> str:
    if requested:
        return requested

    candidates: list[str] = []
    seen: set[str] = set()
    for path_entry in os.environ.get("PATH", "").split(os.pathsep):
        if not path_entry:
            continue
        for name in ("ffmpeg.exe", "ffmpeg"):
            candidate = Path(path_entry) / name
            if candidate.exists():
                resolved = str(candidate.resolve())
                if resolved not in seen:
                    seen.add(resolved)
                    candidates.append(resolved)

    fallback = shutil.which("ffmpeg")
    if fallback:
        resolved = str(Path(fallback).resolve())
        if resolved not in seen:
            candidates.append(resolved)

    for candidate in candidates:
        if ffmpeg_has_encoder(candidate, "libx264"):
            return candidate
    for candidate in candidates:
        if ffmpeg_has_encoder(candidate, "libopenh264"):
            return candidate
    return shutil.which("ffmpeg") or "ffmpeg"


def ffmpeg_encoder_args(ffmpeg: str, crf: int, bitrate: str) -> list[str]:
    if ffmpeg_has_encoder(ffmpeg, "libx264"):
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf)]
    if ffmpeg_has_encoder(ffmpeg, "libopenh264"):
        return ["-c:v", "libopenh264", "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", "2800k"]
    raise RuntimeError(f"{ffmpeg} has no supported H.264 encoder")


def video_file_is_readable(path: Path) -> bool:
    """Return whether Rerun can read at least one frame timestamp from an MP4."""
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        timestamps_ns = rr.AssetVideo(path=path).read_frame_timestamps_nanos()
    except Exception:
        return False
    return len(timestamps_ns) > 0


def make_proxy_video(
    source: Path, proxy_dir: Path, height: int, crf: int, bitrate: str, ffmpeg: str
) -> Path:
    proxy_dir.mkdir(parents=True, exist_ok=True)
    target = proxy_dir / f"{source.stem}_h{height}_crf{crf}.mp4"
    if video_file_is_readable(target):
        return target

    # Keep incomplete ffmpeg output away from the reusable target. This also
    # repairs a previous non-empty proxy that lacks the MP4 moov atom.
    temporary = target.with_name(f"{target.stem}.tmp{target.suffix}")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(source),
            "-map_metadata",
            "0",
            "-vf",
            f"scale=-2:{height},fps=30",
            *ffmpeg_encoder_args(ffmpeg, crf, bitrate),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-an",
            str(temporary),
        ],
        check=True,
    )
    if not video_file_is_readable(temporary):
        raise RuntimeError(f"ffmpeg produced an unreadable proxy video: {temporary}")
    temporary.replace(target)
    return target


def resolve_video_path(
    session_dir: Path,
    spec: VideoSpec,
    artifact_paths: ArtifactPaths,
    use_proxy: bool,
    proxy_height: int,
    proxy_crf: int,
    proxy_bitrate: str,
    ffmpeg: str,
) -> Path:
    source = session_dir / spec.relative_path
    if not source.exists():
        raise FileNotFoundError(source)
    if not use_proxy:
        return source
    return make_proxy_video(
        source, artifact_paths.proxy_dir, proxy_height, proxy_crf, proxy_bitrate, ffmpeg
    )


def fetch_signal_rows(
    db_path: Path,
    table: str,
    columns: Iterable[str],
    max_points: int,
    capture_window: TimeWindow | None = None,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    cols = tuple(columns)
    select_cols = ", ".join(("timestamp", *cols))
    with sqlite3.connect(db_path) as con:
        rows = con.execute(f"select {select_cols} from {table} order by timestamp").fetchall()

    if not rows:
        raise ValueError(f"no rows in {db_path}:{table}")

    data = np.asarray(rows, dtype=np.float64)
    timestamps_ns = data[:, 0].astype(np.int64)
    mask = time_mask(timestamps_ns, capture_window)
    data = data[mask]
    if len(data) == 0:
        return np.asarray([], dtype=np.int64), {
            column: np.asarray([], dtype=np.float64) for column in cols
        }

    if len(data) > max_points:
        indexes = np.linspace(0, len(data) - 1, max_points).astype(np.int64)
        data = data[indexes]

    timestamps_ns = data[:, 0].astype(np.int64)
    values = {column: data[:, index + 1] for index, column in enumerate(cols)}
    return timestamps_ns, values


def extract_segment_name(relative_path: str) -> str | None:
    match = re.search(r"(segment\d+)", relative_path)
    return match.group(1) if match else None


def detect_segment_name(videos: dict[str, VideoSpec], signals: dict[str, SignalSpec]) -> str:
    candidates: set[str] = set()
    for spec in list(videos.values()) + list(signals.values()):
        segment_name = extract_segment_name(spec.relative_path)
        if segment_name is not None:
            candidates.add(segment_name)

    if not candidates:
        raise ValueError("unable to detect segment name from discovered files")
    if len(candidates) != 1:
        raise ValueError(f"multiple segment names detected: {sorted(candidates)}")
    return next(iter(candidates))


def discover_session(
    session_dir: Path,
    segment_name: str | None = None,
    include_robowrist: bool = True,
    include_mag: bool = True,
    include_imu: bool = True,
) -> SessionConfig:
    videos: dict[str, VideoSpec] = {}
    signals: dict[str, SignalSpec] = {}
    notes: dict[str, NoteSpec] = {}

    for label, pattern in VIDEO_PATTERNS.items():
        if not include_robowrist and "wrist" in label:
            continue
        pattern = pattern.format(segment=segment_name or "*")
        relative_path = find_first_relative_path(session_dir, pattern)
        if relative_path is None:
            notes[label] = NoteSpec(label=label, origin=f"notes/video/{label}", text="no data")
        else:
            videos[label] = VideoSpec(
                label=label, entity=f"video/{label}", relative_path=relative_path
            )

    for label, (pattern, table, columns) in SIGNAL_SPECS.items():
        if not include_robowrist and "wrist" in label:
            continue
        if not include_mag and label.endswith("_mag"):
            continue
        if not include_imu and label.endswith(("_acc", "_gyro")):
            continue
        pattern = pattern.format(segment=segment_name or "*")
        relative_path = find_first_relative_path(session_dir, pattern)
        if relative_path is None:
            notes[label] = NoteSpec(label=label, origin=f"notes/signal/{label}", text="no data")
        else:
            signals[label] = SignalSpec(
                label=label,
                origin=f"signals/{label}",
                relative_path=relative_path,
                table=table,
                columns=columns,
            )

    return SessionConfig(
        segment_name=segment_name or detect_segment_name(videos, signals),
        videos=videos,
        signals=signals,
        notes=notes,
    )


def build_artifact_paths(session_dir: Path, config: SessionConfig) -> ArtifactPaths:
    root_dir = session_dir / "_artifacts" / config.segment_name
    proxy_dir = root_dir / "proxy"
    inspection_dir = root_dir / "inspection"
    rrd_path = inspection_dir / f"{session_dir.name}_{config.segment_name}_dual_hands_with_GT.rrd"
    return ArtifactPaths(
        root_dir=root_dir,
        proxy_dir=proxy_dir,
        inspection_dir=inspection_dir,
        rrd_path=rrd_path,
    )


def default_rrd_path(
    artifact_paths: ArtifactPaths, session_dir: Path, config: SessionConfig, alignment_mode: str
) -> Path:
    return (
        artifact_paths.inspection_dir
        / f"{session_dir.name}_{config.segment_name}_dual_hands_with_GT_{alignment_mode}_aligned.rrd"
    )


def normalize_robocap_frame_range(
    start_frame: int | None, end_frame: int | None
) -> RobocapFrameRange | None:
    if start_frame is None and end_frame is None:
        return None
    if start_frame is None or end_frame is None:
        raise ValueError("--robocap-start-frame and --robocap-end-frame must be provided together.")
    if start_frame < 0 or end_frame < 0:
        raise ValueError("Robocap frame indexes must be non-negative (0-based).")
    if start_frame > end_frame:
        raise ValueError("Robocap start frame must be less than or equal to end frame.")
    return start_frame, end_frame


def robocap_frame_capture_window(
    reference_timestamps_ns: np.ndarray | None,
    frame_range: RobocapFrameRange | None,
) -> TimeWindow | None:
    if frame_range is None:
        return None
    if reference_timestamps_ns is None or len(reference_timestamps_ns) == 0:
        raise ValueError("Cannot select Robocap frames because no reference video was found.")

    start_frame, end_frame = frame_range
    frame_count = len(reference_timestamps_ns)
    if end_frame >= frame_count:
        raise ValueError(
            f"Robocap frame range {start_frame}..{end_frame} exceeds reference video bounds "
            f"0..{frame_count - 1}."
        )
    return TimeWindow(
        start_ns=int(reference_timestamps_ns[start_frame]),
        end_ns=int(reference_timestamps_ns[end_frame]),
    )


def intersect_time_windows(
    first: TimeWindow | None, second: TimeWindow | None
) -> TimeWindow | None:
    if first is None:
        return second
    if second is None:
        return first
    start_ns = max(first.start_ns, second.start_ns)
    end_ns = min(first.end_ns, second.end_ns)
    if end_ns < start_ns:
        return None
    return TimeWindow(start_ns=start_ns, end_ns=end_ns)


def compact_filename_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9.-]+", "-", value).strip("-.").lower()
    return token or "unknown"


def compact_number_token(value: float | None) -> str:
    if value is None:
        return "auto"
    return format(float(value), ".9g").replace("-", "m").replace(".", "p")


def with_export_parameter_suffix(path: Path, parameters: ExportNameParameters) -> Path:
    if parameters.alignment_mode == "frame":
        alignment_tokens = [
            f"r{compact_number_token(parameters.frame_ratio)}",
            f"o{parameters.video_frame_offset}",
        ]
    else:
        alignment_tokens = []

    reference_is_used = parameters.alignment_mode == "frame" or parameters.frame_range is not None
    if reference_is_used:
        alignment_tokens.append(f"ref-{compact_filename_token(parameters.reference_video)}")

    if parameters.frame_range is None:
        frame_token = "fall"
    else:
        frame_token = f"f{parameters.frame_range[0]}-{parameters.frame_range[1]}"
    media_token = f"p{parameters.proxy_height}" if parameters.use_proxy else "raw"
    stream_token = (
        f"data-rw{int(parameters.include_robowrist)}-mag{int(parameters.include_mag)}-"
        f"imu{int(parameters.include_imu)}-tp{int(parameters.third_person_video)}"
    )
    readable_tokens = [
        *alignment_tokens,
        frame_token,
        f"rt-{compact_filename_token(parameters.retarget_model)}",
        media_token,
        f"bp-{compact_filename_token(parameters.blueprint_preset)}",
        stream_token,
    ]
    payload = json.dumps(
        {"schema": EXPORT_CONFIG_SCHEMA, "parameters": asdict(parameters)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:10]
    suffix = "_" + "_".join([*readable_tokens, f"cfg-{digest}"])
    if path.stem.endswith(suffix):
        return path
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def gt_source_identifiers(gt_config: GTConfig | None) -> tuple[str, ...]:
    if gt_config is None:
        return ()
    sources: set[str] = set()
    if gt_config.skeleton is not None:
        sources.add("skeleton")
    if gt_config.mesh is not None:
        sources.add("mesh")
    sources.update(f"{track.source}:{track.label}" for track in gt_config.marker_tracks)
    if gt_config.third_person_video is not None:
        sources.add(f"third_person:{gt_config.third_person_video.name}")
    return tuple(sorted(sources))


def signal_time_range(session_dir: Path, spec: SignalSpec) -> tuple[int, int, int]:
    db_path = session_dir / spec.relative_path
    with sqlite3.connect(db_path) as connection:
        start_ns, end_ns, row_count = connection.execute(
            f"SELECT MIN(timestamp), MAX(timestamp), COUNT(*) FROM {spec.table}"
        ).fetchone()
    if start_ns is None or end_ns is None:
        return 0, 0, 0
    return int(start_ns), int(end_ns), int(row_count)


def video_time_range(session_dir: Path, spec: VideoSpec, video_path: Path) -> tuple[int, int, int]:
    video_asset = rr.AssetVideo(path=video_path)
    frame_timestamps_ns = np.asarray(video_asset.read_frame_timestamps_nanos(), dtype=np.int64)
    if len(frame_timestamps_ns) == 0:
        start_ns = metadata_comment_us(session_dir / spec.relative_path) * 1_000
        return start_ns, start_ns, 0
    capture_timestamps_ns = (
        metadata_comment_us(session_dir / spec.relative_path) * 1_000 + frame_timestamps_ns
    )
    return (
        int(capture_timestamps_ns[0]),
        int(capture_timestamps_ns[-1]),
        int(len(capture_timestamps_ns)),
    )


def video_capture_timestamps_ns(
    session_dir: Path,
    spec: VideoSpec,
    video_path: Path,
) -> np.ndarray:
    video_asset = rr.AssetVideo(path=video_path)
    frame_timestamps_ns = np.asarray(video_asset.read_frame_timestamps_nanos(), dtype=np.int64)
    return metadata_comment_us(session_dir / spec.relative_path) * 1_000 + frame_timestamps_ns


def video_nominal_frame_rate_hz(path: Path) -> float | None:
    stream = next(
        (
            stream
            for stream in probe_video(path).get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        None,
    )
    if stream is None:
        return None
    rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if not rate or rate == "0/0":
        return None
    numerator, denominator = rate.split("/")
    denominator_float = float(denominator)
    if denominator_float == 0:
        return None
    return float(numerator) / denominator_float


def reference_video_timestamps_ns(
    session_dir: Path,
    config: SessionConfig,
    artifact_paths: ArtifactPaths,
    use_proxy: bool,
    proxy_height: int,
    proxy_crf: int,
    proxy_bitrate: str,
    ffmpeg: str,
    preferred_label: str,
) -> np.ndarray | None:
    spec = config.videos.get(preferred_label)
    if spec is None:
        spec = next(iter(config.videos.values()), None)
    if spec is None:
        return None
    video_path = resolve_video_path(
        session_dir,
        spec,
        artifact_paths,
        use_proxy,
        proxy_height,
        proxy_crf,
        proxy_bitrate,
        ffmpeg,
    )
    timestamps_ns = video_capture_timestamps_ns(session_dir, spec, video_path)
    return timestamps_ns if len(timestamps_ns) else None


def reference_video_nominal_rate_hz(
    session_dir: Path,
    config: SessionConfig,
    artifact_paths: ArtifactPaths,
    use_proxy: bool,
    proxy_height: int,
    proxy_crf: int,
    proxy_bitrate: str,
    ffmpeg: str,
    preferred_label: str,
) -> float | None:
    spec = config.videos.get(preferred_label)
    if spec is None:
        spec = next(iter(config.videos.values()), None)
    if spec is None:
        return None
    return video_nominal_frame_rate_hz(session_dir / spec.relative_path)


def infer_gt_frame_rate_hz(gt_config: GTConfig) -> float | None:
    candidates: list[np.ndarray] = []
    if gt_config.skeleton is not None:
        candidates.append(gt_config.skeleton.timestamps_ns)
    if gt_config.mesh is not None:
        candidates.append(gt_config.mesh.timestamps_ns)
    candidates.extend(track.timestamps_ns for track in gt_config.marker_tracks)
    candidates.extend(track.timestamps_ns for track in gt_config.mano_mesh_tracks)
    for timestamps_ns in candidates:
        if len(timestamps_ns) < 2:
            continue
        duration_s = (int(timestamps_ns[-1]) - int(timestamps_ns[0])) / 1e9
        if duration_s > 0:
            return round((len(timestamps_ns) - 1) / duration_s)
    return None


def synthesize_frame_aligned_timestamps(
    frame_count: int,
    reference_video_timestamps: np.ndarray,
    ratio: float,
    frame_offset: int = 0,
) -> np.ndarray:
    if frame_count <= 0:
        return np.asarray([], dtype=np.int64)
    if len(reference_video_timestamps) == 1:
        return np.full(frame_count, int(reference_video_timestamps[0]), dtype=np.int64)
    video_dt_ns = int(np.median(np.diff(reference_video_timestamps)))
    result = np.empty(frame_count, dtype=np.int64)
    for gt_index in range(frame_count):
        video_float = (gt_index - frame_offset) / ratio
        video_index = int(np.floor(video_float))
        fraction = video_float - video_index
        if video_index < 0:
            result[gt_index] = int(
                round(int(reference_video_timestamps[0]) + video_float * video_dt_ns)
            )
        elif video_index + 1 < len(reference_video_timestamps):
            start_ns = int(reference_video_timestamps[video_index])
            end_ns = int(reference_video_timestamps[video_index + 1])
            result[gt_index] = int(round(start_ns + (end_ns - start_ns) * fraction))
        else:
            result[gt_index] = int(
                round(
                    int(reference_video_timestamps[-1])
                    + (video_float - (len(reference_video_timestamps) - 1)) * video_dt_ns
                )
            )
    return result


def describe_frame_alignment(ratio: float, video_frame_offset: int) -> str:
    return FrameAlignment(ratio, video_frame_offset).describe()


def resolve_gt_frame_ratio(
    gt_config: GTConfig | None,
    video_rate_hz: float | None,
    frame_ratio: float | None,
) -> float | None:
    if frame_ratio is not None:
        return max(1.0, float(frame_ratio))
    if gt_config is None:
        return None
    gt_rate_hz = infer_gt_frame_rate_hz(gt_config)
    if gt_rate_hz is None or video_rate_hz is None or video_rate_hz <= 0:
        return None
    return float(round_positive_ratio(float(gt_rate_hz) / float(video_rate_hz)))


def with_frame_aligned_gt_timestamps(
    gt_config: GTConfig | None,
    reference_video_timestamps: np.ndarray | None,
    video_rate_hz: float | None,
    frame_ratio: float | None = None,
    video_frame_offset: int = 0,
) -> GTConfig | None:
    if (
        gt_config is None
        or reference_video_timestamps is None
        or len(reference_video_timestamps) == 0
    ):
        return gt_config
    ratio = resolve_gt_frame_ratio(gt_config, video_rate_hz, frame_ratio)
    if ratio is None:
        return gt_config
    alignment = FrameAlignment(ratio, video_frame_offset)

    def aligned(timestamps_ns: np.ndarray) -> np.ndarray:
        return synthesize_frame_aligned_timestamps(
            len(timestamps_ns), reference_video_timestamps, ratio, alignment.gt_frame_offset
        )

    skeleton = (
        replace(gt_config.skeleton, timestamps_ns=aligned(gt_config.skeleton.timestamps_ns))
        if gt_config.skeleton is not None
        else None
    )
    mesh = (
        replace(gt_config.mesh, timestamps_ns=aligned(gt_config.mesh.timestamps_ns))
        if gt_config.mesh is not None
        else None
    )
    marker_tracks = tuple(
        replace(track, timestamps_ns=aligned(track.timestamps_ns))
        for track in gt_config.marker_tracks
    )
    mano_mesh_tracks = tuple(
        replace(track, timestamps_ns=aligned(track.timestamps_ns))
        for track in gt_config.mano_mesh_tracks
    )
    note = gt_config.note or ""
    note = (note + "\n" if note else "") + alignment.describe()
    return replace(
        gt_config,
        skeleton=skeleton,
        mesh=mesh,
        marker_tracks=marker_tracks,
        mano_mesh_tracks=mano_mesh_tracks,
        third_person_start_ns=int(reference_video_timestamps[0]),
        note=note,
    )


def gt_time_ranges(gt_config: GTConfig | None) -> list[tuple[int, int, int]]:
    if gt_config is None:
        return []
    ranges: list[tuple[int, int, int]] = []
    if gt_config.skeleton is not None and len(gt_config.skeleton.timestamps_ns):
        ranges.append(
            (
                int(gt_config.skeleton.timestamps_ns[0]),
                int(gt_config.skeleton.timestamps_ns[-1]),
                len(gt_config.skeleton.timestamps_ns),
            )
        )
    if gt_config.mesh is not None and len(gt_config.mesh.timestamps_ns):
        ranges.append(
            (
                int(gt_config.mesh.timestamps_ns[0]),
                int(gt_config.mesh.timestamps_ns[-1]),
                len(gt_config.mesh.timestamps_ns),
            )
        )
    for track in gt_config.marker_tracks:
        if len(track.timestamps_ns):
            ranges.append(
                (
                    int(track.timestamps_ns[0]),
                    int(track.timestamps_ns[-1]),
                    len(track.timestamps_ns),
                )
            )
    for track in gt_config.mano_mesh_tracks:
        if len(track.timestamps_ns):
            ranges.append(
                (
                    int(track.timestamps_ns[0]),
                    int(track.timestamps_ns[-1]),
                    len(track.timestamps_ns),
                )
            )
    if gt_config.third_person_video is not None and gt_config.third_person_start_ns is not None:
        video_asset = rr.AssetVideo(path=gt_config.third_person_video)
        frame_timestamps_ns = np.asarray(video_asset.read_frame_timestamps_nanos(), dtype=np.int64)
        if len(frame_timestamps_ns):
            start_ns = int(
                gt_config.third_person_start_ns + gt_config.time_offset_ns + frame_timestamps_ns[0]
            )
            end_ns = int(
                gt_config.third_person_start_ns + gt_config.time_offset_ns + frame_timestamps_ns[-1]
            )
            ranges.append((start_ns, end_ns, len(frame_timestamps_ns)))
    return ranges


def compute_common_capture_window(
    session_dir: Path,
    config: SessionConfig,
    artifact_paths: ArtifactPaths,
    use_proxy: bool,
    proxy_height: int,
    proxy_crf: int,
    proxy_bitrate: str,
    ffmpeg: str,
    gt_config: GTConfig | None,
) -> TimeWindow | None:
    ranges: list[tuple[int, int, int]] = []
    for spec in config.videos.values():
        video_path = resolve_video_path(
            session_dir,
            spec,
            artifact_paths,
            use_proxy,
            proxy_height,
            proxy_crf,
            proxy_bitrate,
            ffmpeg,
        )
        ranges.append(video_time_range(session_dir, spec, video_path))
    for spec in config.signals.values():
        ranges.append(signal_time_range(session_dir, spec))
    ranges.extend(gt_time_ranges(gt_config))

    valid_ranges = [
        (start_ns, end_ns) for start_ns, end_ns, count in ranges if count and end_ns >= start_ns
    ]
    if not valid_ranges:
        return None
    start_ns = max(start_ns for start_ns, _ in valid_ranges)
    end_ns = min(end_ns for _, end_ns in valid_ranges)
    if end_ns <= start_ns:
        return None
    return TimeWindow(start_ns=start_ns, end_ns=end_ns)


def time_mask(timestamps_ns: np.ndarray, window: TimeWindow | None) -> np.ndarray:
    if window is None:
        return np.ones(len(timestamps_ns), dtype=bool)
    return (timestamps_ns >= window.start_ns) & (timestamps_ns <= window.end_ns)


def write_time_alignment_report(
    session_dir: Path,
    config: SessionConfig,
    artifact_paths: ArtifactPaths,
    use_proxy: bool,
    proxy_height: int,
    proxy_crf: int,
    proxy_bitrate: str,
    ffmpeg: str,
    gt_config: GTConfig | None,
    alignment_mode: str,
) -> Path:
    rows: list[dict[str, Any]] = []
    reference_start_ns: int | None = None

    for label, spec in config.videos.items():
        video_path = resolve_video_path(
            session_dir,
            spec,
            artifact_paths,
            use_proxy,
            proxy_height,
            proxy_crf,
            proxy_bitrate,
            ffmpeg,
        )
        start_ns, end_ns, count = video_time_range(session_dir, spec, video_path)
        if reference_start_ns is None and label == "left":
            reference_start_ns = start_ns
        rows.append(
            {
                "source": label,
                "kind": "video",
                "timeline": "capture_time",
                "start_ns": start_ns,
                "end_ns": end_ns,
                "duration_s": (end_ns - start_ns) / 1e9 if count else 0.0,
                "count": count,
                "offset_from_left_video_s": "",
            }
        )

    if reference_start_ns is None and rows:
        reference_start_ns = int(rows[0]["start_ns"])

    for label, spec in config.signals.items():
        start_ns, end_ns, count = signal_time_range(session_dir, spec)
        rows.append(
            {
                "source": label,
                "kind": "signal",
                "timeline": "capture_time",
                "start_ns": start_ns,
                "end_ns": end_ns,
                "duration_s": (end_ns - start_ns) / 1e9 if count else 0.0,
                "count": count,
                "offset_from_left_video_s": "",
            }
        )

    if gt_config is not None:
        for track in gt_config.marker_tracks:
            if len(track.timestamps_ns):
                rows.append(
                    {
                        "source": track.label,
                        "kind": f"gt_{track.source}_skeleton",
                        "timeline": "capture_time",
                        "start_ns": int(track.timestamps_ns[0]),
                        "end_ns": int(track.timestamps_ns[-1]),
                        "duration_s": (int(track.timestamps_ns[-1]) - int(track.timestamps_ns[0]))
                        / 1e9,
                        "count": len(track.timestamps_ns),
                        "offset_from_left_video_s": "",
                    }
                )
        for track in gt_config.mano_mesh_tracks:
            if len(track.timestamps_ns):
                rows.append(
                    {
                        "source": track.label,
                        "kind": f"gt_{track.source}_mano_mesh",
                        "timeline": "capture_time",
                        "start_ns": int(track.timestamps_ns[0]),
                        "end_ns": int(track.timestamps_ns[-1]),
                        "duration_s": (int(track.timestamps_ns[-1]) - int(track.timestamps_ns[0]))
                        / 1e9,
                        "count": len(track.timestamps_ns),
                        "offset_from_left_video_s": "",
                    }
                )
        if gt_config.third_person_video is not None and gt_config.third_person_start_ns is not None:
            video_asset = rr.AssetVideo(path=gt_config.third_person_video)
            frame_timestamps_ns = np.asarray(
                video_asset.read_frame_timestamps_nanos(), dtype=np.int64
            )
            if len(frame_timestamps_ns):
                start_ns = int(
                    gt_config.third_person_start_ns
                    + gt_config.time_offset_ns
                    + frame_timestamps_ns[0]
                )
                end_ns = int(
                    gt_config.third_person_start_ns
                    + gt_config.time_offset_ns
                    + frame_timestamps_ns[-1]
                )
                rows.append(
                    {
                        "source": "gt_third_person_video",
                        "kind": "gt_video",
                        "timeline": "capture_time",
                        "start_ns": start_ns,
                        "end_ns": end_ns,
                        "duration_s": (end_ns - start_ns) / 1e9,
                        "count": len(frame_timestamps_ns),
                        "offset_from_left_video_s": "",
                    }
                )

    if reference_start_ns is not None:
        for row in rows:
            row["offset_from_left_video_s"] = (int(row["start_ns"]) - reference_start_ns) / 1e9

    report_path = (
        artifact_paths.inspection_dir
        / f"{session_dir.name}_{config.segment_name}_{alignment_mode}_aligned_time_alignment_report.csv"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "source",
                "kind",
                "timeline",
                "start_ns",
                "end_ns",
                "duration_s",
                "count",
                "offset_from_left_video_s",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return report_path


def log_video(
    session_dir: Path,
    spec: VideoSpec,
    artifact_paths: ArtifactPaths,
    use_proxy: bool,
    proxy_height: int,
    proxy_crf: int,
    proxy_bitrate: str,
    ffmpeg: str,
    capture_window: TimeWindow | None,
    timeline: TimelineContext,
) -> None:
    source_path = session_dir / spec.relative_path
    video_path = resolve_video_path(
        session_dir,
        spec,
        artifact_paths,
        use_proxy,
        proxy_height,
        proxy_crf,
        proxy_bitrate,
        ffmpeg,
    )
    video_asset = rr.AssetVideo(path=video_path)
    rr.log(spec.entity, video_asset, static=True)

    frame_timestamps_ns = np.asarray(video_asset.read_frame_timestamps_nanos(), dtype=np.int64)
    capture_timestamps_ns = metadata_comment_us(source_path) * 1_000 + frame_timestamps_ns
    mask = time_mask(capture_timestamps_ns, capture_window)
    frame_timestamps_ns = frame_timestamps_ns[mask]
    capture_timestamps_ns = capture_timestamps_ns[mask]
    if len(capture_timestamps_ns) == 0:
        return
    rr.send_columns(
        spec.entity,
        indexes=timeline.indexes(capture_timestamps_ns),
        columns=rr.VideoFrameReference.columns_nanos(frame_timestamps_ns),
    )


def log_signal(
    session_dir: Path,
    spec: SignalSpec,
    max_points: int,
    capture_window: TimeWindow | None,
    timeline: TimelineContext,
) -> None:
    timestamps_ns, values_by_axis = fetch_signal_rows(
        session_dir / spec.relative_path,
        spec.table,
        spec.columns,
        max_points,
        capture_window,
    )
    if len(timestamps_ns) == 0:
        return
    indexes = timeline.indexes(timestamps_ns)

    for axis, values in values_by_axis.items():
        entity = f"{spec.origin}/{axis}"
        rr.log(
            entity,
            rr.SeriesLines(names=[axis], colors=[AXIS_COLORS[axis]], widths=[1.5]),
            static=True,
        )
        rr.send_columns(entity, indexes=indexes, columns=rr.Scalars.columns(scalars=values))


def log_note(note: NoteSpec) -> None:
    rr.log(note.origin, rr.TextDocument(note.text), static=True)


def normalize_timestamps_ns(raw: Any, frame_count: int, time_offset_ns: int) -> np.ndarray:
    if raw is None:
        timestamps_ns = np.arange(frame_count, dtype=np.int64) * int(1e9 / 30)
    else:
        timestamps = np.asarray(raw)
        if timestamps.ndim != 1:
            raise ValueError(f"GT timestamps must be 1-D, got {timestamps.shape}")
        if len(timestamps) != frame_count:
            raise ValueError(
                f"GT timestamp count {len(timestamps)} does not match frame count {frame_count}"
            )
        if np.issubdtype(timestamps.dtype, np.floating):
            timestamps_ns = (timestamps.astype(np.float64) * 1e9).astype(np.int64)
        else:
            timestamps_ns = timestamps.astype(np.int64)
    return timestamps_ns + time_offset_ns


def coerce_joints(raw: Any, scale: float) -> np.ndarray:
    joints = np.asarray(raw, dtype=np.float32)
    if joints.ndim != 3 or joints.shape[2] != 3:
        raise ValueError(f"GT joints must have shape [frames, joints, 3], got {joints.shape}")
    return joints * scale


def coerce_vertices(raw: Any, frame_count: int, scale: float) -> np.ndarray:
    vertices = np.asarray(raw, dtype=np.float32)
    if vertices.ndim == 2 and vertices.shape[1] == 3:
        vertices = np.broadcast_to(vertices[None, :, :], (frame_count, vertices.shape[0], 3)).copy()
    if vertices.ndim != 3 or vertices.shape[2] != 3:
        raise ValueError(
            f"GT mesh vertices must have shape [frames, vertices, 3] or [vertices, 3], got {vertices.shape}"
        )
    if vertices.shape[0] != frame_count:
        raise ValueError(
            f"GT mesh frame count {vertices.shape[0]} does not match expected {frame_count}"
        )
    return vertices * scale


def coerce_faces(raw: Any) -> np.ndarray:
    faces = np.asarray(raw, dtype=np.uint32)
    if faces.ndim != 2 or faces.shape[1] != 3:
        raise ValueError(f"GT mesh faces must have shape [faces, 3], got {faces.shape}")
    return faces


def read_npz_dict(path: Path) -> dict[str, Any]:
    with np.load(path, allow_pickle=True) as data:
        return {key: data[key] for key in data.files}


def read_json_dict(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {"frames": data}
    if not isinstance(data, dict):
        raise ValueError(f"JSON GT file must contain an object or a frame list: {path}")
    return data


def first_present(data: dict[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def load_skeleton_csv(path: Path, scale: float, time_offset_ns: int) -> GTSkeletonTrack:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"empty skeleton CSV: {path}")

    required = {"timestamp", "joint", "x", "y", "z"}
    if not required.issubset(rows[0]):
        raise ValueError("skeleton CSV must contain timestamp,joint,x,y,z columns")

    timestamps = sorted({int(float(row["timestamp"])) for row in rows})
    joint_names = tuple(sorted({row["joint"] for row in rows}))
    timestamp_index = {value: index for index, value in enumerate(timestamps)}
    joint_index = {value: index for index, value in enumerate(joint_names)}
    joints = np.full((len(timestamps), len(joint_names), 3), np.nan, dtype=np.float32)

    for row in rows:
        t = timestamp_index[int(float(row["timestamp"]))]
        j = joint_index[row["joint"]]
        joints[t, j] = [float(row["x"]), float(row["y"]), float(row["z"])]

    if np.isnan(joints).any():
        raise ValueError(
            f"skeleton CSV is missing joint samples for at least one timestamp: {path}"
        )

    parents = DEFAULT_SKELETON_PARENTS[: len(joint_names)]
    if len(parents) < len(joint_names):
        parents = tuple([-1, *range(0, len(joint_names) - 1)])
    return GTSkeletonTrack(
        timestamps_ns=normalize_timestamps_ns(timestamps, len(timestamps), time_offset_ns),
        joints=joints * scale,
        joint_names=joint_names,
        parents=parents,
    )


def load_skeleton_track(
    path: Path, scale: float, time_offset_ns: int, max_frames: int | None
) -> GTSkeletonTrack:
    if path.suffix.lower() == ".csv":
        track = load_skeleton_csv(path, scale, time_offset_ns)
    else:
        data = read_npz_dict(path) if path.suffix.lower() == ".npz" else read_json_dict(path)
        joints_raw = first_present(data, ("joints", "joint_positions", "keypoints3d"))
        if joints_raw is None and "frames" in data:
            joints_raw = [frame["joints"] for frame in data["frames"]]
        joints = coerce_joints(joints_raw, scale)
        timestamps_raw = first_present(data, ("capture_time_ns", "timestamps_ns", "timestamps"))
        if timestamps_raw is None and "frames" in data:
            timestamps_raw = [
                frame.get("capture_time_ns", frame.get("timestamp")) for frame in data["frames"]
            ]
        joint_names_raw = data.get("joint_names")
        joint_names = (
            tuple(str(name) for name in joint_names_raw)
            if joint_names_raw is not None
            else tuple(f"joint_{i:02d}" for i in range(joints.shape[1]))
        )
        parents_raw = first_present(data, ("parents", "skeleton_parents"))
        parents = (
            tuple(int(value) for value in parents_raw)
            if parents_raw is not None
            else DEFAULT_SKELETON_PARENTS[: joints.shape[1]]
        )
        if len(parents) < joints.shape[1]:
            parents = tuple([-1, *range(0, joints.shape[1] - 1)])
        track = GTSkeletonTrack(
            timestamps_ns=normalize_timestamps_ns(timestamps_raw, joints.shape[0], time_offset_ns),
            joints=joints,
            joint_names=joint_names,
            parents=parents,
        )

    return downsample_gt_skeleton(track, max_frames)


def load_mesh_track(
    path: Path,
    scale: float,
    time_offset_ns: int,
    max_frames: int | None,
    fallback_timestamps_ns: np.ndarray | None,
) -> GTMeshTrack:
    data = read_npz_dict(path) if path.suffix.lower() == ".npz" else read_json_dict(path)
    vertices_raw = first_present(data, ("vertices", "verts", "vertex_positions"))
    if vertices_raw is None and "frames" in data:
        vertices_raw = [frame["vertices"] for frame in data["frames"]]
    faces_raw = first_present(data, ("faces", "triangles", "triangle_indices"))
    if faces_raw is None:
        raise ValueError(f"GT mesh file must contain faces/triangles: {path}")

    frame_count = (
        np.asarray(vertices_raw).shape[0]
        if np.asarray(vertices_raw).ndim == 3
        else (len(fallback_timestamps_ns) if fallback_timestamps_ns is not None else 1)
    )
    timestamps_raw = first_present(data, ("capture_time_ns", "timestamps_ns", "timestamps"))
    if (
        timestamps_raw is None
        and fallback_timestamps_ns is not None
        and len(fallback_timestamps_ns) == frame_count
    ):
        timestamps_ns = fallback_timestamps_ns
    else:
        timestamps_ns = normalize_timestamps_ns(timestamps_raw, frame_count, time_offset_ns)
    track = GTMeshTrack(
        timestamps_ns=timestamps_ns,
        vertices=coerce_vertices(vertices_raw, len(timestamps_ns), scale),
        faces=coerce_faces(faces_raw),
    )
    return downsample_gt_mesh(track, max_frames)


def downsample_indexes(frame_count: int, max_frames: int | None) -> np.ndarray:
    if max_frames is None or frame_count <= max_frames:
        return np.arange(frame_count, dtype=np.int64)
    return np.linspace(0, frame_count - 1, max_frames).astype(np.int64)


def downsample_gt_skeleton(track: GTSkeletonTrack, max_frames: int | None) -> GTSkeletonTrack:
    indexes = downsample_indexes(len(track.timestamps_ns), max_frames)
    return GTSkeletonTrack(
        timestamps_ns=track.timestamps_ns[indexes],
        joints=track.joints[indexes],
        joint_names=track.joint_names,
        parents=track.parents,
    )


def downsample_gt_mesh(track: GTMeshTrack, max_frames: int | None) -> GTMeshTrack:
    indexes = downsample_indexes(len(track.timestamps_ns), max_frames)
    return GTMeshTrack(
        timestamps_ns=track.timestamps_ns[indexes],
        vertices=track.vertices[indexes],
        faces=track.faces,
    )


def downsample_gt_marker_track(track: GTMarkerTrack, max_frames: int | None) -> GTMarkerTrack:
    indexes = downsample_indexes(len(track.timestamps_ns), max_frames)
    return replace(
        track,
        timestamps_ns=track.timestamps_ns[indexes],
        positions=track.positions[indexes],
    )


def parse_trc(
    path: Path, scale: float, max_frames: int | None, marker_limit: int | None = None
) -> tuple[np.ndarray, tuple[str, ...], np.ndarray]:
    lines = read_text_lines_with_fallback(path)
    frame_header_index = next(
        (index for index, line in enumerate(lines) if line.startswith("Frame#")), None
    )
    if frame_header_index is None:
        raise ValueError(f"TRC file has no Frame# header: {path}")

    header_cells = lines[frame_header_index].split("\t")
    marker_names = tuple(cell.strip() for cell in header_cells[3:] if cell.strip())
    if marker_limit is not None:
        marker_names = marker_names[:marker_limit]
    if not marker_names:
        raise ValueError(f"TRC file has no marker names: {path}")

    timestamps: list[int] = []
    frames: list[list[list[float]]] = []
    for line in lines[frame_header_index + 1 :]:
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) < 3 or not cells[0].strip().isdigit():
            continue
        timestamps.append(int(float(cells[2].strip())) * 1_000_000)
        frame_positions: list[list[float]] = []
        for marker_index in range(len(marker_names)):
            base = 3 + marker_index * 3
            xyz = cells[base : base + 3]
            if len(xyz) < 3 or any(not value.strip() for value in xyz):
                frame_positions.append([np.nan, np.nan, np.nan])
            else:
                frame_positions.append(
                    [float(xyz[0]) * scale, float(xyz[1]) * scale, float(xyz[2]) * scale]
                )
        frames.append(frame_positions)

    if not frames:
        raise ValueError(f"TRC file has no frame rows: {path}")
    track_positions = np.asarray(frames, dtype=np.float32)
    if np.isnan(track_positions).any():
        track_positions = fill_missing_marker_positions(track_positions)
    timestamps_ns = np.asarray(timestamps, dtype=np.int64)
    indexes = downsample_indexes(len(timestamps_ns), max_frames)
    return timestamps_ns[indexes], marker_names, track_positions[indexes]


def fill_missing_marker_positions(positions: np.ndarray) -> np.ndarray:
    filled = positions.copy()
    for marker_index in range(filled.shape[1]):
        marker = filled[:, marker_index, :]
        valid = ~np.isnan(marker).any(axis=1)
        if not valid.any():
            marker[:] = 0.0
            continue
        valid_indexes = np.flatnonzero(valid)
        for axis in range(3):
            marker[:, axis] = np.interp(np.arange(len(marker)), valid_indexes, marker[valid, axis])
    return filled


def default_hand_connections(marker_names: Sequence[str]) -> tuple[tuple[int, int], ...]:
    name_to_index = {name: index for index, name in enumerate(marker_names)}
    fingers = ("FingerThumb", "FingerIndex", "FingerMiddle", "FingerRing", "FingerPinky")
    connections: list[tuple[int, int]] = []
    wrist_candidates = [
        name_to_index[name]
        for name in ("WristM", "WristIn", "WristOut", "HandOffset")
        if name in name_to_index
    ]
    palm_anchor = wrist_candidates[0] if wrist_candidates else None
    for first, second in zip(wrist_candidates, wrist_candidates[1:]):
        connections.append((first, second))
    for finger in fingers:
        chain = [
            name_to_index[f"{finger}{i}"] for i in range(1, 5) if f"{finger}{i}" in name_to_index
        ]
        if palm_anchor is not None and chain:
            connections.append((palm_anchor, chain[0]))
        connections.extend((first, second) for first, second in zip(chain, chain[1:]))
    return tuple(connections)


def load_marker_track_from_trc(
    path: Path,
    label: str,
    entity: str,
    source: str,
    scale: float,
    max_frames: int | None,
    marker_limit: int | None = None,
    colors: tuple[int, int, int] = (255, 210, 80),
    radius: float = 0.018,
    connect_hands: bool = False,
) -> GTMarkerTrack:
    timestamps_ns, marker_names, positions = parse_trc(
        path, scale, max_frames, marker_limit=marker_limit
    )
    return GTMarkerTrack(
        label=label,
        entity=entity,
        source=source,
        timestamps_ns=timestamps_ns,
        positions=positions,
        marker_names=marker_names,
        connections=default_hand_connections(marker_names) if connect_hands else (),
        colors=colors,
        radius=radius,
    )


def read_text_lines_with_fallback(path: Path) -> list[str]:
    for encoding in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=encoding).splitlines()
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("text", b"", 0, 1, f"unable to decode {path}")


def parse_bvh(
    path: Path, scale: float, max_frames: int | None, timestamps_ns: np.ndarray | None = None
) -> tuple[np.ndarray, tuple[str, ...], np.ndarray, tuple[int, ...]]:
    lines = read_text_lines_with_fallback(path)
    joints: list[BvhJoint] = []
    stack: list[int] = []
    pending_joint: tuple[str, int] | None = None
    channel_slices: list[tuple[int, int]] = []
    channel_cursor = 0
    motion_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "MOTION"), None
    )
    if motion_index is None:
        raise ValueError(f"BVH file has no MOTION section: {path}")

    index = 0
    while index < motion_index:
        stripped = lines[index].strip()
        parts = stripped.split()
        if not parts:
            index += 1
            continue
        if parts[0] in {"ROOT", "JOINT"}:
            pending_joint = (parts[1], stack[-1] if stack else -1)
        elif parts[0] == "End" and len(parts) > 1 and parts[1] == "Site":
            parent = stack[-1] if stack else -1
            pending_joint = (f"{joints[parent].name}_End", parent)
        elif parts[0] == "{":
            if pending_joint is not None:
                name, parent = pending_joint
                joints.append(
                    BvhJoint(
                        name=name, parent=parent, offset=np.zeros(3, dtype=np.float32), channels=()
                    )
                )
                stack.append(len(joints) - 1)
                pending_joint = None
        elif parts[0] == "}":
            if stack:
                stack.pop()
        elif parts[0] == "OFFSET" and stack:
            joints[stack[-1]] = replace(
                joints[stack[-1]],
                offset=np.asarray([float(value) for value in parts[1:4]], dtype=np.float32) * scale,
            )
        elif parts[0] == "CHANNELS" and stack:
            count = int(parts[1])
            channels = tuple(parts[2 : 2 + count])
            joints[stack[-1]] = replace(joints[stack[-1]], channels=channels)
            channel_slices.append((channel_cursor, channel_cursor + count))
            channel_cursor += count
        index += 1

    frame_count = int(lines[motion_index + 1].split(":", 1)[1].strip())
    frame_time = float(lines[motion_index + 2].split(":", 1)[1].strip())
    motion_rows = [
        [float(value) for value in line.strip().split()]
        for line in lines[motion_index + 3 :]
        if line.strip()
    ]
    values = np.asarray(motion_rows[:frame_count], dtype=np.float32)
    if values.shape[1] != channel_cursor:
        raise ValueError(
            f"BVH channel count mismatch in {path}: expected {channel_cursor}, got {values.shape[1]}"
        )

    if timestamps_ns is None or len(timestamps_ns) != len(values):
        timestamps_ns = (np.arange(len(values), dtype=np.int64) * int(frame_time * 1e9)).astype(
            np.int64
        )

    positions = bvh_global_positions(joints, channel_slices, values, scale)
    indexes = downsample_indexes(len(timestamps_ns), max_frames)
    return (
        timestamps_ns[indexes],
        tuple(joint.name for joint in joints),
        positions[indexes],
        tuple(joint.parent for joint in joints),
    )


def rotation_matrix(axis: str, degrees: float) -> np.ndarray:
    radians = np.deg2rad(float(degrees))
    c = float(np.cos(radians))
    s = float(np.sin(radians))
    if axis == "X":
        return np.asarray([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)
    if axis == "Y":
        return np.asarray([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float32)
    if axis == "Z":
        return np.asarray([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float32)
    raise ValueError(f"unsupported BVH rotation axis: {axis}")


def bvh_global_positions(
    joints: Sequence[BvhJoint],
    channel_slices: Sequence[tuple[int, int]],
    values: np.ndarray,
    scale: float,
) -> np.ndarray:
    positions = np.zeros((len(values), len(joints), 3), dtype=np.float32)
    rotations = np.zeros((len(joints), 3, 3), dtype=np.float32)
    translations = np.zeros((len(joints), 3), dtype=np.float32)

    for frame_index, row in enumerate(values):
        channel_slice_index = 0
        for joint_index, joint in enumerate(joints):
            local_translation = joint.offset.copy()
            local_rotation = np.eye(3, dtype=np.float32)
            if joint.channels:
                start, end = channel_slices[channel_slice_index]
                channel_slice_index += 1
                channel_values = row[start:end]
                for channel, value in zip(joint.channels, channel_values):
                    if channel.endswith("position"):
                        local_translation["XYZ".index(channel[0])] = float(value) * scale
                    elif channel.endswith("rotation"):
                        local_rotation = local_rotation @ rotation_matrix(channel[0], float(value))
            parent = joint.parent
            if parent < 0:
                rotations[joint_index] = local_rotation
                translations[joint_index] = local_translation
            else:
                rotations[joint_index] = rotations[parent] @ local_rotation
                translations[joint_index] = (
                    translations[parent] + rotations[parent] @ local_translation
                )
            positions[frame_index, joint_index] = translations[joint_index]
    return positions


def load_bvh_track(
    path: Path,
    label: str,
    entity: str,
    source: str,
    scale: float,
    max_frames: int | None,
    timestamps_ns: np.ndarray | None = None,
    colors: tuple[int, int, int] = (255, 210, 80),
    radius: float = 0.014,
) -> GTMarkerTrack:
    track_timestamps_ns, joint_names, positions, parents = parse_bvh(
        path, scale, max_frames, timestamps_ns=timestamps_ns
    )
    connections = tuple((parent, child) for child, parent in enumerate(parents) if parent >= 0)
    return GTMarkerTrack(
        label=label,
        entity=entity,
        source=source,
        timestamps_ns=track_timestamps_ns,
        positions=positions,
        marker_names=joint_names,
        connections=connections,
        colors=colors,
        radius=radius,
    )


def split_nokov_cells(line: str, delimiter: str) -> list[str]:
    if delimiter == ",":
        return [cell.strip() for cell in next(csv.reader([line]))]
    if delimiter == "xrs" and "\t" in line:
        return [cell.strip() for cell in line.rstrip("\r\n").split("\t")]
    return line.strip().split()


def nokov_head_metadata(path: Path) -> dict[str, str]:
    if path.suffix.lower() not in {".csv", ".xrs"}:
        return {}
    delimiter = "," if path.suffix.lower() == ".csv" else "xrs"
    lines = read_text_lines_with_fallback(path)
    section_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "[Head]"), None
    )
    if section_index is None:
        return {}
    row_indexes = [index for index in range(section_index + 1, len(lines)) if lines[index].strip()]
    if len(row_indexes) < 2:
        return {}
    header = split_nokov_cells(lines[row_indexes[0]], delimiter)
    values = split_nokov_cells(lines[row_indexes[1]], delimiter)
    return {
        name: values[index] for index, name in enumerate(header) if name and index < len(values)
    }


def infer_nokov_view_up_axis(paths: Sequence[Path]) -> str | None:
    counts: dict[str, int] = {}
    for path in paths:
        bone_axis = nokov_head_metadata(path).get("BoneAxis", "").upper()
        # XingYing's hand template uses +Z as its bone axis in a Y-up calibration,
        # and +Y in a Z-up calibration.
        up_axis = {"Z": "y", "Y": "z"}.get(bone_axis)
        if up_axis is not None:
            counts[up_axis] = counts.get(up_axis, 0) + 1
    return max(counts, key=lambda axis: (counts[axis], axis)) if counts else None


def parse_nokov_segment_hierarchy(
    lines: Sequence[str], delimiter: str = ","
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    section_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "[SegmentNames&Hierarchy]"),
        None,
    )
    if section_index is None:
        raise ValueError("NOKOV rigid file has no [SegmentNames&Hierarchy] section")

    names: list[str] = []
    parents_raw: list[str] = []
    for line in lines[section_index + 2 :]:
        stripped = line.strip()
        if not stripped:
            break
        if stripped.startswith("["):
            break
        cells = [cell for cell in split_nokov_cells(stripped, delimiter) if cell]
        if not cells:
            continue
        names.append(cells[0])
        parents_raw.append(cells[1] if len(cells) > 1 else "")

    name_to_index = {name: index for index, name in enumerate(names)}
    parents = tuple(name_to_index.get(parent, -1) if parent else -1 for parent in parents_raw)
    return tuple(names), parents


def parse_nokov_pose_file(
    path: Path, scale: float, max_frames: int | None, delimiter: str
) -> tuple[np.ndarray, tuple[str, ...], np.ndarray, tuple[int, ...]]:
    lines = read_text_lines_with_fallback(path)
    names, parents = parse_nokov_segment_hierarchy(lines, delimiter)
    segment_data_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "[SegmentData]"), None
    )
    if segment_data_index is None:
        raise ValueError(f"NOKOV rigid file has no [SegmentData] section: {path}")

    header_index = next(
        (
            index
            for index in range(segment_data_index + 1, len(lines))
            if "Timestamp" in split_nokov_cells(lines[index], delimiter)
            and any(
                cell.startswith(("XToGlobal", "XToParent"))
                for cell in split_nokov_cells(lines[index], delimiter)
            )
        ),
        None,
    )
    if header_index is None:
        raise ValueError(f"NOKOV rigid file has no segment data header: {path}")
    header = split_nokov_cells(lines[header_index], delimiter)
    if len(header) > 1 and not header[0] and header[1].lower() == "timestamp":
        header[0] = "Frame#"
    elif header and header[0].lower() == "timestamp":
        header = ["Frame#"] + header
    x_columns: list[int] = []
    y_columns: list[int] = []
    z_columns: list[int] = []
    for segment_index in range(1, len(names) + 1):
        try:
            x_columns.append(header.index(f"XToGlobal{segment_index}"))
            y_columns.append(header.index(f"YToGlobal{segment_index}"))
            z_columns.append(header.index(f"ZToGlobal{segment_index}"))
        except ValueError:
            try:
                x_columns.append(header.index(f"XToParent{segment_index}"))
                y_columns.append(header.index(f"YToParent{segment_index}"))
                z_columns.append(header.index(f"ZToParent{segment_index}"))
            except ValueError as exc:
                raise ValueError(
                    f"NOKOV rigid file missing global or parent XYZ columns for segment {segment_index}: {path}"
                ) from exc

    timestamps: list[int] = []
    frames: list[list[list[float]]] = []
    for line in lines[header_index + 1 :]:
        if not line.strip():
            continue
        cells = split_nokov_cells(line, delimiter)
        if len(cells) < 2 or not cells[0].strip().isdigit():
            continue
        timestamps.append(int(float(cells[1].strip())) * 1_000_000)
        frame_positions: list[list[float]] = []
        for x_col, y_col, z_col in zip(x_columns, y_columns, z_columns):
            xyz = [
                cells[index].strip() if index < len(cells) else ""
                for index in (x_col, y_col, z_col)
            ]
            if any(not value for value in xyz):
                frame_positions.append([np.nan, np.nan, np.nan])
            else:
                frame_positions.append(
                    [float(xyz[0]) * scale, float(xyz[1]) * scale, float(xyz[2]) * scale]
                )
        frames.append(frame_positions)

    if not frames:
        raise ValueError(f"NOKOV rigid file has no data rows: {path}")
    positions = np.asarray(frames, dtype=np.float32)
    if np.isnan(positions).any():
        positions = fill_missing_marker_positions(positions)
    timestamps_ns = np.asarray(timestamps, dtype=np.int64)
    indexes = downsample_indexes(len(timestamps_ns), max_frames)
    return timestamps_ns[indexes], names, positions[indexes], parents


def parse_nokov_csv(
    path: Path, scale: float, max_frames: int | None
) -> tuple[np.ndarray, tuple[str, ...], np.ndarray, tuple[int, ...]]:
    return parse_nokov_pose_file(path, scale, max_frames, ",")


def parse_xrs(
    path: Path, scale: float, max_frames: int | None
) -> tuple[np.ndarray, tuple[str, ...], np.ndarray, tuple[int, ...]]:
    return parse_nokov_pose_file(path, scale, max_frames, "xrs")


def load_csv_track(
    path: Path,
    label: str,
    entity: str,
    source: str,
    scale: float,
    max_frames: int | None,
    colors: tuple[int, int, int] = (255, 210, 80),
    radius: float = 0.014,
) -> GTMarkerTrack:
    timestamps_ns, joint_names, positions, parents = parse_nokov_csv(path, scale, max_frames)
    connections = tuple((parent, child) for child, parent in enumerate(parents) if parent >= 0)
    return GTMarkerTrack(
        label=label,
        entity=entity,
        source=source,
        timestamps_ns=timestamps_ns,
        positions=positions,
        marker_names=joint_names,
        connections=connections,
        colors=colors,
        radius=radius,
    )


def load_xrs_track(
    path: Path,
    label: str,
    entity: str,
    source: str,
    scale: float,
    max_frames: int | None,
    colors: tuple[int, int, int] = (80, 200, 255),
    radius: float = 0.014,
) -> GTMarkerTrack:
    timestamps_ns, joint_names, positions, parents = parse_xrs(path, scale, max_frames)
    connections = tuple((parent, child) for child, parent in enumerate(parents) if parent >= 0)
    return GTMarkerTrack(
        label=label,
        entity=entity,
        source=source,
        timestamps_ns=timestamps_ns,
        positions=positions,
        marker_names=joint_names,
        connections=connections,
        colors=colors,
        radius=radius,
    )


def chumpy_safe_array(value: Any) -> np.ndarray:
    if hasattr(value, "r"):
        return np.asarray(value.r)
    return np.asarray(value)


class ChumpyPickleStub:
    """Accept legacy chumpy pickle state when only MANO's NumPy fields are needed."""

    def __new__(cls, *_args: Any, **_kwargs: Any) -> "ChumpyPickleStub":
        return super().__new__(cls)

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __setstate__(self, state: Any) -> None:
        if isinstance(state, dict):
            self.__dict__.update(state)
        else:
            self.__dict__["_pickle_state"] = state


class ManoPickleUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if module == "chumpy" or module.startswith("chumpy."):
            return ChumpyPickleStub
        return super().find_class(module, name)


@lru_cache(maxsize=4)
def load_mano_template(model_dir: Path, side: str) -> ManoTemplate:
    if side not in {"left", "right"}:
        raise ValueError(f"side must be left or right, got {side}")
    model_path = model_dir.resolve() / ("MANO_LEFT.pkl" if side == "left" else "MANO_RIGHT.pkl")
    if not model_path.exists():
        raise FileNotFoundError(model_path)

    with model_path.open("rb") as f:
        data = ManoPickleUnpickler(f, encoding="latin1").load()
    vertices = chumpy_safe_array(data["v_template"]).astype(np.float32)
    faces = chumpy_safe_array(data["f"]).astype(np.uint32)
    joints = chumpy_safe_array(data["J"]).astype(np.float32)
    parents = chumpy_safe_array(data["kintree_table"])[0].astype(np.int64)
    parents[parents > 1000] = -1
    weights = chumpy_safe_array(data["weights"]).astype(np.float32)
    return ManoTemplate(
        vertices=vertices, faces=faces, joints=joints, parents=parents, weights=weights
    )


def normalize_mano_template(template: ManoTemplate) -> ManoTemplate:
    center = template.vertices.mean(axis=0, keepdims=True)
    centered_vertices = template.vertices - center
    scale = float(np.percentile(np.linalg.norm(centered_vertices, axis=1), 95))
    if scale <= 1e-8:
        scale = 1.0
    return replace(
        template,
        vertices=(centered_vertices / scale).astype(np.float32),
        joints=((template.joints - center) / scale).astype(np.float32),
    )


def marker_index(marker_names: Sequence[str], name: str) -> int | None:
    try:
        return marker_names.index(name)
    except ValueError:
        return None


def normalize_vector(vector: np.ndarray, fallback: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-8:
        return fallback.astype(np.float32)
    return (vector / norm).astype(np.float32)


def rotation_between_vectors(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    source = normalize_vector(source, np.array([1.0, 0.0, 0.0], dtype=np.float32))
    target = normalize_vector(target, source)
    cross = np.cross(source, target)
    dot = float(np.clip(np.dot(source, target), -1.0, 1.0))
    if dot > 0.9999:
        return np.eye(3, dtype=np.float32)
    if dot < -0.9999:
        axis = normalize_vector(
            np.cross(source, np.array([1.0, 0.0, 0.0], dtype=np.float32)),
            np.array([0.0, 1.0, 0.0], dtype=np.float32),
        )
        if np.linalg.norm(axis) <= 1e-8:
            axis = normalize_vector(
                np.cross(source, np.array([0.0, 1.0, 0.0], dtype=np.float32)),
                np.array([0.0, 0.0, 1.0], dtype=np.float32),
            )
        return axis_angle_matrix(axis, np.pi)
    skew = np.asarray(
        [[0, -cross[2], cross[1]], [cross[2], 0, -cross[0]], [-cross[1], cross[0], 0]],
        dtype=np.float32,
    )
    return (np.eye(3, dtype=np.float32) + skew + skew @ skew * (1.0 / (1.0 + dot))).astype(
        np.float32
    )


def axis_angle_matrix(axis: np.ndarray, angle: float) -> np.ndarray:
    axis = normalize_vector(axis, np.array([1.0, 0.0, 0.0], dtype=np.float32))
    x, y, z = axis
    c = float(np.cos(angle))
    s = float(np.sin(angle))
    C = 1.0 - c
    return np.asarray(
        [
            [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
            [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
            [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
        ],
        dtype=np.float32,
    )


def hand_pose_from_markers(
    marker_names: Sequence[str], positions: np.ndarray
) -> tuple[np.ndarray, np.ndarray, float]:
    wrist_ids = [
        marker_index(marker_names, name)
        for name in ("WristM", "WristIn", "WristOut", "HandOffset", "LeftHand", "RightHand")
    ]
    wrist_ids = [index for index in wrist_ids if index is not None]
    origin = positions[wrist_ids].mean(axis=0) if wrist_ids else positions.mean(axis=0)

    wrist_in = marker_index(marker_names, "WristIn")
    wrist_out = marker_index(marker_names, "WristOut")
    if wrist_in is not None and wrist_out is not None:
        x_axis = normalize_vector(
            positions[wrist_out] - positions[wrist_in], np.array([1.0, 0.0, 0.0], dtype=np.float32)
        )
    else:
        index_base = next(
            (
                marker_index(marker_names, name)
                for name in ("LeftHandIndex0", "RightHandIndex0", "FingerIndex1")
                if marker_index(marker_names, name) is not None
            ),
            None,
        )
        pinky_base = next(
            (
                marker_index(marker_names, name)
                for name in ("LeftHandPinky0", "RightHandPinky0", "FingerPinky1")
                if marker_index(marker_names, name) is not None
            ),
            None,
        )
        if index_base is not None and pinky_base is not None:
            x_axis = normalize_vector(
                positions[index_base] - positions[pinky_base],
                np.array([1.0, 0.0, 0.0], dtype=np.float32),
            )
        else:
            x_axis = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    fingertip_ids = [
        marker_index(marker_names, name)
        for name in (
            "FingerThumb4",
            "FingerIndex4",
            "FingerMiddle4",
            "FingerRing4",
            "FingerPinky4",
            "LeftHandThumbEnd",
            "LeftHandIndexEnd",
            "LeftHandMiddleEnd",
            "LeftHandRingEnd",
            "LeftHandPinkyEnd",
            "RightHandThumbEnd",
            "RightHandIndexEnd",
            "RightHandMiddleEnd",
            "RightHandRingEnd",
            "RightHandPinkyEnd",
        )
    ]
    fingertip_ids = [index for index in fingertip_ids if index is not None]
    forward_point = (
        positions[fingertip_ids].mean(axis=0) if fingertip_ids else positions.mean(axis=0)
    )
    y_axis = normalize_vector(forward_point - origin, np.array([0.0, 1.0, 0.0], dtype=np.float32))
    z_axis = normalize_vector(np.cross(x_axis, y_axis), np.array([0.0, 0.0, 1.0], dtype=np.float32))
    y_axis = normalize_vector(np.cross(z_axis, x_axis), np.array([0.0, 1.0, 0.0], dtype=np.float32))
    basis = np.stack([x_axis, y_axis, z_axis], axis=1)

    distances = np.linalg.norm(positions - origin[None, :], axis=1)
    scale = float(np.nanpercentile(distances, 95))
    if scale <= 1e-6:
        scale = 0.08
    return origin.astype(np.float32), basis.astype(np.float32), scale


MANO_CHAIN_NAMES = {
    "thumb": (13, 14, 15),
    "index": (1, 2, 3),
    "middle": (4, 5, 6),
    "ring": (10, 11, 12),
    "pinky": (7, 8, 9),
}


def first_named_position(
    marker_names: Sequence[str], positions: np.ndarray, names: Sequence[str]
) -> np.ndarray | None:
    for name in names:
        index = marker_index(marker_names, name)
        if index is not None:
            return positions[index]
    return None


def finger_joint_names(
    side: str, finger: str
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    side_prefix = "LeftHand" if side == "left" else "RightHand"
    title = finger.capitalize()
    if finger == "pinky":
        title = "Pinky"
    bvh_prefix = f"{side_prefix}{title}"
    trc_prefix = {
        "thumb": "FingerThumb",
        "index": "FingerIndex",
        "middle": "FingerMiddle",
        "ring": "FingerRing",
        "pinky": "FingerPinky",
    }[finger]
    return (
        (f"{bvh_prefix}0", f"{trc_prefix}1"),
        (f"{bvh_prefix}1", f"{trc_prefix}2"),
        (f"{bvh_prefix}2", f"{trc_prefix}3"),
    )


def target_mano_joints_from_track(
    track: GTMarkerTrack,
    template: ManoTemplate,
    side: str,
    positions: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float]:
    origin, basis, hand_scale = hand_pose_from_markers(track.marker_names, positions)
    target = template.joints * hand_scale @ basis.T + origin[None, :]
    target[0] = origin
    for finger, mano_indexes in MANO_CHAIN_NAMES.items():
        for mano_index, candidate_names in zip(mano_indexes, finger_joint_names(side, finger)):
            value = first_named_position(track.marker_names, positions, candidate_names)
            if value is not None and np.isfinite(value).all():
                target[mano_index] = value
    return target.astype(np.float32), basis.astype(np.float32), hand_scale


def posed_vertices_from_joints(
    template: ManoTemplate,
    target_joints: np.ndarray,
    root_basis: np.ndarray,
    root_scale: float,
) -> np.ndarray:
    transforms_r = np.zeros((len(template.joints), 3, 3), dtype=np.float32)
    transforms_t = np.zeros((len(template.joints), 3), dtype=np.float32)
    for joint_index, parent in enumerate(template.parents):
        if parent < 0:
            rotation = root_basis
        else:
            rest_vec = (template.joints[joint_index] - template.joints[parent]) * root_scale
            target_vec = target_joints[joint_index] - target_joints[parent]
            rotation = rotation_between_vectors(rest_vec, target_vec)
        transforms_r[joint_index] = rotation
        transforms_t[joint_index] = target_joints[joint_index] - rotation @ (
            template.joints[joint_index] * root_scale
        )

    rest_vertices = template.vertices * root_scale
    posed = np.zeros_like(rest_vertices, dtype=np.float32)
    for joint_index in range(len(template.joints)):
        transformed = (
            rest_vertices @ transforms_r[joint_index].T + transforms_t[joint_index][None, :]
        )
        posed += transformed * template.weights[:, joint_index : joint_index + 1]
    return posed.astype(np.float32)


def mano_mesh_from_marker_track(
    track: GTMarkerTrack, model_dir: Path, side: str
) -> GTManoMeshTrack:
    template = normalize_mano_template(load_mano_template(model_dir, side))

    frames: list[np.ndarray] = []
    for positions in track.positions:
        target_joints, root_basis, root_scale = target_mano_joints_from_track(
            track, template, side, positions
        )
        frames.append(posed_vertices_from_joints(template, target_joints, root_basis, root_scale))

    label = f"{track.label}_{side}_mano_mesh"
    color = (0.55, 0.72, 1.0) if side == "left" else (1.0, 0.58, 0.48)
    return GTManoMeshTrack(
        label=label,
        entity=f"{GT_MESH_ENTITY}/{track.source}/{label}",
        source=track.source,
        timestamps_ns=track.timestamps_ns,
        vertices=np.asarray(frames, dtype=np.float32),
        faces=template.faces,
        color=color,
    )


def discover_gt_files(gt_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in gt_dir.rglob("*")
        if path.is_file()
        and not any(part.lower() in GT_DIRECTORY_IGNORES for part in path.relative_to(gt_dir).parts)
        and path.suffix.lower() in GT_FILE_SUFFIXES
    )


def discover_gt_dir(session_dir: Path, explicit_gt_dir: Path | None) -> Path | None:
    if explicit_gt_dir is not None:
        if not explicit_gt_dir.exists():
            raise FileNotFoundError(explicit_gt_dir)
        return explicit_gt_dir

    candidate_roots: set[Path] = set()
    for path in session_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in GT_FILE_SUFFIXES:
            continue
        relative = path.relative_to(session_dir)
        if any(part.lower() in GT_DIRECTORY_IGNORES for part in relative.parts):
            continue
        root = session_dir if len(relative.parts) == 1 else session_dir / relative.parts[0]
        if root.name.lower().startswith("robowrist_"):
            continue
        candidate_roots.add(root)

    if not candidate_roots:
        return None

    preferred = sorted(
        root
        for root in candidate_roots
        if root.name.lower().startswith("test") or root.name.lower() == "nokov"
    )
    choices = preferred or sorted(candidate_roots)
    if len(choices) > 1:
        raise ValueError(f"multiple GT data directories found; pass --gt-dir explicitly: {choices}")
    return choices[0]


def timestamps_from_trc(path: Path) -> np.ndarray | None:
    if not path.exists():
        return None
    timestamps_ns, _, _ = parse_trc(path, 1.0, None, marker_limit=1)
    return timestamps_ns


def first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def find_gt_by_patterns(gt_dir: Path, patterns: Iterable[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(path for path in gt_dir.glob(pattern) if path.is_file())
        if matches:
            return matches[0]
    return None


def safe_entity_label(path: Path) -> str:
    label = re.sub(r"[^A-Za-z0-9_]+", "_", path.stem).strip("_")
    return label or "track"


def infer_gt_side(path: Path) -> str | None:
    lowered = path.stem.lower()
    left_tokens = ("lhand", "left", "_l_", "-l-", "左")
    right_tokens = ("rhand", "right", "_r_", "-r-", "右")
    if any(token in lowered for token in left_tokens):
        return "left"
    if any(token in lowered for token in right_tokens):
        return "right"
    return None


def gt_file_visual_style(path: Path) -> tuple[tuple[int, int, int], float, bool]:
    lowered = path.stem.lower()
    if "tracker" in lowered or "head" in lowered or path.suffix.lower() in {".csv", ".xrs"}:
        return (80, 200, 255), 0.035, False
    if infer_gt_side(path) == "right":
        return (255, 120, 90), 0.014, True
    if infer_gt_side(path) == "left":
        return (255, 210, 80), 0.014, True
    if "hand" in lowered:
        return (170, 130, 255), 0.014, True
    return (255, 210, 80), 0.014, False


def discover_gt_file_sets(gt_dir: Path) -> list[GTFileSet]:
    return gt_file_sets_from_paths(gt_dir, discover_gt_files(gt_dir))


def resolve_gt_file_paths(
    gt_dir: Path, gt_files: Sequence[Path] | None
) -> tuple[list[Path], Path | None]:
    if not gt_files:
        return [], None
    resolved_files: list[Path] = []
    third_person_video: Path | None = None
    for raw_path in gt_files:
        path = raw_path if raw_path.is_absolute() else gt_dir / raw_path
        if not path.exists():
            raise FileNotFoundError(path)
        if path.suffix.lower() == ".mp4":
            third_person_video = path
        else:
            resolved_files.append(path)
    return resolved_files, third_person_video


def gt_file_sets_from_paths(gt_dir: Path, paths: Sequence[Path]) -> list[GTFileSet]:
    suffix_to_field = {".bvh": "bvh", ".trc": "trc", ".csv": "csv", ".xrs": "xrs"}
    suffix_priority = {".bvh": 0, ".trc": 1, ".csv": 2, ".xrs": 3}
    grouped_paths: dict[str, list[Path]] = {}
    for path in paths:
        if path.suffix.lower() not in suffix_to_field:
            continue
        try:
            relative_stem = path.resolve().relative_to(gt_dir.resolve()).with_suffix("")
        except ValueError:
            relative_stem = path.with_suffix("")
        grouped_paths.setdefault(str(relative_stem).casefold(), []).append(path)

    file_sets: list[GTFileSet] = []
    used_labels: dict[str, int] = {}
    ordered_group_keys = sorted(
        grouped_paths,
        key=lambda key: (
            min(suffix_priority[path.suffix.lower()] for path in grouped_paths[key]),
            key,
        ),
    )
    for group_key in ordered_group_keys:
        group = sorted(
            grouped_paths[group_key], key=lambda path: suffix_priority[path.suffix.lower()]
        )
        representative = group[0]
        base_label = safe_entity_label(representative)
        count = used_labels.get(base_label, 0)
        used_labels[base_label] = count + 1
        label = base_label if count == 0 else f"{base_label}_{count + 1}"
        colors, radius, connect_hands = gt_file_visual_style(representative)
        kwargs: dict[str, Any] = {
            "label": label,
            "side": infer_gt_side(representative),
            "colors": colors,
            "radius": radius,
            "connect_hands": connect_hands,
        }
        for path in group:
            kwargs[suffix_to_field[path.suffix.lower()]] = path
        file_sets.append(GTFileSet(**kwargs))
    return file_sets


def append_note(note_parts: list[str], message: str) -> None:
    if message not in note_parts:
        note_parts.append(message)


def append_gt_track(
    track: GTMarkerTrack,
    source_path: Path,
    side: str | None,
    mano_model_dir: Path | None,
    tracks: list[GTMarkerTrack],
    mano_tracks: list[GTManoMeshTrack],
    note_parts: list[str],
) -> None:
    tracks.append(track)
    if mano_model_dir is None or side is None:
        return
    try:
        mano_tracks.append(mano_mesh_from_marker_track(track, mano_model_dir, side))
    except Exception as exc:
        append_note(
            note_parts,
            f"failed to build MANO mesh from {track.source.upper()} {source_path.name}: {exc}",
        )


def load_test_gt_dir(
    gt_dir: Path,
    scale: float,
    bvh_scale: float,
    max_frames: int | None,
    mano_model_dir: Path | None,
    gt_files: Sequence[Path] | None = None,
) -> tuple[tuple[GTMarkerTrack, ...], tuple[GTManoMeshTrack, ...], Path | None, tuple[str, ...]]:
    tracks: list[GTMarkerTrack] = []
    mano_tracks: list[GTManoMeshTrack] = []
    note_parts: list[str] = []
    selected_files, selected_third_person_video = resolve_gt_file_paths(gt_dir, gt_files)
    file_sets = (
        gt_file_sets_from_paths(gt_dir, selected_files)
        if gt_files
        else discover_gt_file_sets(gt_dir)
    )

    for file_set in file_sets:
        if file_set.bvh is not None:
            try:
                bvh_track = load_bvh_track(
                    file_set.bvh,
                    file_set.label,
                    f"{GT_TRACKS_ENTITY}/bvh/{file_set.label}",
                    "bvh",
                    bvh_scale,
                    max_frames,
                    timestamps_ns=timestamps_from_trc(file_set.trc)
                    if file_set.trc is not None
                    else None,
                    colors=file_set.colors,
                    radius=file_set.radius,
                )
            except Exception as exc:
                append_note(note_parts, f"failed to load BVH {file_set.bvh.name}: {exc}")
            else:
                append_gt_track(
                    bvh_track,
                    file_set.bvh,
                    file_set.side,
                    mano_model_dir,
                    tracks,
                    mano_tracks,
                    note_parts,
                )
        if file_set.trc is not None:
            try:
                trc_track = load_marker_track_from_trc(
                    file_set.trc,
                    file_set.label,
                    f"{GT_TRACKS_ENTITY}/trc/{file_set.label}",
                    "trc",
                    scale,
                    max_frames,
                    marker_limit=4 if file_set.label == "camera_trackers" else None,
                    colors=file_set.colors,
                    radius=file_set.radius,
                    connect_hands=file_set.connect_hands,
                )
            except Exception as exc:
                append_note(note_parts, f"failed to load TRC {file_set.trc.name}: {exc}")
            else:
                append_gt_track(
                    trc_track,
                    file_set.trc,
                    file_set.side,
                    mano_model_dir,
                    tracks,
                    mano_tracks,
                    note_parts,
                )
        if file_set.csv is not None:
            try:
                csv_track = load_csv_track(
                    file_set.csv,
                    file_set.label,
                    f"{GT_TRACKS_ENTITY}/csv/{file_set.label}",
                    "csv",
                    scale,
                    max_frames,
                    colors=file_set.colors,
                    radius=file_set.radius,
                )
            except Exception as exc:
                append_note(note_parts, f"failed to load CSV {file_set.csv.name}: {exc}")
            else:
                append_gt_track(
                    csv_track,
                    file_set.csv,
                    file_set.side,
                    mano_model_dir,
                    tracks,
                    mano_tracks,
                    note_parts,
                )
        if file_set.xrs is not None:
            try:
                xrs_track = load_xrs_track(
                    file_set.xrs,
                    file_set.label,
                    f"{GT_TRACKS_ENTITY}/xrs/{file_set.label}",
                    "xrs",
                    scale,
                    max_frames,
                    colors=file_set.colors,
                    radius=file_set.radius,
                )
            except Exception as exc:
                append_note(note_parts, f"failed to load XRS {file_set.xrs.name}: {exc}")
            else:
                append_gt_track(
                    xrs_track,
                    file_set.xrs,
                    file_set.side,
                    mano_model_dir,
                    tracks,
                    mano_tracks,
                    note_parts,
                )

    prefix = gt_dir.name
    third_person_video = selected_third_person_video or find_gt_by_patterns(
        gt_dir, (f"{prefix}-1.mp4", "*.mp4")
    )
    return (
        tuple(tracks),
        tuple(mano_tracks),
        third_person_video
        if third_person_video is not None and third_person_video.exists()
        else None,
        tuple(note_parts),
    )


def first_gt_timestamp(gt_config: GTConfig) -> int | None:
    candidates: list[int] = []
    if gt_config.skeleton is not None and len(gt_config.skeleton.timestamps_ns):
        candidates.append(int(gt_config.skeleton.timestamps_ns[0]))
    if gt_config.mesh is not None and len(gt_config.mesh.timestamps_ns):
        candidates.append(int(gt_config.mesh.timestamps_ns[0]))
    for track in gt_config.marker_tracks:
        if len(track.timestamps_ns):
            candidates.append(int(track.timestamps_ns[0]))
    for track in gt_config.mano_mesh_tracks:
        if len(track.timestamps_ns):
            candidates.append(int(track.timestamps_ns[0]))
    return min(candidates) if candidates else None


def offset_gt_config(gt_config: GTConfig, offset_ns: int) -> GTConfig:
    if offset_ns == 0:
        return gt_config
    skeleton = (
        replace(gt_config.skeleton, timestamps_ns=gt_config.skeleton.timestamps_ns + offset_ns)
        if gt_config.skeleton is not None
        else None
    )
    mesh = (
        replace(gt_config.mesh, timestamps_ns=gt_config.mesh.timestamps_ns + offset_ns)
        if gt_config.mesh is not None
        else None
    )
    marker_tracks = tuple(
        replace(track, timestamps_ns=track.timestamps_ns + offset_ns)
        for track in gt_config.marker_tracks
    )
    mano_mesh_tracks = tuple(
        replace(track, timestamps_ns=track.timestamps_ns + offset_ns)
        for track in gt_config.mano_mesh_tracks
    )
    third_person_start_ns = (
        gt_config.third_person_start_ns + offset_ns
        if gt_config.third_person_start_ns is not None
        else None
    )
    return replace(
        gt_config,
        skeleton=skeleton,
        mesh=mesh,
        marker_tracks=marker_tracks,
        mano_mesh_tracks=mano_mesh_tracks,
        third_person_start_ns=third_person_start_ns,
    )


def robocap_start_timestamp_ns(session_dir: Path, config: SessionConfig) -> int | None:
    for label in VIDEO_SLOT_ORDER:
        spec = config.videos.get(label)
        if spec is None:
            continue
        try:
            return metadata_comment_us(session_dir / spec.relative_path) * 1_000
        except (ValueError, KeyError, subprocess.CalledProcessError, OSError):
            continue
    return None


def maybe_align_gt_to_robocap(
    session_dir: Path, config: SessionConfig, gt_config: GTConfig | None, enabled: bool
) -> GTConfig | None:
    if gt_config is None or not enabled:
        return gt_config
    gt_start_ns = first_gt_timestamp(gt_config)
    capture_start_ns = robocap_start_timestamp_ns(session_dir, config)
    if gt_start_ns is None or capture_start_ns is None:
        return gt_config
    return offset_gt_config(gt_config, capture_start_ns - gt_start_ns)


def maybe_proxy_gt_video(
    gt_config: GTConfig | None,
    artifact_paths: ArtifactPaths,
    use_proxy: bool,
    proxy_height: int,
    proxy_crf: int,
    proxy_bitrate: str,
    ffmpeg: str,
) -> GTConfig | None:
    if gt_config is None or gt_config.third_person_video is None or not use_proxy:
        return gt_config
    proxy_path = make_proxy_video(
        gt_config.third_person_video,
        artifact_paths.proxy_dir / "gt",
        proxy_height,
        proxy_crf,
        proxy_bitrate,
        ffmpeg,
    )
    return replace(gt_config, third_person_video=proxy_path)


def load_gt_config(args: argparse.Namespace, session_dir: Path) -> GTConfig | None:
    gt_dir = discover_gt_dir(session_dir, args.gt_dir)
    marker_tracks: tuple[GTMarkerTrack, ...] = ()
    mano_mesh_tracks: tuple[GTManoMeshTrack, ...] = ()
    discovered_third_person_video: Path | None = None
    note_parts: list[str] = []
    view_up_axis: str | None = None
    if gt_dir is not None:
        selected_gt_paths, _ = resolve_gt_file_paths(gt_dir, args.gt_file)
        view_up_axis = infer_nokov_view_up_axis(selected_gt_paths or discover_gt_files(gt_dir))
        marker_tracks, mano_mesh_tracks, discovered_third_person_video, gt_note_parts = (
            load_test_gt_dir(
                gt_dir,
                args.gt_coordinate_scale,
                args.bvh_coordinate_scale,
                args.gt_max_frames,
                args.mano_model_dir if args.retarget_model == "mano" else None,
                args.gt_file,
            )
        )
        note_parts.extend(gt_note_parts)
        if args.retarget_model in {"smpl", "smplh"}:
            note_parts.append(
                f"{args.retarget_model.upper()} retargeting is selectable but not implemented yet; GT skeleton/rigid tracks were loaded without mesh retargeting."
            )

    third_person_video = args.gt_third_person_video or discovered_third_person_video

    skeleton = (
        load_skeleton_track(
            args.gt_skeleton, args.gt_coordinate_scale, args.gt_time_offset_ns, args.gt_max_frames
        )
        if args.gt_skeleton is not None
        else None
    )
    mesh = (
        load_mesh_track(
            args.gt_mesh,
            args.gt_coordinate_scale,
            args.gt_time_offset_ns,
            args.gt_max_frames,
            skeleton.timestamps_ns if skeleton is not None else None,
        )
        if args.gt_mesh is not None
        else None
    )
    if gt_dir is None:
        note_parts.append("no GT data directory discovered")
    if skeleton is None and not marker_tracks:
        note_parts.append("no GT skeleton/marker tracks discovered")
    if mesh is None and not mano_mesh_tracks:
        note_parts.append("no GT mesh/MANO tracks discovered")
    if third_person_video is None:
        note_parts.append("no GT third-person video discovered")
    note = "\n".join(note_parts) if note_parts else None
    if third_person_video is not None and not third_person_video.exists():
        raise FileNotFoundError(third_person_video)
    first_timestamp_ns = first_gt_timestamp(
        GTConfig(
            skeleton=skeleton,
            mesh=mesh,
            marker_tracks=marker_tracks,
            mano_mesh_tracks=mano_mesh_tracks,
            third_person_video=third_person_video,
            third_person_start_ns=None,
            time_offset_ns=args.gt_time_offset_ns,
            note=note,
            view_up_axis=view_up_axis,
        )
    )
    return GTConfig(
        skeleton=skeleton,
        mesh=mesh,
        marker_tracks=marker_tracks,
        mano_mesh_tracks=mano_mesh_tracks,
        third_person_video=third_person_video,
        third_person_start_ns=first_timestamp_ns,
        time_offset_ns=args.gt_time_offset_ns,
        note=note,
        view_up_axis=view_up_axis,
    )


def skeleton_bone_strips(joints: np.ndarray, parents: Sequence[int]) -> list[np.ndarray]:
    strips: list[np.ndarray] = []
    for child, parent in enumerate(parents):
        if parent < 0 or child >= len(joints) or parent >= len(joints):
            continue
        strips.append(np.stack([joints[parent], joints[child]], axis=0))
    return strips


def log_gt_skeleton(
    track: GTSkeletonTrack, capture_window: TimeWindow | None, timeline: TimelineContext
) -> None:
    for source_frame, (timestamp_ns, joints) in enumerate(zip(track.timestamps_ns, track.joints)):
        if capture_window is not None and not (
            capture_window.start_ns <= timestamp_ns <= capture_window.end_ns
        ):
            continue
        frame_index = (
            timeline.gt_frame(source_frame) if timeline.alignment_mode == "frame" else None
        )
        timeline.set_time(int(timestamp_ns), frame_index)
        rr.log(
            f"{GT_SKELETON_ENTITY}/joints",
            rr.Points3D(joints, radii=0.018, colors=[255, 210, 80]),
        )
        rr.log(
            f"{GT_SKELETON_ENTITY}/bones",
            rr.LineStrips3D(
                skeleton_bone_strips(joints, track.parents), radii=0.01, colors=[60, 180, 255]
            ),
        )


def log_gt_mesh(
    track: GTMeshTrack, capture_window: TimeWindow | None, timeline: TimelineContext
) -> None:
    for source_frame, (timestamp_ns, vertices) in enumerate(
        zip(track.timestamps_ns, track.vertices)
    ):
        if capture_window is not None and not (
            capture_window.start_ns <= timestamp_ns <= capture_window.end_ns
        ):
            continue
        frame_index = (
            timeline.gt_frame(source_frame) if timeline.alignment_mode == "frame" else None
        )
        timeline.set_time(int(timestamp_ns), frame_index)
        rr.log(
            GT_MESH_ENTITY,
            rr.Mesh3D(
                vertex_positions=vertices,
                triangle_indices=track.faces,
                albedo_factor=[0.78, 0.78, 0.82],
            ),
        )


def log_gt_mano_mesh(
    track: GTManoMeshTrack, capture_window: TimeWindow | None, timeline: TimelineContext
) -> None:
    for source_frame, (timestamp_ns, vertices) in enumerate(
        zip(track.timestamps_ns, track.vertices)
    ):
        if capture_window is not None and not (
            capture_window.start_ns <= timestamp_ns <= capture_window.end_ns
        ):
            continue
        frame_index = (
            timeline.gt_frame(source_frame) if timeline.alignment_mode == "frame" else None
        )
        timeline.set_time(int(timestamp_ns), frame_index)
        rr.log(
            track.entity,
            rr.Mesh3D(
                vertex_positions=vertices,
                triangle_indices=track.faces,
                albedo_factor=list(track.color),
            ),
        )


def marker_connection_strips(
    positions: np.ndarray, connections: Sequence[tuple[int, int]]
) -> list[np.ndarray]:
    return [
        np.stack([positions[first], positions[second]], axis=0)
        for first, second in connections
        if first < len(positions) and second < len(positions)
    ]


def log_gt_marker_track(
    track: GTMarkerTrack, capture_window: TimeWindow | None, timeline: TimelineContext
) -> None:
    rr.log(f"{track.entity}/labels", rr.TextDocument(", ".join(track.marker_names)), static=True)
    for source_frame, (timestamp_ns, positions) in enumerate(
        zip(track.timestamps_ns, track.positions)
    ):
        if capture_window is not None and not (
            capture_window.start_ns <= timestamp_ns <= capture_window.end_ns
        ):
            continue
        frame_index = (
            timeline.gt_frame(source_frame) if timeline.alignment_mode == "frame" else None
        )
        timeline.set_time(int(timestamp_ns), frame_index)
        rr.log(
            f"{track.entity}/points",
            rr.Points3D(positions, radii=track.radius, colors=list(track.colors)),
        )
        if track.connections:
            rr.log(
                f"{track.entity}/connections",
                rr.LineStrips3D(
                    marker_connection_strips(positions, track.connections),
                    radii=track.radius * 0.45,
                    colors=list(track.colors),
                ),
            )


def log_gt_third_person_video(
    video_path: Path,
    time_offset_ns: int,
    start_timestamp_ns: int | None,
    capture_window: TimeWindow | None,
    timeline: TimelineContext,
) -> None:
    video_asset = rr.AssetVideo(path=video_path)
    rr.log(GT_THIRD_PERSON_VIDEO_ENTITY, video_asset, static=True)

    frame_timestamps_ns = np.asarray(video_asset.read_frame_timestamps_nanos(), dtype=np.int64)
    if start_timestamp_ns is None:
        try:
            start_timestamp_ns = metadata_comment_us(video_path) * 1_000
        except (ValueError, KeyError, subprocess.CalledProcessError, OSError):
            start_timestamp_ns = 0
    capture_timestamps_ns = start_timestamp_ns + time_offset_ns + frame_timestamps_ns
    mask = time_mask(capture_timestamps_ns, capture_window)
    frame_timestamps_ns = frame_timestamps_ns[mask]
    capture_timestamps_ns = capture_timestamps_ns[mask]
    if len(capture_timestamps_ns) == 0:
        return
    rr.send_columns(
        GT_THIRD_PERSON_VIDEO_ENTITY,
        indexes=timeline.indexes(capture_timestamps_ns),
        columns=rr.VideoFrameReference.columns_nanos(frame_timestamps_ns),
    )


def log_gt(
    gt_config: GTConfig | None,
    capture_window: TimeWindow | None,
    timeline: TimelineContext,
) -> None:
    if gt_config is None:
        rr.log(GT_NOTE_ENTITY, rr.TextDocument("no GT data discovered"), static=True)
        return
    view_coordinates = {
        "x": rr.ViewCoordinates.RIGHT_HAND_X_UP,
        "y": rr.ViewCoordinates.RIGHT_HAND_Y_UP,
        "z": rr.ViewCoordinates.RIGHT_HAND_Z_UP,
    }.get(gt_config.view_up_axis or "")
    if view_coordinates is not None:
        rr.log(GT_TRACKS_ENTITY, view_coordinates, static=True)
        rr.log(GT_MESH_ENTITY, view_coordinates, static=True)
    if gt_config.skeleton is not None:
        log_gt_skeleton(gt_config.skeleton, capture_window, timeline)
    if gt_config.mesh is not None:
        log_gt_mesh(gt_config.mesh, capture_window, timeline)
    for track in gt_config.mano_mesh_tracks:
        log_gt_mano_mesh(track, capture_window, timeline)
    for track in gt_config.marker_tracks:
        log_gt_marker_track(track, capture_window, timeline)
    if gt_config.third_person_video is not None:
        log_gt_third_person_video(
            gt_config.third_person_video,
            gt_config.time_offset_ns,
            gt_config.third_person_start_ns,
            capture_window,
            timeline,
        )
    rr.log(
        GT_NOTE_ENTITY,
        rr.TextDocument(
            gt_config.note or "GT data loaded; unavailable tabs have no source for this session"
        ),
        static=True,
    )


def video_or_note_view(config: SessionConfig, label: str) -> rrb.View:
    if label in config.videos:
        spec = config.videos[label]
        return rrb.Spatial2DView(name=label, origin=spec.entity)
    return rrb.TextDocumentView(name=label, origin=config.notes[label].origin)


def signal_or_note_view(config: SessionConfig, label: str) -> rrb.View:
    if label in config.signals:
        spec = config.signals[label]
        return rrb.TimeSeriesView(name=label, origin=spec.origin)
    return rrb.TextDocumentView(name=label, origin=config.notes[label].origin)


def available_video_view(config: SessionConfig, label: str) -> rrb.View | None:
    if label not in config.videos:
        return None
    spec = config.videos[label]
    return rrb.Spatial2DView(name=label, origin=spec.entity)


def available_signal_view(config: SessionConfig, label: str) -> rrb.View | None:
    if label not in config.signals:
        return None
    spec = config.signals[label]
    return rrb.TimeSeriesView(name=label, origin=spec.origin)


def non_empty_grid(
    name: str, views: Sequence[rrb.View], grid_columns: int = 1
) -> rrb.ContainerLike:
    if not views:
        return rrb.TextDocumentView(name=name, origin=GT_NOTE_ENTITY)
    return rrb.Grid(*views, grid_columns=grid_columns, name=name)


def ordered_gt_sources(sources: Iterable[str]) -> list[str]:
    priority = {"bvh": 0, "trc": 1, "csv": 2, "xrs": 3}
    return sorted(set(sources), key=lambda source: (priority.get(source, 99), source))


def gt_marker_sources(gt_config: GTConfig | None) -> list[str]:
    if gt_config is None:
        return []
    return ordered_gt_sources(track.source for track in gt_config.marker_tracks)


def gt_mano_sources(gt_config: GTConfig | None) -> list[str]:
    if gt_config is None:
        return []
    return ordered_gt_sources(track.source for track in gt_config.mano_mesh_tracks)


def gt_skeleton_source_view(source: str) -> rrb.View:
    return rrb.Spatial3DView(name=source.upper(), origin=f"{GT_TRACKS_ENTITY}/{source}")


def gt_mesh_source_view(source: str) -> rrb.View:
    return rrb.Spatial3DView(name=source.upper(), origin=f"{GT_MESH_ENTITY}/{source}")


def gt_skeleton_tabs(gt_config: GTConfig | None) -> rrb.Tabs | None:
    sources = gt_marker_sources(gt_config)
    if not sources:
        return None
    return rrb.Tabs(
        *(gt_skeleton_source_view(source) for source in sources),
        active_tab=sources[0].upper(),
        name="GT skeleton",
    )


def gt_mesh_tabs(gt_config: GTConfig | None) -> rrb.Tabs | None:
    sources = gt_mano_sources(gt_config)
    if not sources:
        return None
    return rrb.Tabs(
        *(gt_mesh_source_view(source) for source in sources),
        active_tab=sources[0].upper(),
        name="GT MANO mesh",
    )


def gt_third_person_video_view(gt_config: GTConfig | None) -> rrb.View | None:
    if gt_config is not None and gt_config.third_person_video is not None:
        return rrb.Spatial2DView(name="GT third-person video", origin=GT_THIRD_PERSON_VIDEO_ENTITY)
    return None


def robocap_sensors_container(config: SessionConfig) -> rrb.ContainerLike | None:
    mag_view = available_signal_view(config, "middle_mag")
    imu_rows: list[rrb.ContainerLike] = []
    for labels in (
        ("left_robocap_acc", "left_robocap_gyro"),
        ("right_robocap_acc", "right_robocap_gyro"),
    ):
        views = [
            view for label in labels if (view := available_signal_view(config, label)) is not None
        ]
        if views:
            imu_rows.append(rrb.Horizontal(*views, column_shares=[1.0 for _ in views]))

    columns: list[rrb.ContainerLike] = []
    column_shares: list[float] = []
    if mag_view is not None:
        columns.append(mag_view)
        column_shares.append(1.0)
    if imu_rows:
        columns.append(rrb.Vertical(*imu_rows, row_shares=[1.0 for _ in imu_rows]))
        column_shares.append(2.0)
    if not columns:
        return None
    return rrb.Horizontal(*columns, column_shares=column_shares, name="Robocap sensors only")


def all_signals_container(config: SessionConfig) -> rrb.ContainerLike | None:
    views = [
        view
        for label in SIGNAL_SLOT_ORDER
        if (view := available_signal_view(config, label)) is not None
    ]
    if not views:
        return None
    return rrb.Grid(*views, grid_columns=3, name="Signals")


def display_videos_container(config: SessionConfig) -> rrb.ContainerLike:
    columns: list[rrb.ContainerLike] = []
    for name, labels in (
        ("left / right", ("left", "right")),
        ("left eye / right eye", ("left_eye", "right_eye")),
        ("left front / right front", ("left_front", "right_front")),
    ):
        views = [
            view for label in labels if (view := available_video_view(config, label)) is not None
        ]
        if views:
            columns.append(rrb.Vertical(*views, row_shares=[1.0 for _ in views], name=name))
    if not columns:
        return rrb.TextDocumentView(name="Robocap videos", origin=GT_NOTE_ENTITY)
    return rrb.Horizontal(*columns, column_shares=[1.0 for _ in columns], name="Robocap videos")


def gt_overview_container(gt_config: GTConfig | None) -> rrb.ContainerLike | None:
    candidates = (
        (gt_skeleton_tabs(gt_config), 1.0),
        (gt_mesh_tabs(gt_config), 1.0),
        (gt_third_person_video_view(gt_config), 1.25),
    )
    views = [view for view, _share in candidates if view is not None]
    column_shares = [share for view, share in candidates if view is not None]
    if not views:
        return None
    if len(views) == 1:
        return views[0]
    return rrb.Horizontal(*views, column_shares=column_shares, name="GT")


def build_display_blueprint(
    config: SessionConfig,
    gt_config: GTConfig | None = None,
    timeline_name: str = "capture_time",
) -> rrb.Blueprint:
    rows: list[rrb.ContainerLike] = [display_videos_container(config)]
    row_shares = [2.4]
    sensor_overview = robocap_sensors_container(config)
    if sensor_overview is not None:
        rows.append(sensor_overview)
        row_shares.append(1.6)
    gt_overview = gt_overview_container(gt_config)
    if gt_overview is not None:
        rows.append(gt_overview)
        row_shares.append(2.4)
    return rrb.Blueprint(
        rrb.TimePanel(timeline=timeline_name),
        rrb.Vertical(*rows, row_shares=row_shares),
        collapse_panels=True,
    )


def build_blueprint(
    config: SessionConfig,
    gt_config: GTConfig | None = None,
    preset: str = "default",
    timeline_name: str = "capture_time",
) -> rrb.Blueprint:
    if preset == "display":
        return build_display_blueprint(config, gt_config, timeline_name)

    rows: list[rrb.ContainerLike] = [
        non_empty_grid(
            "Videos",
            [
                view
                for label in VIDEO_SLOT_ORDER
                if (view := available_video_view(config, label)) is not None
            ],
            grid_columns=4,
        )
    ]
    row_shares = [2.4]
    signals_container = all_signals_container(config)
    if signals_container is not None:
        rows.append(signals_container)
        row_shares.append(3.6)
    gt_overview = gt_overview_container(gt_config)
    if gt_overview is not None:
        rows.append(gt_overview)
        row_shares.append(2.4)

    return rrb.Blueprint(
        rrb.TimePanel(timeline=timeline_name),
        rrb.Vertical(*rows, row_shares=row_shares),
        collapse_panels=True,
    )


def print_layout(config: SessionConfig) -> None:
    print("layout:")
    print(
        f"  videos: {', '.join(label for label in VIDEO_SLOT_ORDER if label in config.videos) or 'none'}"
    )
    print(
        f"  signals: {', '.join(label for label in SIGNAL_SLOT_ORDER if label in config.signals) or 'none'}"
    )
    print("  structure: Vertical(available Videos, available Signals, GT)")
    missing_labels = sorted(config.notes)
    if missing_labels:
        print(f"  no-data slots: {', '.join(missing_labels)}")


def inspect_session(session_dir: Path, config: SessionConfig) -> None:
    print(f"session: {session_dir.resolve()}")
    print(f"segment: {config.segment_name}")
    print_layout(config)
    for label in VIDEO_SLOT_ORDER:
        if label not in config.videos:
            continue
        spec = config.videos[label]
        path = session_dir / spec.relative_path
        data = probe_video(path)
        stream = data["streams"][0]
        fmt = data["format"]
        comment = fmt.get("tags", {}).get("comment")
        size_mb = int(fmt["size"]) / 1024 / 1024
        print(
            f"video {spec.entity}: {stream['width']}x{stream['height']} "
            f"{float(fmt['duration']):.3f}s {stream.get('nb_frames')} frames "
            f"{size_mb:.1f} MB comment={comment}"
        )
    for label in SIGNAL_SLOT_ORDER:
        if label not in config.signals:
            continue
        spec = config.signals[label]
        with sqlite3.connect(session_dir / spec.relative_path) as con:
            count = con.execute(f"select count(*) from {spec.table}").fetchone()[0]
            min_ts, max_ts = con.execute(
                f"select min(timestamp), max(timestamp) from {spec.table}"
            ).fetchone()
        print(f"signal {spec.origin}: {count} rows {min_ts}..{max_ts} ns")
    for label in sorted(config.notes):
        note = config.notes[label]
        print(f"note {note.origin}: {note.text}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a dual-hands Rerun recording from one robocap/robowrist session directory.",
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        help="Path to the session directory that contains robocap files and left/right robowrist subdirectories.",
    )
    parser.add_argument(
        "--save",
        type=Path,
        default=None,
        help="Optional explicit output .rrd path. If omitted, write to <session-dir>/_artifacts/<segment>/inspection/.",
    )
    parser.add_argument(
        "--spawn",
        action="store_true",
        help="Open the native Rerun viewer after logging the recording.",
    )
    parser.add_argument(
        "--use-proxy",
        action="store_true",
        help="Generate and reuse compressed proxy MP4 files instead of using the original videos directly.",
    )
    parser.add_argument(
        "--proxy-height",
        type=int,
        default=540,
        help="Target proxy video height in pixels. Width is scaled automatically.",
    )
    parser.add_argument(
        "--proxy-crf",
        type=int,
        default=28,
        help="CRF used when ffmpeg has libx264. Lower means larger files and higher quality.",
    )
    parser.add_argument(
        "--proxy-bitrate",
        default="1400k",
        help="Fallback bitrate used when ffmpeg only has libopenh264.",
    )
    parser.add_argument(
        "--ffmpeg",
        default=None,
        help="Optional explicit ffmpeg executable path. If omitted, auto-pick one from PATH.",
    )
    parser.add_argument(
        "--segment",
        default=None,
        help="Target segment name such as segment1 or segment2. If omitted, auto-detect from discovered files.",
    )
    parser.add_argument(
        "--max-sensor-points",
        type=int,
        default=6000,
        help="Maximum number of rows per sensor stream after uniform downsampling.",
    )
    parser.add_argument(
        "--no-trim-to-common-time",
        action="store_true",
        help="Keep the full union of all stream timestamps instead of cropping to the common valid capture_time range.",
    )
    parser.add_argument(
        "--robocap-start-frame",
        type=int,
        default=None,
        help=(
            "First reference Robocap video frame to export (0-based, inclusive). "
            "Must be used with --robocap-end-frame."
        ),
    )
    parser.add_argument(
        "--robocap-end-frame",
        type=int,
        default=None,
        help=(
            "Last reference Robocap video frame to export (0-based, inclusive). "
            "Must be used with --robocap-start-frame."
        ),
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print discovered files, layout, and artifact paths before logging.",
    )
    parser.add_argument(
        "--layout-only",
        action="store_true",
        help="Only print the resolved layout and discovery result. Do not write an .rrd.",
    )
    parser.add_argument(
        "--blueprint-preset",
        choices=("default", "display"),
        default="default",
        help="Blueprint layout preset. display uses 3 video columns, robocap sensors only, and BVH/CSV/TRC skeleton row.",
    )
    parser.add_argument(
        "--gt-skeleton",
        type=Path,
        default=None,
        help=(
            "Optional GT skeleton trajectory. Supported formats: npz/json with joints [T,J,3] plus optional "
            "capture_time_ns/timestamps_ns/timestamps, joint_names, parents; or CSV with timestamp,joint,x,y,z."
        ),
    )
    parser.add_argument(
        "--gt-dir",
        type=Path,
        default=None,
        help=(
            "Optional GT export folder. If omitted, a single session child folder named test*, nokov, or another "
            "directory containing GT files is auto-discovered. "
            "For the current NOKOV export this reads *-1.mp4, *-Tracker0.trc, *-LHand.trc, and *-RHand.trc."
        ),
    )
    parser.add_argument(
        "--gt-file",
        type=Path,
        action="append",
        default=None,
        help="Explicit GT file to include. May be repeated. Relative paths are resolved under --gt-dir.",
    )
    parser.add_argument(
        "--gt-mesh",
        type=Path,
        default=None,
        help=(
            "Optional GT mesh trajectory. Supported formats: npz/json with vertices [T,V,3] or [V,3], "
            "faces [F,3], and optional capture_time_ns/timestamps_ns/timestamps."
        ),
    )
    parser.add_argument(
        "--gt-third-person-video",
        type=Path,
        default=None,
        help=(
            "Optional third-person/reference video shown as the third GT column. If the MP4 has a comment "
            "metadata timestamp, it is used as capture start; otherwise frame timestamps start at zero plus "
            "--gt-time-offset-ns."
        ),
    )
    parser.add_argument(
        "--mano-model-dir",
        type=Path,
        default=Path("Z:/MODELS/hand_models/mano"),
        help="Directory containing MANO_LEFT.pkl and MANO_RIGHT.pkl for GT hand mesh generation.",
    )
    parser.add_argument(
        "--retarget-model",
        choices=("none", "mano", "smpl", "smplh"),
        default="mano",
        help="Target model for marker-to-mesh retargeting. Currently MANO is implemented; SMPL/SMPLH produce an explicit note.",
    )
    parser.add_argument(
        "--no-mano-mesh",
        dest="retarget_model",
        action="store_const",
        const="none",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--gt-coordinate-scale",
        type=float,
        default=0.001,
        help="Scale factor applied to GT joint and mesh coordinates. Defaults to 0.001 for NOKOV millimeters to meters.",
    )
    parser.add_argument(
        "--bvh-coordinate-scale",
        type=float,
        default=0.01,
        help="Scale factor applied to BVH hand skeleton coordinates. Current NOKOV BVH exports are 1/10 of TRC mm, so 0.01 maps to meters.",
    )
    parser.add_argument(
        "--gt-time-offset-ns",
        type=int,
        default=0,
        help="Constant nanosecond offset added to GT timestamps before logging on the capture_time axis.",
    )
    parser.add_argument(
        "--no-gt-align-to-robocap",
        action="store_true",
        help="Do not shift GT/TRC timestamps so their first frame starts at the first robocap video capture timestamp.",
    )
    parser.add_argument(
        "--gt-alignment-mode",
        choices=("time", "frame"),
        default="time",
        help="GT alignment mode. time keeps source timestamps shifted to robocap start; frame forces GT ratio*N frames onto reference video frame N.",
    )
    parser.add_argument(
        "--gt-frame-reference-video",
        default="left",
        help="Video slot used as frame-alignment reference, e.g. left, right, left_eye.",
    )
    parser.add_argument(
        "--gt-frame-ratio",
        type=float,
        default=None,
        help="Explicit GT/video frame ratio for frame alignment. If omitted, use GT FPS divided by original reference video FPS.",
    )
    parser.add_argument(
        "--gt-video-frame-offset",
        "--gt-frame-offset",
        dest="gt_video_frame_offset",
        type=int,
        default=0,
        help=(
            "Signed Robocap-video-frame offset. Positive advances NOKOV/GT relative to "
            "Robocap video; negative delays it. It is converted to the source script's GT "
            "frame offset with round(offset*ratio). --gt-frame-offset is a compatibility alias."
        ),
    )
    parser.add_argument(
        "--gt-max-frames",
        type=int,
        default=0,
        help="Optional maximum GT frames after uniform downsampling. Default 0 keeps all GT frames.",
    )
    parser.add_argument(
        "--no-robowrist",
        action="store_true",
        help="Do not discover or log robowrist video/sensor streams.",
    )
    parser.add_argument(
        "--no-mag",
        action="store_true",
        help="Do not discover or log Robocap/robowrist magnetometer streams.",
    )
    parser.add_argument(
        "--no-imu",
        action="store_true",
        help="Do not discover or log Robocap/robowrist accelerometer and gyroscope streams.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.gt_max_frames == 0:
        args.gt_max_frames = None
    frame_range = normalize_robocap_frame_range(args.robocap_start_frame, args.robocap_end_frame)
    session_dir = args.session_dir
    config = discover_session(
        session_dir,
        args.segment,
        include_robowrist=not args.no_robowrist,
        include_mag=not args.no_mag,
        include_imu=not args.no_imu,
    )
    gt_config = load_gt_config(args, session_dir)
    gt_config = maybe_align_gt_to_robocap(
        session_dir, config, gt_config, not args.no_gt_align_to_robocap
    )

    if args.layout_only:
        if args.inspect:
            inspect_session(session_dir, config)
        else:
            print_layout(config)
        if gt_config is not None:
            print("gt:")
            print(f"  skeleton: {'yes' if gt_config.skeleton is not None else 'no'}")
            print(f"  mesh: {'yes' if gt_config.mesh is not None else 'no'}")
            print(
                f"  marker_tracks: {', '.join(track.label for track in gt_config.marker_tracks) if gt_config.marker_tracks else 'no'}"
            )
            print(
                f"  mano_mesh_tracks: {', '.join(track.label for track in gt_config.mano_mesh_tracks) if gt_config.mano_mesh_tracks else 'no'}"
            )
            print(
                f"  third_person_video: {gt_config.third_person_video if gt_config.third_person_video is not None else 'no'}"
            )
            if gt_config.note is not None:
                print(f"  note: {gt_config.note}")
        return

    ffmpeg = choose_ffmpeg(args.ffmpeg)
    artifact_paths = build_artifact_paths(session_dir, config)
    gt_config = maybe_proxy_gt_video(
        gt_config,
        artifact_paths,
        args.use_proxy,
        args.proxy_height,
        args.proxy_crf,
        args.proxy_bitrate,
        ffmpeg,
    )
    reference_timestamps = None
    reference_rate_hz = None
    resolved_frame_ratio = None
    if args.gt_alignment_mode == "frame" or frame_range is not None:
        reference_timestamps = reference_video_timestamps_ns(
            session_dir,
            config,
            artifact_paths,
            args.use_proxy,
            args.proxy_height,
            args.proxy_crf,
            args.proxy_bitrate,
            ffmpeg,
            args.gt_frame_reference_video,
        )
    if args.gt_alignment_mode == "frame":
        reference_rate_hz = reference_video_nominal_rate_hz(
            session_dir,
            config,
            artifact_paths,
            args.use_proxy,
            args.proxy_height,
            args.proxy_crf,
            args.proxy_bitrate,
            ffmpeg,
            args.gt_frame_reference_video,
        )
        resolved_frame_ratio = resolve_gt_frame_ratio(
            gt_config, reference_rate_hz, args.gt_frame_ratio
        )
        if reference_timestamps is None or len(reference_timestamps) == 0:
            raise ValueError("Frame alignment requires a reference Robocap video with timestamps.")
        if resolved_frame_ratio is None:
            raise ValueError(
                "Frame alignment could not resolve the GT/Robocap frame ratio. "
                "Run inspect first or provide --gt-frame-ratio explicitly."
            )
        gt_config = with_frame_aligned_gt_timestamps(
            gt_config,
            reference_timestamps,
            reference_rate_hz,
            frame_ratio=resolved_frame_ratio,
            video_frame_offset=args.gt_video_frame_offset,
        )
        print(describe_frame_alignment(resolved_frame_ratio, args.gt_video_frame_offset))
    frame_alignment = (
        FrameAlignment(resolved_frame_ratio, args.gt_video_frame_offset)
        if args.gt_alignment_mode == "frame" and resolved_frame_ratio is not None
        else None
    )
    timeline = TimelineContext(
        alignment_mode=args.gt_alignment_mode,
        reference_timestamps_ns=reference_timestamps,
        frame_alignment=frame_alignment,
    )
    if timeline.alignment_mode == "frame":
        print(
            "Rerun primary timeline: frame (GT/NOKOV frame scale); "
            "capture_time is retained as a secondary timeline."
        )
    time_report_path = write_time_alignment_report(
        session_dir,
        config,
        artifact_paths,
        args.use_proxy,
        args.proxy_height,
        args.proxy_crf,
        args.proxy_bitrate,
        ffmpeg,
        gt_config,
        args.gt_alignment_mode,
    )
    requested_frame_window = robocap_frame_capture_window(reference_timestamps, frame_range)
    common_window = None
    if not args.no_trim_to_common_time:
        common_window = compute_common_capture_window(
            session_dir,
            config,
            artifact_paths,
            args.use_proxy,
            args.proxy_height,
            args.proxy_crf,
            args.proxy_bitrate,
            ffmpeg,
            gt_config,
        )
    capture_window = intersect_time_windows(requested_frame_window, common_window)
    if requested_frame_window is not None and common_window is not None and capture_window is None:
        raise ValueError(
            "The requested Robocap frame range does not overlap the common capture_time window."
        )
    if args.inspect:
        inspect_session(session_dir, config)
        print(f"artifacts: {artifact_paths.root_dir}")
        if frame_range is not None:
            print(
                f"requested Robocap frames: {frame_range[0]}..{frame_range[1]} (0-based, inclusive)"
            )
        if capture_window is not None:
            print(
                "effective capture_time window: "
                f"{capture_window.start_ns}..{capture_window.end_ns} ns "
                f"({(capture_window.end_ns - capture_window.start_ns) / 1e9:.3f} s)"
            )

    blueprint = build_blueprint(
        config,
        gt_config,
        args.blueprint_preset,
        timeline_name=timeline.primary_timeline,
    )
    name_parameters = ExportNameParameters(
        alignment_mode=args.gt_alignment_mode,
        frame_ratio=resolved_frame_ratio,
        video_frame_offset=args.gt_video_frame_offset,
        reference_video=args.gt_frame_reference_video,
        frame_range=frame_range,
        retarget_model=args.retarget_model,
        use_proxy=args.use_proxy,
        proxy_height=args.proxy_height,
        proxy_crf=args.proxy_crf,
        proxy_bitrate=args.proxy_bitrate,
        ffmpeg=args.ffmpeg or "auto",
        blueprint_preset=args.blueprint_preset,
        max_sensor_points=args.max_sensor_points,
        trim_to_common_time=not args.no_trim_to_common_time,
        align_gt_to_robocap=not args.no_gt_align_to_robocap,
        gt_coordinate_scale=args.gt_coordinate_scale,
        bvh_coordinate_scale=args.bvh_coordinate_scale,
        gt_time_offset_ns=args.gt_time_offset_ns,
        gt_max_frames=args.gt_max_frames,
        include_robowrist=not args.no_robowrist,
        include_mag=not args.no_mag,
        include_imu=not args.no_imu,
        gt_dir=str(args.gt_dir) if args.gt_dir is not None else None,
        gt_input_files=tuple(sorted(str(path) for path in (args.gt_file or []))),
        gt_skeleton=str(args.gt_skeleton) if args.gt_skeleton is not None else None,
        gt_mesh=str(args.gt_mesh) if args.gt_mesh is not None else None,
        third_person_input=(
            str(args.gt_third_person_video) if args.gt_third_person_video is not None else None
        ),
        mano_model_dir=str(args.mano_model_dir),
        gt_sources=gt_source_identifiers(gt_config),
        third_person_video=bool(gt_config is not None and gt_config.third_person_video is not None),
    )
    save_path = with_export_parameter_suffix(
        args.save or default_rrd_path(artifact_paths, session_dir, config, args.gt_alignment_mode),
        name_parameters,
    )
    rr.init(
        save_path.stem,
        spawn=args.spawn,
        default_blueprint=blueprint,
    )

    save_path.parent.mkdir(parents=True, exist_ok=True)
    rr.save(save_path, default_blueprint=blueprint)

    for spec in config.videos.values():
        log_video(
            session_dir,
            spec,
            artifact_paths,
            args.use_proxy,
            args.proxy_height,
            args.proxy_crf,
            args.proxy_bitrate,
            ffmpeg,
            capture_window,
            timeline,
        )
    for spec in config.signals.values():
        log_signal(session_dir, spec, args.max_sensor_points, capture_window, timeline)
    for note in config.notes.values():
        log_note(note)
    log_gt(gt_config, capture_window, timeline)

    rr.send_blueprint(blueprint)
    print(f"Rerun dual-hands demo logging complete: {save_path}")
    print(f"Time alignment report: {time_report_path}")
    if capture_window is not None:
        print(
            "Exported capture_time window: "
            f"{capture_window.start_ns}..{capture_window.end_ns} ns "
            f"({(capture_window.end_ns - capture_window.start_ns) / 1e9:.3f} s)"
        )


if __name__ == "__main__":
    main()
