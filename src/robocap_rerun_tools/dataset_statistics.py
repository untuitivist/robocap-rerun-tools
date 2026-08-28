from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .cli import ffprobe_video, ratio_to_float
from .session_layout import discover_mocap_directories

TIMESTAMP_REPORT_NAME = "timestamp_anomaly_detail_table.html"
REPORT_PREFIX = "const report="
REPORT_SUFFIX = "; const eventTypes="
PRIMITIVE_PATTERN = re.compile(r"(?<![A-Z0-9])(P\d{2})(?![A-Z0-9])", re.IGNORECASE)
ROBOCAP_VIDEO_PATTERN = re.compile(
    r"^robocap_(?P<segment>.+?)_video_(?P<camera>.+)\.mp4$", re.IGNORECASE
)
UNASSIGNED_PRIMITIVE = "UNASSIGNED"


@dataclass(frozen=True)
class SegmentReference:
    session_dir: Path
    segment: str
    video_path: Path
    report_path: Path


@dataclass(frozen=True)
class SegmentStatistic:
    segment: str
    video_path: Path
    report_path: Path
    duration_s: float | None
    status: str
    detail: str = ""


@dataclass(frozen=True)
class SessionStatistic:
    primitive_id: str
    session_dir: Path
    duration_s: float
    unchecked_or_problem_duration_s: float
    segments: tuple[SegmentStatistic, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class PrimitiveStatistic:
    primitive_id: str
    duration_s: float
    unchecked_or_problem_duration_s: float
    sessions: tuple[SessionStatistic, ...]


def session_has_clean_frame_counts(session: SessionStatistic) -> bool:
    return bool(session.segments) and all(segment.status == "clean" for segment in session.segments)


def _camera_priority(camera: str) -> tuple[int, str]:
    priorities = {
        "left": 0,
        "right": 1,
        "left_eye": 2,
        "right_eye": 3,
        "left_front": 4,
        "right_front": 5,
    }
    normalized = camera.casefold()
    return priorities.get(normalized, 100), normalized


def timestamp_report_path(session_dir: Path, segment: str) -> Path:
    return session_dir / "_artifacts" / segment / "inspection" / TIMESTAMP_REPORT_NAME


def discover_segment_references(session_dir: Path) -> tuple[SegmentReference, ...]:
    selected: dict[str, tuple[tuple[int, str], str, Path]] = {}
    try:
        children = sorted(session_dir.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return ()

    for path in children:
        if not path.is_file():
            continue
        match = ROBOCAP_VIDEO_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        segment = match.group("segment")
        rank = _camera_priority(match.group("camera"))
        current = selected.get(segment.casefold())
        if current is None or rank < current[0]:
            selected[segment.casefold()] = (rank, segment, path)

    references = [
        SegmentReference(
            session_dir=session_dir,
            segment=segment,
            video_path=path,
            report_path=timestamp_report_path(session_dir, segment),
        )
        for _, segment, path in selected.values()
    ]
    return tuple(sorted(references, key=lambda item: item.segment.casefold()))


def infer_action_primitive(dataset_root: Path, session_dir: Path) -> str:
    matches: set[str] = set()
    try:
        relative_parts = session_dir.resolve().relative_to(dataset_root.resolve()).parts
    except (OSError, ValueError):
        relative_parts = (session_dir.name,)

    for part in relative_parts:
        matches.update(match.group(1).upper() for match in PRIMITIVE_PATTERN.finditer(part))
    for mocap_dir in discover_mocap_directories(session_dir):
        matches.update(
            match.group(1).upper() for match in PRIMITIVE_PATTERN.finditer(mocap_dir.name)
        )
    return next(iter(matches)) if len(matches) == 1 else UNASSIGNED_PRIMITIVE


def probe_video_duration(path: Path, ffprobe: str) -> tuple[float | None, str | None]:
    data, error = ffprobe_video(path, ffprobe)
    if data is None:
        return None, error or "ffprobe returned no data"

    stream = (data.get("streams") or [{}])[0]
    video_format = data.get("format") or {}
    for raw_duration in (stream.get("duration"), video_format.get("duration")):
        try:
            duration_s = float(raw_duration)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration_s) and duration_s > 0:
            return duration_s, None

    frame_count = stream.get("nb_frames")
    fps = ratio_to_float(stream.get("avg_frame_rate")) or ratio_to_float(stream.get("r_frame_rate"))
    try:
        frames = int(frame_count)
    except (TypeError, ValueError):
        frames = 0
    if frames > 0 and fps is not None and math.isfinite(fps) and fps > 0:
        return frames / fps, None
    return None, "video duration and frame_count/FPS fallback are unavailable"


def load_report_payload(path: Path) -> dict[str, object]:
    document = path.read_text(encoding="utf-8")
    start = document.find(REPORT_PREFIX)
    if start < 0:
        raise ValueError("inspection report payload marker is missing")
    payload_start = start + len(REPORT_PREFIX)
    payload_end = document.find(REPORT_SUFFIX, payload_start)
    if payload_end < 0:
        raise ValueError("inspection report payload terminator is missing")
    payload = json.loads(document[payload_start:payload_end])
    if not isinstance(payload, dict):
        raise TypeError("inspection report payload is not an object")
    return payload


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_integer(value: object) -> int | None:
    number = _finite_number(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def report_has_frame_count_difference(payload: dict[str, object]) -> bool:
    reference_frames = _nonnegative_integer(payload.get("referenceFrames"))
    mocap_frames = _nonnegative_integer(payload.get("mocapFrames"))
    third_frames = _nonnegative_integer(payload.get("thirdFrames"))
    ratio = _nonnegative_integer(payload.get("ratio"))
    if None in (reference_frames, mocap_frames, third_frames) or ratio not in {4, 8}:
        return True
    return (
        mocap_frames != ratio * (reference_frames + 1)
        or third_frames != reference_frames + 1
    )


def summarize_session(
    dataset_root: Path,
    session_dir: Path,
    ffprobe: str,
) -> SessionStatistic:
    segments: list[SegmentStatistic] = []
    errors: list[str] = []
    total_duration_s = 0.0
    unchecked_or_problem_duration_s = 0.0

    references = discover_segment_references(session_dir)
    if not references:
        errors.append("no direct robocap_<segment>_video_<camera>.mp4 reference video")

    for reference in references:
        duration_s, duration_error = probe_video_duration(reference.video_path, ffprobe)
        if duration_s is not None:
            total_duration_s += duration_s
        if duration_error:
            errors.append(f"{reference.video_path.name}: {duration_error}")

        if not reference.report_path.is_file():
            status = "unchecked"
            detail = "inspection report is missing"
        else:
            try:
                payload = load_report_payload(reference.report_path)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                status = "unchecked"
                detail = f"inspection report is unreadable: {exc}"
            else:
                status = (
                    "frame_difference"
                    if report_has_frame_count_difference(payload)
                    else "clean"
                )
                detail = (
                    "frame counts do not match n:ratio*(n+1):n+1"
                    if status != "clean"
                    else ""
                )

        if status != "clean" and duration_s is not None:
            unchecked_or_problem_duration_s += duration_s
        if detail and status == "unchecked":
            errors.append(f"{reference.segment}: {detail}")
        segments.append(
            SegmentStatistic(
                segment=reference.segment,
                video_path=reference.video_path,
                report_path=reference.report_path,
                duration_s=duration_s,
                status=status,
                detail=detail,
            )
        )

    return SessionStatistic(
        primitive_id=infer_action_primitive(dataset_root, session_dir),
        session_dir=session_dir,
        duration_s=total_duration_s,
        unchecked_or_problem_duration_s=unchecked_or_problem_duration_s,
        segments=tuple(segments),
        errors=tuple(errors),
    )


def _primitive_sort_key(value: str) -> tuple[int, int | str]:
    match = re.fullmatch(r"P(\d{2})", value, re.IGNORECASE)
    return (0, int(match.group(1))) if match else (1, value.casefold())


def aggregate_by_primitive(
    sessions: list[SessionStatistic],
) -> tuple[PrimitiveStatistic, ...]:
    grouped: dict[str, list[SessionStatistic]] = {}
    for session in sessions:
        grouped.setdefault(session.primitive_id, []).append(session)

    rows = []
    for primitive_id, items in grouped.items():
        ordered = tuple(sorted(items, key=lambda item: str(item.session_dir).casefold()))
        rows.append(
            PrimitiveStatistic(
                primitive_id=primitive_id,
                duration_s=sum(item.duration_s for item in ordered),
                unchecked_or_problem_duration_s=sum(
                    item.unchecked_or_problem_duration_s for item in ordered
                ),
                sessions=ordered,
            )
        )
    return tuple(sorted(rows, key=lambda item: _primitive_sort_key(item.primitive_id)))


def format_duration(duration_s: float) -> str:
    total_ms = max(0, round(duration_s * 1000.0))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def render_statistics_markdown(
    dataset_root: Path,
    primitives: tuple[PrimitiveStatistic, ...],
    *,
    language: str,
) -> str:
    is_chinese = language == "中文"
    session_count = sum(len(item.sessions) for item in primitives)
    total_duration_s = sum(item.duration_s for item in primitives)
    problem_duration_s = sum(item.unchecked_or_problem_duration_s for item in primitives)
    if is_chinese:
        lines = [
            "## 数据集时长统计",
            "",
            f"- 根目录：`{dataset_root}`",
            f"- Session：**{session_count}**",
            f"- 总时长：**{format_duration(total_duration_s)}**",
            f"- 未检查/差帧时长：**{format_duration(problem_duration_s)}**",
            "",
            "| PXX | 未检查/差帧时长 | 总时长 | Session 数 | {Session: Session 时长} |",
            "|---|---:|---:|---:|---|",
        ]
    else:
        lines = [
            "## Dataset duration statistics",
            "",
            f"- Root: `{dataset_root}`",
            f"- Sessions: **{session_count}**",
            f"- Total duration: **{format_duration(total_duration_s)}**",
            (
                "- Unchecked/frame-count-difference duration: "
                f"**{format_duration(problem_duration_s)}**"
            ),
            "",
            (
                "| PXX | Unchecked/frame-count-difference duration | Total duration | Sessions | "
                "{Session: duration} |"
            ),
            "|---|---:|---:|---:|---|",
        ]

    for primitive in primitives:
        name_counts = Counter(item.session_dir.name for item in primitive.sessions)
        session_durations = {
            (
                item.session_dir.name
                if name_counts[item.session_dir.name] == 1
                else item.session_dir.as_posix()
            ): format_duration(item.duration_s)
            for item in primitive.sessions
        }
        mapping = json.dumps(session_durations, ensure_ascii=False, separators=(", ", ": "))
        primitive_label = (
            "未分类"
            if is_chinese and primitive.primitive_id == UNASSIGNED_PRIMITIVE
            else primitive.primitive_id
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(primitive_label),
                    format_duration(primitive.unchecked_or_problem_duration_s),
                    format_duration(primitive.duration_s),
                    str(len(primitive.sessions)),
                    f"`{_markdown_cell(mapping)}`",
                ]
            )
            + " |"
        )

    if is_chinese:
        lines.extend(
            [
                "",
                (
                    "“未检查/差帧”只包含缺少或无法读取检查报告，以及帧数不满足 "
                    "n:ratio*(n+1):(n+1) 的 Segment。时间戳 diff、推算丢帧、缺失时间戳和 "
                    "frame_index 等其他问题不计入此时长。每个 Segment 只使用一条 Robocap "
                    "参考视频计时。"
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                (
                    "Unchecked/frame-count-difference duration only includes missing or unreadable "
                    "reports and segments whose frame counts do not satisfy "
                    "n:ratio*(n+1):(n+1). Other timestamp, inferred-drop, and frame-index findings "
                    "are ignored. Each segment is timed from one Robocap reference video."
                ),
            ]
        )
    return "\n".join(lines)
