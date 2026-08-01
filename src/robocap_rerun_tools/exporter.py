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
- 已存在的代理视频会被复用，不会重复压缩。
"""

import argparse
import csv
import importlib.util
import inspect
import json
import os
import pickle
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence


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
    -1, 0, 1, 2, 0, 4, 5, 0, 7, 8, 9, 8, 11, 12, 8, 14, 15, 16, 15, 18, 19, 20, 15, 22, 23
)

GT_SKELETON_ENTITY = "gt/skeleton"
GT_MESH_ENTITY = "gt/mesh"
GT_TRACKS_ENTITY = "gt/tracks"
GT_THIRD_PERSON_VIDEO_ENTITY = "gt/third_person_video"
GT_NOTE_ENTITY = "notes/gt"

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
    "left_wrist_mag": ("robowrist_*_left/robowrist_{segment}_mag_left.db", "mag_data", ("mag_x", "mag_y", "mag_z")),
    "left_wrist_acc": ("robowrist_*_left/robowrist_{segment}_imu_left.db", "acc_data", ("x", "y", "z")),
    "left_wrist_gyro": ("robowrist_*_left/robowrist_{segment}_imu_left.db", "gyro_data", ("x", "y", "z")),
    "right_wrist_mag": ("robowrist_*_right/robowrist_{segment}_mag_right.db", "mag_data", ("mag_x", "mag_y", "mag_z")),
    "right_wrist_acc": ("robowrist_*_right/robowrist_{segment}_imu_right.db", "acc_data", ("x", "y", "z")),
    "right_wrist_gyro": ("robowrist_*_right/robowrist_{segment}_imu_right.db", "gyro_data", ("x", "y", "z")),
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


@dataclass(frozen=True)
class GTFileSet:
    label: str
    side: str | None
    bvh: Path | None = None
    trc: Path | None = None
    csv: Path | None = None
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
    return run_json(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(path)])


def metadata_comment_us(path: Path) -> int:
    comment = probe_video(path).get("format", {}).get("tags", {}).get("comment")
    if comment is None:
        raise ValueError(f"missing MP4 metadata comment: {path}")
    return int(comment)


def ffmpeg_has_encoder(ffmpeg: str, encoder: str) -> bool:
    try:
        encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], check=True, text=True, capture_output=True).stdout
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


def make_proxy_video(source: Path, proxy_dir: Path, height: int, crf: int, bitrate: str, ffmpeg: str) -> Path:
    proxy_dir.mkdir(parents=True, exist_ok=True)
    target = proxy_dir / f"{source.stem}_h{height}_crf{crf}.mp4"
    if target.exists() and target.stat().st_size > 0:
        return target

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
            str(target),
        ],
        check=True,
    )
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
    return make_proxy_video(source, artifact_paths.proxy_dir, proxy_height, proxy_crf, proxy_bitrate, ffmpeg)


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
        return np.asarray([], dtype=np.int64), {column: np.asarray([], dtype=np.float64) for column in cols}

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


def discover_session(session_dir: Path, segment_name: str | None = None) -> SessionConfig:
    videos: dict[str, VideoSpec] = {}
    signals: dict[str, SignalSpec] = {}
    notes: dict[str, NoteSpec] = {}

    for label, pattern in VIDEO_PATTERNS.items():
        pattern = pattern.format(segment=segment_name or "*")
        relative_path = find_first_relative_path(session_dir, pattern)
        if relative_path is None:
            notes[label] = NoteSpec(label=label, origin=f"notes/video/{label}", text="no data")
        else:
            videos[label] = VideoSpec(label=label, entity=f"video/{label}", relative_path=relative_path)

    for label, (pattern, table, columns) in SIGNAL_SPECS.items():
        pattern = pattern.format(segment=segment_name or "*")
        relative_path = find_first_relative_path(session_dir, pattern)
        if relative_path is None:
            notes[label] = NoteSpec(label=label, origin=f"notes/signal/{label}", text="no data")
        else:
            signals[label] = SignalSpec(label=label, origin=f"signals/{label}", relative_path=relative_path, table=table, columns=columns)

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


def default_rrd_path(artifact_paths: ArtifactPaths, session_dir: Path, config: SessionConfig, alignment_mode: str) -> Path:
    return artifact_paths.inspection_dir / f"{session_dir.name}_{config.segment_name}_dual_hands_with_GT_{alignment_mode}_aligned.rrd"


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
    capture_timestamps_ns = metadata_comment_us(session_dir / spec.relative_path) * 1_000 + frame_timestamps_ns
    return int(capture_timestamps_ns[0]), int(capture_timestamps_ns[-1]), int(len(capture_timestamps_ns))


def video_capture_timestamps_ns(
    session_dir: Path,
    spec: VideoSpec,
    video_path: Path,
) -> np.ndarray:
    video_asset = rr.AssetVideo(path=video_path)
    frame_timestamps_ns = np.asarray(video_asset.read_frame_timestamps_nanos(), dtype=np.int64)
    return metadata_comment_us(session_dir / spec.relative_path) * 1_000 + frame_timestamps_ns


def video_nominal_frame_rate_hz(path: Path) -> float | None:
    stream = next((stream for stream in probe_video(path).get("streams", []) if stream.get("codec_type") == "video"), None)
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
            result[gt_index] = int(round(int(reference_video_timestamps[0]) + video_float * video_dt_ns))
        elif video_index + 1 < len(reference_video_timestamps):
            start_ns = int(reference_video_timestamps[video_index])
            end_ns = int(reference_video_timestamps[video_index + 1])
            result[gt_index] = int(round(start_ns + (end_ns - start_ns) * fraction))
        else:
            result[gt_index] = int(round(int(reference_video_timestamps[-1]) + (video_float - (len(reference_video_timestamps) - 1)) * video_dt_ns))
    return result


def with_frame_aligned_gt_timestamps(
    gt_config: GTConfig | None,
    reference_video_timestamps: np.ndarray | None,
    video_rate_hz: float | None,
    frame_ratio: float | None = None,
    frame_offset: int = 0,
) -> GTConfig | None:
    if gt_config is None or reference_video_timestamps is None or len(reference_video_timestamps) == 0:
        return gt_config
    gt_rate_hz = infer_gt_frame_rate_hz(gt_config)
    if frame_ratio is None and (gt_rate_hz is None or video_rate_hz is None or video_rate_hz <= 0):
        return gt_config
    ratio = max(1.0, float(frame_ratio) if frame_ratio is not None else float(gt_rate_hz) / float(video_rate_hz))

    def aligned(timestamps_ns: np.ndarray) -> np.ndarray:
        return synthesize_frame_aligned_timestamps(len(timestamps_ns), reference_video_timestamps, ratio, frame_offset)

    skeleton = replace(gt_config.skeleton, timestamps_ns=aligned(gt_config.skeleton.timestamps_ns)) if gt_config.skeleton is not None else None
    mesh = replace(gt_config.mesh, timestamps_ns=aligned(gt_config.mesh.timestamps_ns)) if gt_config.mesh is not None else None
    marker_tracks = tuple(replace(track, timestamps_ns=aligned(track.timestamps_ns)) for track in gt_config.marker_tracks)
    mano_mesh_tracks = tuple(replace(track, timestamps_ns=aligned(track.timestamps_ns)) for track in gt_config.mano_mesh_tracks)
    note = gt_config.note or ""
    note = (note + "\n" if note else "") + (
        f"GT frame-aligned to reference video: ratio={ratio:.9f} GT frames per video frame; "
        f"frame_offset={frame_offset}, so video frame N maps to GT frame round(N*ratio)+offset."
    )
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
        ranges.append((int(gt_config.skeleton.timestamps_ns[0]), int(gt_config.skeleton.timestamps_ns[-1]), len(gt_config.skeleton.timestamps_ns)))
    if gt_config.mesh is not None and len(gt_config.mesh.timestamps_ns):
        ranges.append((int(gt_config.mesh.timestamps_ns[0]), int(gt_config.mesh.timestamps_ns[-1]), len(gt_config.mesh.timestamps_ns)))
    for track in gt_config.marker_tracks:
        if len(track.timestamps_ns):
            ranges.append((int(track.timestamps_ns[0]), int(track.timestamps_ns[-1]), len(track.timestamps_ns)))
    for track in gt_config.mano_mesh_tracks:
        if len(track.timestamps_ns):
            ranges.append((int(track.timestamps_ns[0]), int(track.timestamps_ns[-1]), len(track.timestamps_ns)))
    if gt_config.third_person_video is not None and gt_config.third_person_start_ns is not None:
        video_asset = rr.AssetVideo(path=gt_config.third_person_video)
        frame_timestamps_ns = np.asarray(video_asset.read_frame_timestamps_nanos(), dtype=np.int64)
        if len(frame_timestamps_ns):
            start_ns = int(gt_config.third_person_start_ns + gt_config.time_offset_ns + frame_timestamps_ns[0])
            end_ns = int(gt_config.third_person_start_ns + gt_config.time_offset_ns + frame_timestamps_ns[-1])
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

    valid_ranges = [(start_ns, end_ns) for start_ns, end_ns, count in ranges if count and end_ns >= start_ns]
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
                        "duration_s": (int(track.timestamps_ns[-1]) - int(track.timestamps_ns[0])) / 1e9,
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
                        "duration_s": (int(track.timestamps_ns[-1]) - int(track.timestamps_ns[0])) / 1e9,
                        "count": len(track.timestamps_ns),
                        "offset_from_left_video_s": "",
                    }
                )
        if gt_config.third_person_video is not None and gt_config.third_person_start_ns is not None:
            video_asset = rr.AssetVideo(path=gt_config.third_person_video)
            frame_timestamps_ns = np.asarray(video_asset.read_frame_timestamps_nanos(), dtype=np.int64)
            if len(frame_timestamps_ns):
                start_ns = int(gt_config.third_person_start_ns + gt_config.time_offset_ns + frame_timestamps_ns[0])
                end_ns = int(gt_config.third_person_start_ns + gt_config.time_offset_ns + frame_timestamps_ns[-1])
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

    report_path = artifact_paths.inspection_dir / f"{session_dir.name}_{config.segment_name}_{alignment_mode}_aligned_time_alignment_report.csv"
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
        indexes=[rr.TimeColumn("capture_time", duration=capture_timestamps_ns * 1e-9)],
        columns=rr.VideoFrameReference.columns_nanos(frame_timestamps_ns),
    )


def log_signal(session_dir: Path, spec: SignalSpec, max_points: int, capture_window: TimeWindow | None) -> None:
    timestamps_ns, values_by_axis = fetch_signal_rows(
        session_dir / spec.relative_path,
        spec.table,
        spec.columns,
        max_points,
        capture_window,
    )
    if len(timestamps_ns) == 0:
        return
    time_column = rr.TimeColumn("capture_time", duration=timestamps_ns * 1e-9)

    for axis, values in values_by_axis.items():
        entity = f"{spec.origin}/{axis}"
        rr.log(entity, rr.SeriesLines(names=[axis], colors=[AXIS_COLORS[axis]], widths=[1.5]), static=True)
        rr.send_columns(entity, indexes=[time_column], columns=rr.Scalars.columns(scalars=values))


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
            raise ValueError(f"GT timestamp count {len(timestamps)} does not match frame count {frame_count}")
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
        raise ValueError(f"GT mesh vertices must have shape [frames, vertices, 3] or [vertices, 3], got {vertices.shape}")
    if vertices.shape[0] != frame_count:
        raise ValueError(f"GT mesh frame count {vertices.shape[0]} does not match expected {frame_count}")
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
        raise ValueError(f"skeleton CSV is missing joint samples for at least one timestamp: {path}")

    parents = DEFAULT_SKELETON_PARENTS[: len(joint_names)]
    if len(parents) < len(joint_names):
        parents = tuple([-1, *range(0, len(joint_names) - 1)])
    return GTSkeletonTrack(
        timestamps_ns=normalize_timestamps_ns(timestamps, len(timestamps), time_offset_ns),
        joints=joints * scale,
        joint_names=joint_names,
        parents=parents,
    )


def load_skeleton_track(path: Path, scale: float, time_offset_ns: int, max_frames: int | None) -> GTSkeletonTrack:
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
            timestamps_raw = [frame.get("capture_time_ns", frame.get("timestamp")) for frame in data["frames"]]
        joint_names_raw = data.get("joint_names")
        joint_names = tuple(str(name) for name in joint_names_raw) if joint_names_raw is not None else tuple(f"joint_{i:02d}" for i in range(joints.shape[1]))
        parents_raw = first_present(data, ("parents", "skeleton_parents"))
        parents = tuple(int(value) for value in parents_raw) if parents_raw is not None else DEFAULT_SKELETON_PARENTS[: joints.shape[1]]
        if len(parents) < joints.shape[1]:
            parents = tuple([-1, *range(0, joints.shape[1] - 1)])
        track = GTSkeletonTrack(
            timestamps_ns=normalize_timestamps_ns(timestamps_raw, joints.shape[0], time_offset_ns),
            joints=joints,
            joint_names=joint_names,
            parents=parents,
        )

    return downsample_gt_skeleton(track, max_frames)


def load_mesh_track(path: Path, scale: float, time_offset_ns: int, max_frames: int | None, fallback_timestamps_ns: np.ndarray | None) -> GTMeshTrack:
    data = read_npz_dict(path) if path.suffix.lower() == ".npz" else read_json_dict(path)
    vertices_raw = first_present(data, ("vertices", "verts", "vertex_positions"))
    if vertices_raw is None and "frames" in data:
        vertices_raw = [frame["vertices"] for frame in data["frames"]]
    faces_raw = first_present(data, ("faces", "triangles", "triangle_indices"))
    if faces_raw is None:
        raise ValueError(f"GT mesh file must contain faces/triangles: {path}")

    frame_count = np.asarray(vertices_raw).shape[0] if np.asarray(vertices_raw).ndim == 3 else (len(fallback_timestamps_ns) if fallback_timestamps_ns is not None else 1)
    timestamps_raw = first_present(data, ("capture_time_ns", "timestamps_ns", "timestamps"))
    if timestamps_raw is None and fallback_timestamps_ns is not None and len(fallback_timestamps_ns) == frame_count:
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


def parse_trc(path: Path, scale: float, max_frames: int | None, marker_limit: int | None = None) -> tuple[np.ndarray, tuple[str, ...], np.ndarray]:
    lines = read_text_lines_with_fallback(path)
    frame_header_index = next((index for index, line in enumerate(lines) if line.startswith("Frame#")), None)
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
                frame_positions.append([float(xyz[0]) * scale, float(xyz[1]) * scale, float(xyz[2]) * scale])
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
    wrist_candidates = [name_to_index[name] for name in ("WristM", "WristIn", "WristOut", "HandOffset") if name in name_to_index]
    palm_anchor = wrist_candidates[0] if wrist_candidates else None
    for first, second in zip(wrist_candidates, wrist_candidates[1:]):
        connections.append((first, second))
    for finger in fingers:
        chain = [name_to_index[f"{finger}{i}"] for i in range(1, 5) if f"{finger}{i}" in name_to_index]
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
    timestamps_ns, marker_names, positions = parse_trc(path, scale, max_frames, marker_limit=marker_limit)
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


def parse_bvh(path: Path, scale: float, max_frames: int | None, timestamps_ns: np.ndarray | None = None) -> tuple[np.ndarray, tuple[str, ...], np.ndarray, tuple[int, ...]]:
    lines = read_text_lines_with_fallback(path)
    joints: list[BvhJoint] = []
    stack: list[int] = []
    pending_joint: tuple[str, int] | None = None
    channel_slices: list[tuple[int, int]] = []
    channel_cursor = 0
    motion_index = next((index for index, line in enumerate(lines) if line.strip() == "MOTION"), None)
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
                joints.append(BvhJoint(name=name, parent=parent, offset=np.zeros(3, dtype=np.float32), channels=()))
                stack.append(len(joints) - 1)
                pending_joint = None
        elif parts[0] == "}":
            if stack:
                stack.pop()
        elif parts[0] == "OFFSET" and stack:
            joints[stack[-1]] = replace(joints[stack[-1]], offset=np.asarray([float(value) for value in parts[1:4]], dtype=np.float32) * scale)
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
        raise ValueError(f"BVH channel count mismatch in {path}: expected {channel_cursor}, got {values.shape[1]}")

    if timestamps_ns is None or len(timestamps_ns) != len(values):
        timestamps_ns = (np.arange(len(values), dtype=np.int64) * int(frame_time * 1e9)).astype(np.int64)

    positions = bvh_global_positions(joints, channel_slices, values, scale)
    indexes = downsample_indexes(len(timestamps_ns), max_frames)
    return timestamps_ns[indexes], tuple(joint.name for joint in joints), positions[indexes], tuple(joint.parent for joint in joints)


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


def bvh_global_positions(joints: Sequence[BvhJoint], channel_slices: Sequence[tuple[int, int]], values: np.ndarray, scale: float) -> np.ndarray:
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
                translations[joint_index] = translations[parent] + rotations[parent] @ local_translation
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
    track_timestamps_ns, joint_names, positions, parents = parse_bvh(path, scale, max_frames, timestamps_ns=timestamps_ns)
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


def parse_nokov_segment_hierarchy(lines: Sequence[str]) -> tuple[tuple[str, ...], tuple[int, ...]]:
    section_index = next((index for index, line in enumerate(lines) if line.strip() == "[SegmentNames&Hierarchy]"), None)
    if section_index is None:
        raise ValueError("CSV file has no [SegmentNames&Hierarchy] section")

    names: list[str] = []
    parents_raw: list[str] = []
    for line in lines[section_index + 2 :]:
        stripped = line.strip()
        if not stripped:
            break
        if stripped.startswith("["):
            break
        cells = [cell.strip() for cell in stripped.split(",")]
        if not cells or not cells[0]:
            continue
        names.append(cells[0])
        parents_raw.append(cells[1] if len(cells) > 1 else "")

    name_to_index = {name: index for index, name in enumerate(names)}
    parents = tuple(name_to_index.get(parent, -1) if parent else -1 for parent in parents_raw)
    return tuple(names), parents


def parse_nokov_csv(path: Path, scale: float, max_frames: int | None) -> tuple[np.ndarray, tuple[str, ...], np.ndarray, tuple[int, ...]]:
    lines = read_text_lines_with_fallback(path)
    names, parents = parse_nokov_segment_hierarchy(lines)
    segment_data_index = next((index for index, line in enumerate(lines) if line.strip() == "[SegmentData]"), None)
    if segment_data_index is None:
        raise ValueError(f"CSV file has no [SegmentData] section: {path}")

    header = next(csv.reader([lines[segment_data_index + 2]]))
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
                raise ValueError(f"CSV missing global or parent XYZ columns for segment {segment_index}: {path}") from exc

    timestamps: list[int] = []
    frames: list[list[list[float]]] = []
    for line in lines[segment_data_index + 3 :]:
        if not line.strip():
            continue
        cells = next(csv.reader([line]))
        if len(cells) < 2 or not cells[0].strip().isdigit():
            continue
        timestamps.append(int(float(cells[1].strip())) * 1_000_000)
        frame_positions: list[list[float]] = []
        for x_col, y_col, z_col in zip(x_columns, y_columns, z_columns):
            xyz = [cells[x_col].strip(), cells[y_col].strip(), cells[z_col].strip()]
            if any(not value for value in xyz):
                frame_positions.append([np.nan, np.nan, np.nan])
            else:
                frame_positions.append([float(xyz[0]) * scale, float(xyz[1]) * scale, float(xyz[2]) * scale])
        frames.append(frame_positions)

    if not frames:
        raise ValueError(f"CSV file has no data rows: {path}")
    positions = np.asarray(frames, dtype=np.float32)
    if np.isnan(positions).any():
        positions = fill_missing_marker_positions(positions)
    timestamps_ns = np.asarray(timestamps, dtype=np.int64)
    indexes = downsample_indexes(len(timestamps_ns), max_frames)
    return timestamps_ns[indexes], names, positions[indexes], parents


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


def chumpy_safe_array(value: Any) -> np.ndarray:
    if hasattr(value, "r"):
        return np.asarray(value.r)
    return np.asarray(value)


def load_mano_template(model_dir: Path, side: str) -> ManoTemplate:
    if side not in {"left", "right"}:
        raise ValueError(f"side must be left or right, got {side}")
    model_path = model_dir / ("MANO_LEFT.pkl" if side == "left" else "MANO_RIGHT.pkl")
    if not model_path.exists():
        raise FileNotFoundError(model_path)

    # MANO pkl files may import old chumpy symbols on modern Python/Numpy.
    inspect.getargspec = getattr(inspect, "getargspec", inspect.getfullargspec)
    for name, value in (("int", int), ("float", float), ("complex", complex), ("object", object), ("unicode", str), ("str", str)):
        if not hasattr(np, name):
            setattr(np, name, value)

    with model_path.open("rb") as f:
        data = pickle.load(f, encoding="latin1")
    vertices = chumpy_safe_array(data["v_template"]).astype(np.float32)
    faces = chumpy_safe_array(data["f"]).astype(np.uint32)
    joints = chumpy_safe_array(data["J"]).astype(np.float32)
    parents = chumpy_safe_array(data["kintree_table"])[0].astype(np.int64)
    parents[parents > 1000] = -1
    weights = chumpy_safe_array(data["weights"]).astype(np.float32)
    return ManoTemplate(vertices=vertices, faces=faces, joints=joints, parents=parents, weights=weights)


def normalize_mano_template(template: ManoTemplate) -> ManoTemplate:
    vertices = template.vertices
    centered = vertices - vertices.mean(axis=0, keepdims=True)
    scale = float(np.percentile(np.linalg.norm(centered, axis=1), 95))
    if scale <= 1e-8:
        scale = 1.0
    center = vertices.mean(axis=0, keepdims=True)
    return replace(
        template,
        vertices=(template.vertices - center) / scale,
        joints=(template.joints - center) / scale,
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
        axis = normalize_vector(np.cross(source, np.array([1.0, 0.0, 0.0], dtype=np.float32)), np.array([0.0, 1.0, 0.0], dtype=np.float32))
        if np.linalg.norm(axis) <= 1e-8:
            axis = normalize_vector(np.cross(source, np.array([0.0, 1.0, 0.0], dtype=np.float32)), np.array([0.0, 0.0, 1.0], dtype=np.float32))
        return axis_angle_matrix(axis, np.pi)
    skew = np.asarray(
        [[0, -cross[2], cross[1]], [cross[2], 0, -cross[0]], [-cross[1], cross[0], 0]],
        dtype=np.float32,
    )
    return (np.eye(3, dtype=np.float32) + skew + skew @ skew * (1.0 / (1.0 + dot))).astype(np.float32)


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


def hand_pose_from_markers(marker_names: Sequence[str], positions: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    wrist_ids = [marker_index(marker_names, name) for name in ("WristM", "WristIn", "WristOut", "HandOffset", "LeftHand", "RightHand")]
    wrist_ids = [index for index in wrist_ids if index is not None]
    origin = positions[wrist_ids].mean(axis=0) if wrist_ids else positions.mean(axis=0)

    wrist_in = marker_index(marker_names, "WristIn")
    wrist_out = marker_index(marker_names, "WristOut")
    if wrist_in is not None and wrist_out is not None:
        x_axis = normalize_vector(positions[wrist_out] - positions[wrist_in], np.array([1.0, 0.0, 0.0], dtype=np.float32))
    else:
        index_base = next((marker_index(marker_names, name) for name in ("LeftHandIndex0", "RightHandIndex0", "FingerIndex1") if marker_index(marker_names, name) is not None), None)
        pinky_base = next((marker_index(marker_names, name) for name in ("LeftHandPinky0", "RightHandPinky0", "FingerPinky1") if marker_index(marker_names, name) is not None), None)
        if index_base is not None and pinky_base is not None:
            x_axis = normalize_vector(positions[index_base] - positions[pinky_base], np.array([1.0, 0.0, 0.0], dtype=np.float32))
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
    forward_point = positions[fingertip_ids].mean(axis=0) if fingertip_ids else positions.mean(axis=0)
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


def first_named_position(marker_names: Sequence[str], positions: np.ndarray, names: Sequence[str]) -> np.ndarray | None:
    for name in names:
        index = marker_index(marker_names, name)
        if index is not None:
            return positions[index]
    return None


def finger_joint_names(side: str, finger: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
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


def target_mano_joints_from_track(track: GTMarkerTrack, template: ManoTemplate, side: str, positions: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    origin, basis, hand_scale = hand_pose_from_markers(track.marker_names, positions)
    target = template.joints * hand_scale @ basis.T + origin[None, :]
    target[0] = origin
    for finger, mano_indexes in MANO_CHAIN_NAMES.items():
        for mano_index, candidate_names in zip(mano_indexes, finger_joint_names(side, finger)):
            value = first_named_position(track.marker_names, positions, candidate_names)
            if value is not None:
                target[mano_index] = value
    return target.astype(np.float32), basis.astype(np.float32), hand_scale


def posed_vertices_from_joints(template: ManoTemplate, target_joints: np.ndarray, root_basis: np.ndarray, root_scale: float) -> np.ndarray:
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
        transforms_t[joint_index] = target_joints[joint_index] - rotation @ (template.joints[joint_index] * root_scale)

    rest_vertices = template.vertices * root_scale
    posed = np.zeros_like(rest_vertices, dtype=np.float32)
    for joint_index in range(len(template.joints)):
        transformed = rest_vertices @ transforms_r[joint_index].T + transforms_t[joint_index][None, :]
        posed += transformed * template.weights[:, joint_index : joint_index + 1]
    return posed.astype(np.float32)


def mano_mesh_from_marker_track(track: GTMarkerTrack, model_dir: Path, side: str) -> GTManoMeshTrack:
    template = normalize_mano_template(load_mano_template(model_dir, side))

    frames: list[np.ndarray] = []
    for positions in track.positions:
        target_joints, root_basis, root_scale = target_mano_joints_from_track(track, template, side, positions)
        frames.append(posed_vertices_from_joints(template, target_joints, root_basis, root_scale))

    label = f"{track.source}_{'left' if side == 'left' else 'right'}_mano_mesh"
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


def discover_gt_dir(session_dir: Path, explicit_gt_dir: Path | None) -> Path | None:
    if explicit_gt_dir is not None:
        if not explicit_gt_dir.exists():
            raise FileNotFoundError(explicit_gt_dir)
        return explicit_gt_dir
    matches = sorted(path for path in session_dir.glob("test*") if path.is_dir())
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"multiple test* GT folders found; pass --gt-dir explicitly: {matches}")
    return matches[0]


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


def discover_gt_file_sets(gt_dir: Path) -> list[GTFileSet]:
    prefix = gt_dir.name
    return [
        GTFileSet(
            label="camera_trackers",
            side=None,
            trc=find_gt_by_patterns(gt_dir, (f"{prefix}-Tracker0.trc", "*Tracker0.trc", "*head.trc", "*Head.trc")),
            csv=find_gt_by_patterns(gt_dir, (f"{prefix}-Tracker0.csv", "*Tracker0.csv", "*head.csv", "*Head.csv")),
            colors=(80, 200, 255),
            radius=0.035,
        ),
        GTFileSet(
            label="LHand",
            side="left",
            bvh=find_gt_by_patterns(gt_dir, (f"{prefix}-LHand.bvh", "*LHand.bvh", "*Left.bvh", "*_Left.bvh")),
            trc=find_gt_by_patterns(gt_dir, (f"{prefix}-LHand.trc", "*LHand.trc", "*Left.trc", "*_Left.trc")),
            csv=find_gt_by_patterns(gt_dir, (f"{prefix}-LHand.csv", "*LHand.csv", "*Left.csv", "*_Left.csv")),
            colors=(255, 210, 80),
            connect_hands=True,
        ),
        GTFileSet(
            label="RHand",
            side="right",
            bvh=find_gt_by_patterns(gt_dir, (f"{prefix}-RHand.bvh", "*RHand.bvh", "*Right.bvh", "*_Right.bvh")),
            trc=find_gt_by_patterns(gt_dir, (f"{prefix}-RHand.trc", "*RHand.trc", "*Right.trc", "*_Right.trc")),
            csv=find_gt_by_patterns(gt_dir, (f"{prefix}-RHand.csv", "*RHand.csv", "*Right.csv", "*_Right.csv")),
            colors=(255, 120, 90),
            connect_hands=True,
        ),
        GTFileSet(
            label="hand",
            side=None,
            bvh=find_gt_by_patterns(gt_dir, (f"{prefix}-hand.bvh", "*-hand.bvh", "*hand.bvh")),
            trc=find_gt_by_patterns(gt_dir, (f"{prefix}-hand.trc", "*-hand.trc", "*hand.trc")),
            csv=find_gt_by_patterns(gt_dir, (f"{prefix}-hand.csv", "*-hand.csv", "*hand.csv")),
            colors=(170, 130, 255),
            connect_hands=True,
        ),
    ]


def append_note(note_parts: list[str], message: str) -> None:
    if message not in note_parts:
        note_parts.append(message)


def load_test_gt_dir(
    gt_dir: Path,
    scale: float,
    bvh_scale: float,
    max_frames: int | None,
    mano_model_dir: Path | None,
) -> tuple[tuple[GTMarkerTrack, ...], tuple[GTManoMeshTrack, ...], Path | None, tuple[str, ...]]:
    tracks: list[GTMarkerTrack] = []
    mano_tracks: list[GTManoMeshTrack] = []
    note_parts: list[str] = []

    for file_set in discover_gt_file_sets(gt_dir):
        if file_set.bvh is not None:
            try:
                bvh_track = load_bvh_track(
                    file_set.bvh,
                    file_set.label,
                    f"{GT_TRACKS_ENTITY}/bvh/{file_set.label}",
                    "bvh",
                    bvh_scale,
                    max_frames,
                    timestamps_ns=timestamps_from_trc(file_set.trc) if file_set.trc is not None else None,
                    colors=file_set.colors,
                    radius=file_set.radius,
                )
                tracks.append(bvh_track)
                if mano_model_dir is not None and file_set.side is not None:
                    mano_tracks.append(mano_mesh_from_marker_track(bvh_track, mano_model_dir, file_set.side))
            except Exception as exc:
                append_note(note_parts, f"failed to load BVH {file_set.bvh.name}: {exc}")
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
                tracks.append(trc_track)
                if mano_model_dir is not None and file_set.side is not None:
                    mano_tracks.append(mano_mesh_from_marker_track(trc_track, mano_model_dir, file_set.side))
            except Exception as exc:
                append_note(note_parts, f"failed to load TRC {file_set.trc.name}: {exc}")
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
                tracks.append(csv_track)
                if mano_model_dir is not None and file_set.side is not None:
                    mano_tracks.append(mano_mesh_from_marker_track(csv_track, mano_model_dir, file_set.side))
            except Exception as exc:
                append_note(note_parts, f"failed to load CSV {file_set.csv.name}: {exc}")

    prefix = gt_dir.name
    third_person_video = find_gt_by_patterns(gt_dir, (f"{prefix}-1.mp4", "*.mp4"))
    return tuple(tracks), tuple(mano_tracks), third_person_video if third_person_video is not None and third_person_video.exists() else None, tuple(note_parts)


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
    skeleton = replace(gt_config.skeleton, timestamps_ns=gt_config.skeleton.timestamps_ns + offset_ns) if gt_config.skeleton is not None else None
    mesh = replace(gt_config.mesh, timestamps_ns=gt_config.mesh.timestamps_ns + offset_ns) if gt_config.mesh is not None else None
    marker_tracks = tuple(replace(track, timestamps_ns=track.timestamps_ns + offset_ns) for track in gt_config.marker_tracks)
    mano_mesh_tracks = tuple(replace(track, timestamps_ns=track.timestamps_ns + offset_ns) for track in gt_config.mano_mesh_tracks)
    third_person_start_ns = gt_config.third_person_start_ns + offset_ns if gt_config.third_person_start_ns is not None else None
    return replace(gt_config, skeleton=skeleton, mesh=mesh, marker_tracks=marker_tracks, mano_mesh_tracks=mano_mesh_tracks, third_person_start_ns=third_person_start_ns)


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


def maybe_align_gt_to_robocap(session_dir: Path, config: SessionConfig, gt_config: GTConfig | None, enabled: bool) -> GTConfig | None:
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
    if gt_dir is not None:
        marker_tracks, mano_mesh_tracks, discovered_third_person_video, gt_note_parts = load_test_gt_dir(
            gt_dir,
            args.gt_coordinate_scale,
            args.bvh_coordinate_scale,
            args.gt_max_frames,
            args.mano_model_dir if not args.no_mano_mesh else None,
        )
        note_parts.extend(gt_note_parts)

    third_person_video = args.gt_third_person_video or discovered_third_person_video

    skeleton = (
        load_skeleton_track(args.gt_skeleton, args.gt_coordinate_scale, args.gt_time_offset_ns, args.gt_max_frames)
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
        note_parts.append("no test* GT directory discovered")
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
    )


def skeleton_bone_strips(joints: np.ndarray, parents: Sequence[int]) -> list[np.ndarray]:
    strips: list[np.ndarray] = []
    for child, parent in enumerate(parents):
        if parent < 0 or child >= len(joints) or parent >= len(joints):
            continue
        strips.append(np.stack([joints[parent], joints[child]], axis=0))
    return strips


def set_capture_time(timestamp_ns: int) -> None:
    if hasattr(rr, "set_time"):
        rr.set_time("capture_time", duration=float(timestamp_ns) * 1e-9)
    else:
        rr.set_time_nanos("capture_time", int(timestamp_ns))


def log_gt_skeleton(track: GTSkeletonTrack, capture_window: TimeWindow | None) -> None:
    for timestamp_ns, joints in zip(track.timestamps_ns, track.joints):
        if capture_window is not None and not (capture_window.start_ns <= timestamp_ns <= capture_window.end_ns):
            continue
        set_capture_time(int(timestamp_ns))
        rr.log(
            f"{GT_SKELETON_ENTITY}/joints",
            rr.Points3D(joints, radii=0.018, colors=[255, 210, 80]),
        )
        rr.log(
            f"{GT_SKELETON_ENTITY}/bones",
            rr.LineStrips3D(skeleton_bone_strips(joints, track.parents), radii=0.01, colors=[60, 180, 255]),
        )


def log_gt_mesh(track: GTMeshTrack, capture_window: TimeWindow | None) -> None:
    for timestamp_ns, vertices in zip(track.timestamps_ns, track.vertices):
        if capture_window is not None and not (capture_window.start_ns <= timestamp_ns <= capture_window.end_ns):
            continue
        set_capture_time(int(timestamp_ns))
        rr.log(
            GT_MESH_ENTITY,
            rr.Mesh3D(
                vertex_positions=vertices,
                triangle_indices=track.faces,
                albedo_factor=[0.78, 0.78, 0.82],
            ),
        )


def log_gt_mano_mesh(track: GTManoMeshTrack, capture_window: TimeWindow | None) -> None:
    for timestamp_ns, vertices in zip(track.timestamps_ns, track.vertices):
        if capture_window is not None and not (capture_window.start_ns <= timestamp_ns <= capture_window.end_ns):
            continue
        set_capture_time(int(timestamp_ns))
        rr.log(
            track.entity,
            rr.Mesh3D(
                vertex_positions=vertices,
                triangle_indices=track.faces,
                albedo_factor=list(track.color),
            ),
        )


def marker_connection_strips(positions: np.ndarray, connections: Sequence[tuple[int, int]]) -> list[np.ndarray]:
    return [np.stack([positions[first], positions[second]], axis=0) for first, second in connections]


def log_gt_marker_track(track: GTMarkerTrack, capture_window: TimeWindow | None) -> None:
    rr.log(f"{track.entity}/labels", rr.TextDocument(", ".join(track.marker_names)), static=True)
    for timestamp_ns, positions in zip(track.timestamps_ns, track.positions):
        if capture_window is not None and not (capture_window.start_ns <= timestamp_ns <= capture_window.end_ns):
            continue
        set_capture_time(int(timestamp_ns))
        rr.log(
            f"{track.entity}/points",
            rr.Points3D(positions, radii=track.radius, colors=list(track.colors)),
        )
        if track.connections:
            rr.log(
                f"{track.entity}/connections",
                rr.LineStrips3D(marker_connection_strips(positions, track.connections), radii=track.radius * 0.45, colors=list(track.colors)),
            )


def log_gt_third_person_video(video_path: Path, time_offset_ns: int, start_timestamp_ns: int | None, capture_window: TimeWindow | None) -> None:
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
        indexes=[rr.TimeColumn("capture_time", duration=capture_timestamps_ns * 1e-9)],
        columns=rr.VideoFrameReference.columns_nanos(frame_timestamps_ns),
    )


def log_gt(gt_config: GTConfig | None, capture_window: TimeWindow | None) -> None:
    if gt_config is None:
        rr.log(GT_NOTE_ENTITY, rr.TextDocument("no GT data discovered"), static=True)
        return
    if gt_config.skeleton is not None:
        log_gt_skeleton(gt_config.skeleton, capture_window)
    if gt_config.mesh is not None:
        log_gt_mesh(gt_config.mesh, capture_window)
    for track in gt_config.mano_mesh_tracks:
        log_gt_mano_mesh(track, capture_window)
    for track in gt_config.marker_tracks:
        log_gt_marker_track(track, capture_window)
    if gt_config.third_person_video is not None:
        log_gt_third_person_video(gt_config.third_person_video, gt_config.time_offset_ns, gt_config.third_person_start_ns, capture_window)
    rr.log(GT_NOTE_ENTITY, rr.TextDocument(gt_config.note or "GT data loaded; unavailable tabs have no source for this session"), static=True)


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


def source_has_marker_tracks(gt_config: GTConfig | None, source: str) -> bool:
    return gt_config is not None and any(track.source == source for track in gt_config.marker_tracks)


def source_has_mano_mesh_tracks(gt_config: GTConfig | None, source: str) -> bool:
    return gt_config is not None and any(track.source == source for track in gt_config.mano_mesh_tracks)


def gt_skeleton_source_view(gt_config: GTConfig | None, source: str) -> rrb.View:
    if source_has_marker_tracks(gt_config, source):
        return rrb.Spatial3DView(name=source.upper(), origin=f"{GT_TRACKS_ENTITY}/{source}")
    return rrb.TextDocumentView(name=source.upper(), origin=GT_NOTE_ENTITY)


def gt_mesh_source_view(gt_config: GTConfig | None, source: str) -> rrb.View:
    if source_has_mano_mesh_tracks(gt_config, source):
        return rrb.Spatial3DView(name=source.upper(), origin=f"{GT_MESH_ENTITY}/{source}")
    return rrb.TextDocumentView(name=source.upper(), origin=GT_NOTE_ENTITY)


def gt_skeleton_tabs(gt_config: GTConfig | None) -> rrb.Tabs:
    return rrb.Tabs(
        gt_skeleton_source_view(gt_config, "bvh"),
        gt_skeleton_source_view(gt_config, "trc"),
        gt_skeleton_source_view(gt_config, "csv"),
        active_tab="BVH",
        name="GT skeleton",
    )


def gt_mesh_tabs(gt_config: GTConfig | None) -> rrb.Tabs:
    return rrb.Tabs(
        gt_mesh_source_view(gt_config, "bvh"),
        gt_mesh_source_view(gt_config, "trc"),
        gt_mesh_source_view(gt_config, "csv"),
        active_tab="BVH",
        name="GT MANO mesh",
    )


def gt_third_person_video_view(gt_config: GTConfig | None) -> rrb.View:
    if gt_config is not None and gt_config.third_person_video is not None:
        return rrb.Spatial2DView(name="GT third-person video", origin=GT_THIRD_PERSON_VIDEO_ENTITY)
    return rrb.TextDocumentView(name="GT third-person video", origin=GT_NOTE_ENTITY)


def robocap_sensors_container(config: SessionConfig) -> rrb.ContainerLike:
    return rrb.Grid(
        rrb.Horizontal(
            signal_or_note_view(config, "middle_mag"),
            rrb.Vertical(
                rrb.Horizontal(
                    signal_or_note_view(config, "left_robocap_acc"),
                    signal_or_note_view(config, "left_robocap_gyro"),
                    name="Left robocap IMU",
                ),
                rrb.Horizontal(
                    signal_or_note_view(config, "right_robocap_acc"),
                    signal_or_note_view(config, "right_robocap_gyro"),
                    name="Right robocap IMU",
                ),
                row_shares=[1.0, 1.0],
                name="Robocap IMU rows",
            ),
            column_shares=[1.0, 2.0],
            name="Robocap sensors",
        ),
        grid_columns=1,
        name="Robocap sensors only",
    )


def all_signals_container(config: SessionConfig) -> rrb.ContainerLike:
    return rrb.Grid(
        robocap_sensors_container(config),
        rrb.Horizontal(
            signal_or_note_view(config, "left_wrist_mag"),
            signal_or_note_view(config, "left_wrist_acc"),
            signal_or_note_view(config, "left_wrist_gyro"),
            name="Left wrist sensors",
        ),
        rrb.Horizontal(
            signal_or_note_view(config, "right_wrist_mag"),
            signal_or_note_view(config, "right_wrist_acc"),
            signal_or_note_view(config, "right_wrist_gyro"),
            name="Right wrist sensors",
        ),
        grid_columns=1,
        row_shares=[1.6, 1.0, 1.0],
        name="Signals",
    )


def display_videos_container(config: SessionConfig) -> rrb.ContainerLike:
    return rrb.Horizontal(
        rrb.Vertical(
            video_or_note_view(config, "left"),
            video_or_note_view(config, "right"),
            row_shares=[1.0, 1.0],
            name="left / right",
        ),
        rrb.Vertical(
            video_or_note_view(config, "left_eye"),
            video_or_note_view(config, "right_eye"),
            row_shares=[1.0, 1.0],
            name="left eye / right eye",
        ),
        rrb.Vertical(
            video_or_note_view(config, "left_front"),
            video_or_note_view(config, "right_front"),
            row_shares=[1.0, 1.0],
            name="left front / right front",
        ),
        column_shares=[1.0, 1.0, 1.0],
        name="Robocap videos",
    )


def display_gt_skeleton_row(gt_config: GTConfig | None) -> rrb.ContainerLike:
    return rrb.Horizontal(
        gt_skeleton_source_view(gt_config, "bvh"),
        gt_skeleton_source_view(gt_config, "csv"),
        gt_skeleton_source_view(gt_config, "trc"),
        column_shares=[1.0, 1.0, 1.0],
        name="GT skeleton bvh / csv / trc",
    )


def build_display_blueprint(config: SessionConfig, gt_config: GTConfig | None = None) -> rrb.Blueprint:
    return rrb.Blueprint(
        rrb.TimePanel(timeline="capture_time"),
        rrb.Vertical(
            display_videos_container(config),
            robocap_sensors_container(config),
            display_gt_skeleton_row(gt_config),
            row_shares=[2.4, 1.6, 2.4],
        ),
        collapse_panels=True,
    )


def build_blueprint(config: SessionConfig, gt_config: GTConfig | None = None, preset: str = "default") -> rrb.Blueprint:
    if preset == "display":
        return build_display_blueprint(config, gt_config)

    signals_container = all_signals_container(config)

    rows: list[rrb.ContainerLike] = [
        rrb.Grid(
            video_or_note_view(config, "left"),
            video_or_note_view(config, "left_eye"),
            video_or_note_view(config, "left_front"),
            video_or_note_view(config, "left_wrist_down"),
            video_or_note_view(config, "right"),
            video_or_note_view(config, "right_eye"),
            video_or_note_view(config, "right_front"),
            video_or_note_view(config, "right_wrist_down"),
            grid_columns=4,
            name="Videos",
        ),
        signals_container,
    ]
    row_shares = [2.4, 3.6]
    rows.append(
        rrb.Horizontal(
            gt_skeleton_tabs(gt_config),
            gt_mesh_tabs(gt_config),
            gt_third_person_video_view(gt_config),
            column_shares=[1.0, 1.0, 1.25],
            name="GT",
        )
    )
    row_shares.append(2.4)

    return rrb.Blueprint(
        rrb.TimePanel(timeline="capture_time"),
        rrb.Vertical(*rows, row_shares=row_shares),
        collapse_panels=True,
    )


def print_layout(config: SessionConfig) -> None:
    print("layout:")
    print("  left  | left_eye  | left_front  | left_wrist_down")
    print("  right | right_eye | right_front | right_wrist_down")
    print("  middle_mag | left_robocap_acc  | left_robocap_gyro")
    print("  middle_mag | right_robocap_acc | right_robocap_gyro")
    print("  left_wrist_mag  | left_wrist_acc  | left_wrist_gyro")
    print("  right_wrist_mag | right_wrist_acc | right_wrist_gyro")
    print("  structure: Vertical(Videos, Signals)")
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
            min_ts, max_ts = con.execute(f"select min(timestamp), max(timestamp) from {spec.table}").fetchone()
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
            "Optional GT export folder. If omitted, a single session child folder named test* is auto-discovered. "
            "For the current NOKOV export this reads *-1.mp4, *-Tracker0.trc, *-LHand.trc, and *-RHand.trc."
        ),
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
        "--no-mano-mesh",
        action="store_true",
        help="Disable MANO mesh generation from test* hand TRC tracks.",
    )
    parser.add_argument(
        "--gt-coordinate-scale",
        type=float,
        default=1.0,
        help="Scale factor applied to GT joint and mesh coordinates, e.g. 0.001 for millimeters to meters.",
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
        "--gt-frame-offset",
        type=int,
        default=0,
        help="GT frame offset for frame alignment: video frame N maps to GT frame round(N*ratio)+offset.",
    )
    parser.add_argument(
        "--gt-max-frames",
        type=int,
        default=0,
        help="Optional maximum GT frames after uniform downsampling. Default 0 keeps all GT frames.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.gt_max_frames == 0:
        args.gt_max_frames = None
    session_dir = args.session_dir
    config = discover_session(session_dir, args.segment)
    gt_config = load_gt_config(args, session_dir)
    gt_config = maybe_align_gt_to_robocap(session_dir, config, gt_config, not args.no_gt_align_to_robocap)

    if args.layout_only:
        if args.inspect:
            inspect_session(session_dir, config)
        else:
            print_layout(config)
        if gt_config is not None:
            print("gt:")
            print(f"  skeleton: {'yes' if gt_config.skeleton is not None else 'no'}")
            print(f"  mesh: {'yes' if gt_config.mesh is not None else 'no'}")
            print(f"  marker_tracks: {', '.join(track.label for track in gt_config.marker_tracks) if gt_config.marker_tracks else 'no'}")
            print(f"  mano_mesh_tracks: {', '.join(track.label for track in gt_config.mano_mesh_tracks) if gt_config.mano_mesh_tracks else 'no'}")
            print(f"  third_person_video: {gt_config.third_person_video if gt_config.third_person_video is not None else 'no'}")
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
    if args.gt_alignment_mode == "frame":
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
        gt_config = with_frame_aligned_gt_timestamps(
            gt_config,
            reference_timestamps,
            reference_rate_hz,
            frame_ratio=args.gt_frame_ratio,
            frame_offset=args.gt_frame_offset,
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
    capture_window = None
    if not args.no_trim_to_common_time:
        capture_window = compute_common_capture_window(
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
    if args.inspect:
        inspect_session(session_dir, config)
        print(f"artifacts: {artifact_paths.root_dir}")
        if capture_window is not None:
            print(
                "common capture_time window: "
                f"{capture_window.start_ns}..{capture_window.end_ns} ns "
                f"({(capture_window.end_ns - capture_window.start_ns) / 1e9:.3f} s)"
            )

    blueprint = build_blueprint(config, gt_config, args.blueprint_preset)
    rr.init(
        f"frodobots_{session_dir.name}_{config.segment_name}_dual_hands_{args.gt_alignment_mode}_aligned",
        spawn=args.spawn,
        default_blueprint=blueprint,
    )

    save_path = args.save or default_rrd_path(artifact_paths, session_dir, config, args.gt_alignment_mode)
    if save_path is not None:
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
        )
    for spec in config.signals.values():
        log_signal(session_dir, spec, args.max_sensor_points, capture_window)
    for note in config.notes.values():
        log_note(note)
    log_gt(gt_config, capture_window)

    rr.send_blueprint(blueprint)
    print(f"Rerun dual-hands demo logging complete: {save_path}")
    print(f"Time alignment report: {time_report_path}")
    if capture_window is not None:
        print(
            "Trimmed to common capture_time window: "
            f"{capture_window.start_ns}..{capture_window.end_ns} ns "
            f"({(capture_window.end_ns - capture_window.start_ns) / 1e9:.3f} s)"
        )


if __name__ == "__main__":
    main()
