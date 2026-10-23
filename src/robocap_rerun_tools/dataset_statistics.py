from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .cli import ffprobe_video, ratio_to_float
from .inspection_issues import (
    FRAME_ANOMALY_ORDER,
    ISSUE_LABELS,
    classify_frame_count_anomalies,
    classify_inspection,
    issue_record,
    quality_status,
)
from .mocap_metadata import COMPACT_ACTION_ID_PATTERN, parse_mocap_capture_directory
from .session_layout import discover_mocap_directories

TIMESTAMP_REPORT_NAME = "timestamp_anomaly_detail_table.html"
REPORT_PREFIX = "const report="
REPORT_SUFFIX = "; const eventTypes="
PRIMITIVE_PATTERN = re.compile(r"(?<![A-Z0-9])(P\d+)(?![A-Z0-9])", re.IGNORECASE)
ROBOCAP_VIDEO_PATTERN = re.compile(
    r"^robocap_(?P<segment>.+?)_video_(?P<camera>.+)\.mp4$", re.IGNORECASE
)
UNASSIGNED_PRIMITIVE = "UNASSIGNED"
MOCAP_INSPECTION_SUFFIXES = frozenset({".trc", ".bvh", ".csv"})
UNCHECKED_REASON_ORDER = (
    "missing_mocap_directory",
    "missing_mocap_motion_file",
    "missing_valid_inspection",
)


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
    frame_anomalies: tuple[str, ...] = ()
    quality_issues: tuple[dict, ...] = ()
    quality_status: str = "unchecked"


@dataclass(frozen=True)
class SessionStatistic:
    primitive_id: str
    session_dir: Path
    unchecked_duration_s: float
    frame_difference_duration_s: float
    clean_duration_s: float
    segments: tuple[SegmentStatistic, ...]
    errors: tuple[str, ...]
    integrity_status: str = "unchecked"
    integrity_issues: tuple[str, ...] = ()
    quality_issues: tuple[dict, ...] = ()
    quality_status: str = "unchecked"

    @property
    def duration_s(self) -> float:
        return self.unchecked_duration_s + self.frame_difference_duration_s + self.clean_duration_s


@dataclass(frozen=True)
class PrimitiveStatistic:
    primitive_id: str
    unchecked_duration_s: float
    frame_difference_duration_s: float
    clean_duration_s: float
    sessions: tuple[SessionStatistic, ...]

    @property
    def duration_s(self) -> float:
        return self.unchecked_duration_s + self.frame_difference_duration_s + self.clean_duration_s


def session_has_clean_frame_counts(session: SessionStatistic) -> bool:
    return bool(session.segments) and all(segment.status == "clean" for segment in session.segments)


def session_is_error_free(session: SessionStatistic) -> bool:
    return (
        session_has_clean_frame_counts(session)
        and session.quality_status == "passed"
        and not session.errors
    )


def quality_durations(sessions) -> tuple[float, float, float]:
    """Classify whole Sessions, the normal batch uploader's unit of work."""
    durations = {"unchecked": 0.0, "error": 0.0, "passed": 0.0}
    for session in sessions:
        state = (
            "passed"
            if session_is_error_free(session)
            else "error"
            if session.quality_status == "error"
            else "unchecked"
        )
        durations[state] += session.duration_s
    return durations["unchecked"], durations["error"], durations["passed"]


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
    try:
        relative_parts = session_dir.resolve().relative_to(dataset_root.resolve()).parts
    except (OSError, ValueError):
        relative_parts = (session_dir.name,)

    mocap_dirs = discover_mocap_directories(session_dir)
    matches: set[str] = set()
    for part in relative_parts:
        matches.update(match.group(1).upper() for match in PRIMITIVE_PATTERN.finditer(part))
    for mocap_dir in mocap_dirs:
        matches.update(
            match.group(1).upper() for match in PRIMITIVE_PATTERN.finditer(mocap_dir.name)
        )
    if matches:
        return next(iter(matches)) if len(matches) == 1 else UNASSIGNED_PRIMITIVE

    mocap_matches = {
        metadata.action_id
        for mocap_dir in mocap_dirs
        if (metadata := parse_mocap_capture_directory(mocap_dir.name)) is not None
    }
    if mocap_matches:
        return next(iter(mocap_matches)) if len(mocap_matches) == 1 else UNASSIGNED_PRIMITIVE

    if len(relative_parts) < 2:
        return UNASSIGNED_PRIMITIVE
    action_directory = relative_parts[-2]
    return (
        action_directory.upper()
        if COMPACT_ACTION_ID_PATTERN.fullmatch(action_directory) is not None
        else UNASSIGNED_PRIMITIVE
    )


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


def report_has_frame_count_difference(payload: dict[str, object]) -> bool:
    anomalies = classify_frame_count_anomalies(payload)
    return anomalies is None or bool(anomalies)


def summarize_session(
    dataset_root: Path,
    session_dir: Path,
    ffprobe: str,
) -> SessionStatistic:
    segments: list[SegmentStatistic] = []
    errors: list[str] = []
    issues: list[dict] = []
    signatures = {}
    duration_by_status = {
        "unchecked": 0.0,
        "frame_difference": 0.0,
        "clean": 0.0,
    }

    references = discover_segment_references(session_dir)
    if not references:
        errors.append("no direct robocap_<segment>_video_<camera>.mp4 reference video")
        issues.append(
            issue_record("inspection_incomplete", detail=errors[-1], severity="unchecked")
        )

    for reference in references:
        duration_s, duration_error = probe_video_duration(reference.video_path, ffprobe)
        if duration_error:
            errors.append(f"{reference.video_path.name}: {duration_error}")

        frame_anomalies: tuple[str, ...] = ()
        segment_issues = []
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
                segment_issues = classify_inspection(payload)["issues"]
                integrity = payload.get("integrity") or {}
                if not isinstance(integrity, dict):
                    integrity = {}
                if integrity.get("source_signature"):
                    from .session_integrity import source_signature

                    for scope in (None, reference.segment):
                        if scope not in signatures:
                            signatures[scope] = source_signature(session_dir, segment=scope)
                    if integrity["source_signature"] not in {
                        signatures[None],
                        signatures[reference.segment],
                    }:
                        segment_issues.append(
                            issue_record(
                                "inspection_stale",
                                detail="源文件与报告签名不一致",
                                severity="unchecked",
                            )
                        )
                else:
                    segment_issues.append(
                        issue_record(
                            "inspection_incomplete",
                            detail="报告缺少源文件签名",
                            severity="unchecked",
                        )
                    )
                classified = classify_frame_count_anomalies(payload)
                if classified is None:
                    status = "unchecked"
                    detail = "inspection report has invalid frame-count fields"
                elif classified:
                    status = "frame_difference"
                    detail = "frame counts do not match n:ratio*(n+1):n+1"
                    frame_anomalies = classified
                else:
                    status = "clean"
                    detail = ""

        if duration_s is not None:
            duration_by_status[status] += duration_s
        if detail and status == "unchecked":
            errors.append(f"{reference.segment}: {detail}")
            segment_issues.append(
                issue_record(
                    "inspection_incomplete",
                    path=str(reference.report_path),
                    detail=detail,
                    severity="unchecked",
                )
            )
        issues.extend({**row, "segment": reference.segment} for row in segment_issues)
        segments.append(
            SegmentStatistic(
                segment=reference.segment,
                video_path=reference.video_path,
                report_path=reference.report_path,
                duration_s=duration_s,
                status=status,
                detail=detail,
                frame_anomalies=frame_anomalies,
                quality_issues=tuple(segment_issues),
                quality_status=quality_status(segment_issues),
            )
        )

    from .session_integrity import integrity_summary

    integrity_status, integrity_issues = integrity_summary(session_dir)
    return SessionStatistic(
        primitive_id=infer_action_primitive(dataset_root, session_dir),
        session_dir=session_dir,
        unchecked_duration_s=duration_by_status["unchecked"],
        frame_difference_duration_s=duration_by_status["frame_difference"],
        clean_duration_s=duration_by_status["clean"],
        segments=tuple(segments),
        errors=tuple(errors),
        integrity_status=integrity_status,
        integrity_issues=integrity_issues,
        quality_issues=tuple(issues),
        quality_status=quality_status(issues),
    )


def _primitive_sort_key(value: str) -> tuple[int, str, int, str]:
    match = re.fullmatch(r"([A-Z]+)(\d+)", value, re.IGNORECASE)
    if match is None:
        return 2, "", 0, value.casefold()
    prefix = match.group(1).upper()
    return (0 if prefix == "P" else 1), prefix, int(match.group(2)), value.casefold()


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
                unchecked_duration_s=sum(item.unchecked_duration_s for item in ordered),
                frame_difference_duration_s=sum(
                    item.frame_difference_duration_s for item in ordered
                ),
                clean_duration_s=sum(item.clean_duration_s for item in ordered),
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


def format_error_free_ratio(clean_duration_s: float, total_duration_s: float) -> str:
    ratio = clean_duration_s / total_duration_s if total_duration_s > 0 else 0.0
    return f"{ratio:.2%}"


def _markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def session_unchecked_reason(session: SessionStatistic) -> str:
    mocap_directories = discover_mocap_directories(session.session_dir)
    if not mocap_directories:
        return "missing_mocap_directory"
    if not any(
        path.is_file() and path.suffix.casefold() in MOCAP_INSPECTION_SUFFIXES
        for directory in mocap_directories
        for path in directory.rglob("*")
    ):
        return "missing_mocap_motion_file"
    return "missing_valid_inspection"


def unchecked_reason_labels(language: str) -> dict[str, str]:
    if language == "中文":
        return {
            "missing_mocap_directory": "没有mocap文件夹",
            "missing_mocap_motion_file": "mocap中没有TRC/BVH/CSV",
            "missing_valid_inspection": "有动捕文件但没有有效检查报告",
        }
    return {
        "missing_mocap_directory": "missing mocap directory",
        "missing_mocap_motion_file": "no TRC/BVH/CSV in mocap",
        "missing_valid_inspection": "motion file exists but no valid inspection report",
    }


def session_frame_anomaly_labels(
    session: SessionStatistic,
    *,
    language: str,
) -> tuple[str, ...]:
    is_chinese = language == "中文"
    labels = {
        "mocap_extra": "mocap多帧" if is_chinese else "mocap extra",
        "mocap_missing": "mocap少帧" if is_chinese else "mocap missing",
        "third_person_extra": "第三人称多帧" if is_chinese else "third-person extra",
        "third_person_missing": "第三人称少帧" if is_chinese else "third-person missing",
    }
    anomaly_codes = {anomaly for segment in session.segments for anomaly in segment.frame_anomalies}
    values = [labels[anomaly] for anomaly in FRAME_ANOMALY_ORDER if anomaly in anomaly_codes]
    if not session.segments or any(segment.status == "unchecked" for segment in session.segments):
        reason = session_unchecked_reason(session)
        values.insert(0, unchecked_reason_labels(language)[reason])
    if not values:
        values.append("帧数一致" if is_chinese else "frame-count matching")
    return tuple(values)


def duration_summary(
    unchecked_duration_s: float,
    error_duration_s: float,
    clean_duration_s: float,
    *,
    language: str,
) -> dict[str, str]:
    total_duration_s = unchecked_duration_s + error_duration_s + clean_duration_s
    labels = (
        ("全部", "无误", "有错误", "未检查或待确认")
        if language == "中文"
        else ("total", "error-free", "with errors", "unchecked or review")
    )
    return {
        labels[0]: format_duration(total_duration_s),
        labels[1]: format_duration(clean_duration_s),
        labels[2]: format_duration(error_duration_s),
        labels[3]: format_duration(unchecked_duration_s),
    }


def session_count_summary(
    sessions: tuple[SessionStatistic, ...],
    *,
    language: str,
) -> dict[str, int]:
    is_chinese = language == "中文"
    clean_count = sum(session_is_error_free(session) for session in sessions)
    error_count = sum(session.quality_status == "error" for session in sessions)
    unchecked_count = len(sessions) - clean_count - error_count
    total_count = len(sessions)
    labels = (
        ("全部", "无误", "有错误", "未检查或待确认")
        if is_chinese
        else ("total", "error-free", "with errors", "unchecked or review")
    )
    return {
        labels[0]: total_count,
        labels[1]: clean_count,
        labels[2]: error_count,
        labels[3]: unchecked_count,
    }


def session_anomaly_count_summary(
    sessions: tuple[SessionStatistic, ...],
    *,
    language: str,
) -> dict[str, int]:
    is_chinese = language == "中文"
    reason_labels = unchecked_reason_labels(language)
    error_labels = tuple(reason_labels[code] for code in UNCHECKED_REASON_ORDER) + (
        ("mocap多帧", "mocap少帧", "第三人称多帧", "第三人称少帧")
        if is_chinese
        else ("mocap extra", "mocap missing", "third-person extra", "third-person missing")
    )
    normal_label = "帧数一致" if is_chinese else "frame-count matching"
    counts: Counter[str] = Counter({label: 0 for label in error_labels})
    for session in sessions:
        counts.update(
            label
            for label in session_frame_anomaly_labels(session, language=language)
            if label != normal_label
        )
    return dict(counts)


def session_anomaly_duration_summary(
    sessions: tuple[SessionStatistic, ...],
    *,
    language: str,
) -> dict[str, str]:
    is_chinese = language == "中文"
    labels = {
        **unchecked_reason_labels(language),
        "mocap_extra": "mocap多帧" if is_chinese else "mocap extra",
        "mocap_missing": "mocap少帧" if is_chinese else "mocap missing",
        "third_person_extra": "第三人称多帧" if is_chinese else "third-person extra",
        "third_person_missing": "第三人称少帧" if is_chinese else "third-person missing",
    }
    durations = {code: 0.0 for code in labels}
    for session in sessions:
        unchecked_reason = session_unchecked_reason(session)
        for segment in session.segments:
            duration_s = segment.duration_s or 0.0
            if segment.status == "unchecked":
                durations[unchecked_reason] += duration_s
            for anomaly in segment.frame_anomalies:
                durations[anomaly] += duration_s
    return {labels[code]: format_duration(durations[code]) for code in labels}


def quality_count_summary(sessions):
    counts = Counter(session.quality_status for session in sessions)
    return {
        "total": len(sessions),
        **{state: counts[state] for state in ("passed", "error", "review", "unchecked")},
    }


def issue_label(code: str, language: str) -> str:
    frame_labels = {
        "mocap_extra": ("mocap多帧", "mocap extra"),
        "mocap_missing": ("mocap少帧", "mocap missing"),
        "third_person_extra": ("第三人称多帧", "third-person extra"),
        "third_person_missing": ("第三人称少帧", "third-person missing"),
    }
    if code in frame_labels:
        return frame_labels[code][0 if language == "中文" else 1]
    return ISSUE_LABELS[code][1] if language == "中文" else code.replace("_", " ")


def session_issue_labels(session: SessionStatistic, *, language: str) -> tuple[str, ...]:
    values = []
    if not session.segments or any(segment.status == "unchecked" for segment in session.segments):
        values.append(unchecked_reason_labels(language)[session_unchecked_reason(session)])
    codes = {row["code"] for row in session.quality_issues if row["severity"] != "info"}
    values.extend(issue_label(code, language) for code in ISSUE_LABELS if code in codes)
    if not values:
        values.append(
            ("无误" if language == "中文" else "error-free")
            if session_is_error_free(session)
            else issue_label("inspection_incomplete", language)
        )
    return tuple(values)


def session_issue_details(session, *, language):
    labels = session_issue_labels(session, language=language)
    evidence = dict.fromkeys(
        f"{issue_label(row['code'], language)}: {row['path']} {row['detail']}".strip()
        for row in session.quality_issues
        if row["severity"] != "info" and (row["path"] or row["detail"])
    )
    return list(labels) + list(evidence) + list(session.errors)


def issue_summaries(sessions, *, language):
    """Count each Session once per issue; time only affected Segments per issue."""
    reason_labels = unchecked_reason_labels(language)
    counts = {label: 0 for label in reason_labels.values()}
    durations = {label: 0.0 for label in reason_labels.values()}
    for code in ISSUE_LABELS:
        if code != "format_note":
            counts[issue_label(code, language)] = 0
            durations[issue_label(code, language)] = 0.0
    for session in sessions:
        for label in session_issue_labels(session, language=language):
            if label in counts:
                counts[label] += 1
        for segment in session.segments:
            codes = {row["code"] for row in segment.quality_issues if row["severity"] != "info"}
            labels = {issue_label(code, language) for code in codes}
            if segment.status == "unchecked":
                labels.add(reason_labels[session_unchecked_reason(session)])
            for label in labels:
                durations[label] += segment.duration_s or 0.0
    return counts, {label: format_duration(value) for label, value in durations.items()}


def quality_type_summary(sessions):
    rows = []
    for code, (group, label) in ISSUE_LABELS.items():
        affected = [
            session
            for session in sessions
            if any(issue["code"] == code for issue in session.quality_issues)
        ]
        if not affected:
            continue
        duration = sum(
            segment.duration_s or 0.0
            for session in affected
            for segment in session.segments
            if any(issue["code"] == code for issue in segment.quality_issues)
        )
        severity = next(
            issue["severity"] for issue in affected[0].quality_issues if issue["code"] == code
        )
        rows.append(
            {
                "code": code,
                "group": group,
                "label": label,
                "severity": severity,
                "sessions": len(affected),
                "duration_s": duration,
            }
        )
    return rows


def render_statistics_markdown(
    dataset_root: Path,
    primitives: tuple[PrimitiveStatistic, ...],
    *,
    language: str,
) -> str:
    is_chinese = language == "中文"
    session_count = sum(len(item.sessions) for item in primitives)
    all_sessions = tuple(session for item in primitives for session in item.sessions)
    unchecked_duration_s, error_duration_s, clean_duration_s = quality_durations(all_sessions)
    total_duration_s = unchecked_duration_s + error_duration_s + clean_duration_s
    overall_duration_counts = duration_summary(
        unchecked_duration_s,
        error_duration_s,
        clean_duration_s,
        language=language,
    )
    overall_session_counts = session_count_summary(all_sessions, language=language)
    overall_anomaly_counts, overall_anomaly_durations = issue_summaries(
        all_sessions, language=language
    )
    if is_chinese:
        lines = [
            "## 数据集统计",
            "",
            f"- 根目录：`{dataset_root}`",
            f"- Session：**{session_count}**",
            f"- 未检查或待确认时长：**{format_duration(unchecked_duration_s)}**",
            f"- 有错误时长：**{format_duration(error_duration_s)}**",
            f"- 无误时长：**{format_duration(clean_duration_s)}**",
            f"- 总时长：**{format_duration(total_duration_s)}**",
            (
                "- 时长统计：`"
                + json.dumps(overall_duration_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- Session 统计：`"
                + json.dumps(overall_session_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- 错误类型 Session 统计：`"
                + json.dumps(overall_anomaly_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- 错误类型时长统计：`"
                + json.dumps(overall_anomaly_durations, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            "",
            (
                "| 动作基元 | 未检查或待确认时长 | 有错误时长 | 无误时长 | 总时长 | 无误比率 | "
                "{Session: Session 时长} | "
                "{Session: [异常](帧数、时间戳、轨迹、文件及检查状态)} |"
            ),
            "|---|---:|---:|---:|---:|---:|---|---|",
        ]
    else:
        lines = [
            "## Dataset statistics",
            "",
            f"- Root: `{dataset_root}`",
            f"- Sessions: **{session_count}**",
            f"- Unchecked or review duration: **{format_duration(unchecked_duration_s)}**",
            f"- With-errors duration: **{format_duration(error_duration_s)}**",
            f"- Error-free duration: **{format_duration(clean_duration_s)}**",
            f"- Total duration: **{format_duration(total_duration_s)}**",
            (
                "- Duration summary: `"
                + json.dumps(overall_duration_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- Session counts: `"
                + json.dumps(overall_session_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- Error-type Session counts: `"
                + json.dumps(overall_anomaly_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- Error-type duration summary: `"
                + json.dumps(overall_anomaly_durations, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            "",
            (
                "| Primitive | Unchecked or review duration | With-errors duration | "
                "Error-free duration | Total duration | Error-free ratio | "
                "{Session: duration} | "
                "{Session: [anomalies](frames, timestamps, trajectories, files, inspection status)} |"
            ),
            "|---|---:|---:|---:|---:|---:|---|---|",
        ]

    for primitive in primitives:
        unchecked, with_errors, error_free = quality_durations(primitive.sessions)
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
        session_anomalies = {
            (
                item.session_dir.name
                if name_counts[item.session_dir.name] == 1
                else item.session_dir.as_posix()
            ): session_issue_details(item, language=language)
            for item in primitive.sessions
        }
        anomaly_mapping = json.dumps(
            session_anomalies,
            ensure_ascii=False,
            separators=(", ", ": "),
        )
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
                    format_duration(unchecked),
                    format_duration(with_errors),
                    format_duration(error_free),
                    format_duration(primitive.duration_s),
                    format_error_free_ratio(
                        error_free,
                        primitive.duration_s,
                    ),
                    f"`{_markdown_cell(mapping)}`",
                    f"`{_markdown_cell(anomaly_mapping)}`",
                ]
            )
            + " |"
        )

    if is_chinese:
        lines.extend(
            [
                "",
                (
                    "按整个 Session 判断无误，与批量正常上传的筛选单位一致：帧数、时间戳、轨迹、"
                    "文件结构及内容检查均通过，报告有效且源文件未变化，才计入无误时长。"
                    "确认存在错误的 Session 计入有错误时长；其余缺检查、旧报告或待确认的计入"
                    "未检查或待确认时长。三类互斥：未检查或待确认时长 + 有错误时长 + 无误时长 = 总时长。"
                    "每个 Segment 只用一条 Robocap 参考视频计时。四种差帧分类和新增文件等问题"
                    "共用上表的异常列表及分类统计；兼容格式说明不计错误。每种问题按 Session 去重，"
                    "分类时长只计受影响 Segment；同一 Segment 的多种问题可重复计入，分类时长不能相加。"
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                (
                    "Error-free requires the whole Session to pass frame, timestamp, trajectory and "
                    "file checks with current inspection evidence, matching the normal batch upload "
                    "selection. Confirmed defects count as with errors; incomplete, stale and review "
                    "results remain unchecked or review. These categories partition total duration. "
                    "Each Segment is timed from one Robocap reference video. All issue types share "
                    "the existing anomaly list and summaries. Compatible format notes are not errors. "
                    "Each issue counts a Session once and only times affected Segments; overlapping "
                    "issue durations cannot be added together."
                ),
            ]
        )
    return "\n".join(lines)
