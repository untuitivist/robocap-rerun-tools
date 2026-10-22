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
        return (
            self.unchecked_duration_s
            + self.frame_difference_duration_s
            + self.clean_duration_s
        )


@dataclass(frozen=True)
class PrimitiveStatistic:
    primitive_id: str
    unchecked_duration_s: float
    frame_difference_duration_s: float
    clean_duration_s: float
    sessions: tuple[SessionStatistic, ...]

    @property
    def duration_s(self) -> float:
        return (
            self.unchecked_duration_s
            + self.frame_difference_duration_s
            + self.clean_duration_s
        )


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
        return (
            next(iter(mocap_matches))
            if len(mocap_matches) == 1
            else UNASSIGNED_PRIMITIVE
        )

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
        issues.append(issue_record("inspection_incomplete", detail=errors[-1], severity="unchecked"))

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
                        signatures[None], signatures[reference.segment],
                    }:
                        segment_issues.append(issue_record(
                            "inspection_stale", detail="源文件与报告签名不一致", severity="unchecked",
                        ))
                else:
                    segment_issues.append(issue_record(
                        "inspection_incomplete", detail="报告缺少源文件签名", severity="unchecked",
                    ))
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
            segment_issues.append(issue_record(
                "inspection_incomplete", path=str(reference.report_path), detail=detail,
                severity="unchecked",
            ))
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
    frame_difference_duration_s: float,
    clean_duration_s: float,
    *,
    language: str,
) -> dict[str, str]:
    total_duration_s = unchecked_duration_s + frame_difference_duration_s + clean_duration_s
    labels = (
        ("全部", "帧数一致", "差帧或未检查", "未检查")
        if language == "中文"
        else ("total", "frame-count matching", "mismatch or unchecked", "unchecked")
    )
    return {
        labels[0]: format_duration(total_duration_s),
        labels[1]: format_duration(clean_duration_s),
        labels[2]: format_duration(unchecked_duration_s + frame_difference_duration_s),
        labels[3]: format_duration(unchecked_duration_s),
    }


def session_count_summary(
    sessions: tuple[SessionStatistic, ...],
    *,
    language: str,
) -> dict[str, int]:
    is_chinese = language == "中文"
    clean_count = sum(session_has_clean_frame_counts(session) for session in sessions)
    unchecked_count = sum(
        not session.segments or any(segment.status == "unchecked" for segment in session.segments)
        for session in sessions
    )
    total_count = len(sessions)
    labels = (
        ("全部", "帧数一致", "差帧或未检查", "未检查")
        if is_chinese
        else ("total", "frame-count matching", "mismatch or unchecked", "unchecked")
    )
    return {
        labels[0]: total_count,
        labels[1]: clean_count,
        labels[2]: total_count - clean_count,
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
    return {"total": len(sessions), **{
        state: counts[state] for state in ("passed", "error", "review", "unchecked")
    }}


def quality_type_summary(sessions):
    rows = []
    for code, (group, label) in ISSUE_LABELS.items():
        affected = [session for session in sessions
                    if any(issue["code"] == code for issue in session.quality_issues)]
        if not affected:
            continue
        duration = sum(
            segment.duration_s or 0.0
            for session in affected for segment in session.segments
            if any(issue["code"] == code for issue in segment.quality_issues)
        )
        severity = next(issue["severity"] for issue in affected[0].quality_issues
                        if issue["code"] == code)
        rows.append({"code": code, "group": group, "label": label, "severity": severity,
                     "sessions": len(affected), "duration_s": duration})
    return rows


def render_quality_markdown(sessions, dataset_root):
    names = {"passed": "正常", "error": "确认异常", "review": "待确认", "unchecked": "未完成"}
    counts = quality_count_summary(sessions)
    lines = ["## 综合质量统计", "", "；".join(
        f"{label}：{counts[code]}" for code, label in (("total", "全部"), *names.items())
    ), "", "| 大类 | 问题 | 状态 | 受影响 Session | 受影响时长 |", "|---|---|---|---:|---:|"]
    states = {"error": "错误", "warning": "待确认", "unchecked": "未完成", "info": "说明"}
    for row in quality_type_summary(sessions):
        lines.append(f"| {row['group']} | {row['label']} | {states[row['severity']]} | "
                     f"{row['sessions']} | {format_duration(row['duration_s'])} |")
    lines += ["", ("同一 Session 可命中多类；每类按 Session 去重，分类数量与时长不能相加。"
              "旧报告、未完成检查不算正常；兼容表头仅作说明，采样间隔异常单列待确认。"),
              "", "| Session | 综合结论 | 问题及文件依据 |", "|---|---|---|"]
    for session in sessions:
        details = dict.fromkeys(
            f"{issue['label']}：{issue['path'] or issue.get('segment', 'Session')} {issue['detail']}"
            for issue in session.quality_issues
        )
        lines.append("| " + " | ".join(_markdown_cell(value) for value in (
            session.session_dir.relative_to(dataset_root).as_posix(),
            names[session.quality_status], "; ".join(details),
        )) + " |")
    return "\n".join(lines)


def render_statistics_markdown(
    dataset_root: Path,
    primitives: tuple[PrimitiveStatistic, ...],
    *,
    language: str,
) -> str:
    is_chinese = language == "中文"
    session_count = sum(len(item.sessions) for item in primitives)
    unchecked_duration_s = sum(item.unchecked_duration_s for item in primitives)
    frame_difference_duration_s = sum(
        item.frame_difference_duration_s for item in primitives
    )
    clean_duration_s = sum(item.clean_duration_s for item in primitives)
    total_duration_s = unchecked_duration_s + frame_difference_duration_s + clean_duration_s
    all_sessions = tuple(session for item in primitives for session in item.sessions)
    overall_duration_counts = duration_summary(
        unchecked_duration_s,
        frame_difference_duration_s,
        clean_duration_s,
        language=language,
    )
    overall_session_counts = session_count_summary(all_sessions, language=language)
    overall_anomaly_counts = session_anomaly_count_summary(all_sessions, language=language)
    overall_anomaly_durations = session_anomaly_duration_summary(
        all_sessions, language=language
    )
    if is_chinese:
        lines = [
            "## 帧数与时长统计（独立于综合质量）",
            "",
            f"- 根目录：`{dataset_root}`",
            f"- Session：**{session_count}**",
            f"- 未检查时长：**{format_duration(unchecked_duration_s)}**",
            f"- 差帧时长：**{format_duration(frame_difference_duration_s)}**",
            f"- 帧数一致时长：**{format_duration(clean_duration_s)}**",
            f"- 总时长：**{format_duration(total_duration_s)}**",
            (
                "- 时长统计：`"
                + json.dumps(overall_duration_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- 帧数分类 Session 统计：`"
                + json.dumps(overall_session_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- 帧数分类明细 Session 统计：`"
                + json.dumps(overall_anomaly_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- 帧数分类明细时长统计：`"
                + json.dumps(
                    overall_anomaly_durations, ensure_ascii=False, separators=(", ", ": ")
                )
                + "`"
            ),
            "",
            (
                "| 动作基元 | 未检查时长 | 差帧时长 | 帧数一致时长 | 总时长 | 帧数一致比率 | "
                "{Session: Session 时长} | "
                "{Session: [异常s](未检查原因, mocap多帧, mocap少帧, "
                "第三人称多帧, 第三人称少帧)} |"
            ),
            "|---|---:|---:|---:|---:|---:|---|---|",
        ]
    else:
        lines = [
            "## Frame counts and duration (separate from overall quality)",
            "",
            f"- Root: `{dataset_root}`",
            f"- Sessions: **{session_count}**",
            f"- Unchecked duration: **{format_duration(unchecked_duration_s)}**",
            (
                "- Frame-count-difference duration: "
                f"**{format_duration(frame_difference_duration_s)}**"
            ),
            f"- Frame-count matching duration: **{format_duration(clean_duration_s)}**",
            f"- Total duration: **{format_duration(total_duration_s)}**",
            (
                "- Duration summary: `"
                + json.dumps(overall_duration_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- Frame-count Session counts: `"
                + json.dumps(overall_session_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- Frame-category Session counts: `"
                + json.dumps(overall_anomaly_counts, ensure_ascii=False, separators=(", ", ": "))
                + "`"
            ),
            (
                "- Frame-category duration summary: `"
                + json.dumps(
                    overall_anomaly_durations, ensure_ascii=False, separators=(", ", ": ")
                )
                + "`"
            ),
            "",
            (
                "| Primitive | Unchecked duration | Frame-count-difference duration | "
                "Frame-count matching duration | Total duration | Matching ratio | "
                "{Session: duration} | "
                "{Session: [anomalies](unchecked reason, mocap extra, mocap missing, "
                "third-person extra, third-person missing)} |"
            ),
            "|---|---:|---:|---:|---:|---:|---|---|",
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
        session_anomalies = {
            (
                item.session_dir.name
                if name_counts[item.session_dir.name] == 1
                else item.session_dir.as_posix()
            ): list(session_frame_anomaly_labels(item, language=language))
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
                    format_duration(primitive.unchecked_duration_s),
                    format_duration(primitive.frame_difference_duration_s),
                    format_duration(primitive.clean_duration_s),
                    format_duration(primitive.duration_s),
                    format_error_free_ratio(
                        primitive.clean_duration_s,
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
                    "三类时长互斥且覆盖全部可计时时长：未检查包含缺少、无法读取或帧数字段无效的"
                    "检查报告；差帧包含帧数不满足 n:ratio*(n+1):(n+1) 的 Segment；帧数一致包含"
                    "检查报告有效且帧数关系一致的 Segment。因此未检查时长 + 差帧时长 + 帧数一致时长 "
                    "= 总时长。时间戳 diff、推算丢帧、缺失时间戳和 frame_index 等其他问题不改变"
                    "这项帧数分类。每个 Segment 只使用一条 Robocap "
                    "参考视频计时。异常列表按每个 Segment 的实际帧数与期望帧数比较后，在 Session "
                    "内取并集；未检查会进一步区分没有 mocap 文件夹、mocap 中没有 TRC/BVH/CSV，"
                    "以及已有动捕文件但没有有效检查报告。"
                    "错误类型时长按受影响 Segment 的 Robocap 参考视频时长累计；同一 Segment "
                    "若有多种错误会分别计入，所以各错误类型时长不能相加作为有错误时长。"
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                (
                    "The three duration categories are mutually exclusive and cover all measurable "
                    "duration. Unchecked means a missing, unreadable, or invalid inspection report; "
                    "frame-count difference means n:ratio*(n+1):(n+1) is not satisfied; frame-count matching "
                    "means a valid report with matching frame counts. Therefore unchecked + "
                    "frame-count difference + frame-count matching = total. Other timestamp, inferred-drop, "
                    "and frame-index findings do not change this frame-count classification. Each "
                    "segment is timed from one Robocap reference video. The "
                    "anomaly list compares actual and expected frame counts per Segment, then "
                    "takes their union per Session. Unchecked results distinguish a missing mocap "
                    "directory, no TRC/BVH/CSV in mocap, and motion files without a valid report."
                    " Error-type durations sum the Robocap reference-video duration of affected "
                    "segments. A segment with multiple errors contributes to each type, so these "
                    "type durations must not be added together as the with-errors duration."
                ),
            ]
        )
    lines.extend([
        "", "### 文件完整性（独立于帧数）" if is_chinese else "### File integrity (separate from frame counts)", "",
        "| Session | 完整性 / Integrity | 缺失、损坏或待确认 / Issues |", "|---|---|---|",
    ])
    for session in all_sessions:
        lines.append("| " + " | ".join(_markdown_cell(value) for value in (
            session.session_dir.relative_to(dataset_root).as_posix(), session.integrity_status,
            "; ".join(session.integrity_issues),
        )) + " |")
    return render_quality_markdown(all_sessions, dataset_root) + "\n\n" + "\n".join(lines)
