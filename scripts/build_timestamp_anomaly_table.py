from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import rerun as rr

from robocap_rerun_tools.cli import normalize_time_values

TARGET_KINDS = {"third_person_video", "bvh", "trc", "csv", "xrs"}
VIDEO_FPS = 30.0
MOCAP_FPS = 240.0
VIDEO_NORMAL_MS = (32.5, 34.5)
MOCAP_NORMAL_MS = (3.5, 5.5)

EVENT_TYPES = [
    "timestamp_missing",
    "timestamp_too_short",
    "timestamp_too_long",
    "timestamp_duplicate",
    "timestamp_reversed",
    "frame_index_gap",
    "frame_index_duplicate",
    "frame_index_reversed",
]
EVENT_TYPE_INDEX = {name: index for index, name in enumerate(EVENT_TYPES)}


@dataclass(slots=True)
class Sample:
    source_row: int | None
    frame_index: int | float | None
    raw_timestamp: str
    timestamp_s: float | None
    missing_reason: str = ""


@dataclass(slots=True)
class SourceData:
    session: str
    relative_path: str
    kind: str
    stream: str
    frame_count: int | None
    declared_fps: float | None
    samples: list[Sample]
    expected_timestamp_count: int
    expected_missing_count: int
    expected_diff_count: int
    expected_skipped_diff_count: int


@dataclass(slots=True)
class Event:
    session: str
    file_index: int
    kind: str
    event_type: str
    previous: Sample | None
    current: Sample | None
    following: Sample | None
    diff_ms: float | None = None
    expected_min_ms: float | None = None
    expected_max_ms: float | None = None
    frame_span: float | None = None
    implied_frame_span: float | None = None
    estimated_extra_frames: int | None = None
    detail: str = ""
    event_id: int = 0


@dataclass(slots=True)
class SourceResult:
    source: SourceData
    interval_count: int
    skipped_interval_count: int
    abnormal_interval_count: int
    missing_count: int
    frame_index_issue_count: int
    min_diff_ms: float | None
    max_diff_ms: float | None
    events: list[Event] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a session overview and a row/frame-index level timestamp anomaly report."
        )
    )
    parser.add_argument("analysis_dir", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--extra-root", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--tsv-output",
        type=Path,
        help="Optionally write the complete point-level event table as TSV.",
    )
    return parser.parse_args()


def finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_frame_index(value: object) -> int | float | None:
    number = finite_float(value)
    if number is None:
        return None
    rounded = round(number)
    return int(rounded) if abs(number - rounded) < 1e-9 else number


def parse_int(value: str) -> int | None:
    number = finite_float(value)
    return int(number) if number is not None else None


def expectation(kind: str) -> tuple[float, float, float]:
    if kind == "third_person_video":
        return VIDEO_FPS, *VIDEO_NORMAL_MS
    return MOCAP_FPS, *MOCAP_NORMAL_MS


def session_directories(roots: Iterable[Path]) -> dict[str, Path]:
    sessions: dict[str, Path] = {}
    for root in roots:
        for path in sorted(root.resolve().iterdir()):
            if (
                not path.is_dir()
                or "_session" not in path.name.lower()
                or path.name.startswith("_")
            ):
                continue
            existing = sessions.get(path.name)
            if existing is not None and existing.resolve() != path.resolve():
                raise RuntimeError(
                    f"duplicate session directory {path.name}: {existing.resolve()} and {path.resolve()}"
                )
            sessions[path.name] = path.resolve()
    return sessions


def load_summary(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def missing_reason(raw: str, zero_is_missing: bool) -> str:
    stripped = raw.strip()
    if not stripped:
        return "empty"
    value = finite_float(stripped)
    if value is None:
        return "invalid"
    if zero_is_missing and value == 0:
        return "zero"
    return ""


def normalize_samples(samples: list[Sample], column_name: str) -> None:
    valid = [sample for sample in samples if not sample.missing_reason]
    raw_values = [float(sample.raw_timestamp) for sample in valid]
    normalized = normalize_time_values(raw_values, column_name)
    if len(normalized) != len(valid):
        raise RuntimeError("timestamp normalization changed the sample count")
    for sample, value in zip(valid, normalized, strict=True):
        sample.timestamp_s = value


def video_samples(path: Path) -> tuple[list[Sample], float | None]:
    timestamps_ns = list(rr.AssetVideo(path=path).read_frame_timestamps_nanos())
    return [
        Sample(None, index, str(timestamp_ns), timestamp_ns / 1e9)
        for index, timestamp_ns in enumerate(timestamps_ns)
    ], None


def bvh_samples(path: Path, frame_count: int | None) -> tuple[list[Sample], float | None]:
    frame_time: float | None = None
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            if line.strip().lower().startswith("frame time:"):
                frame_time = finite_float(line.split(":", 1)[1].strip())
                break
    if frame_count is None or frame_time is None:
        return [], None
    return [
        Sample(None, index, f"{index * frame_time:.12g}", index * frame_time)
        for index in range(frame_count)
    ], 1.0 / frame_time if frame_time > 0 else None


def trc_samples(path: Path) -> tuple[list[Sample], float | None]:
    samples: list[Sample] = []
    declared_fps: float | None = None
    found_header = False
    metadata_header: list[str] | None = None
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row in reader:
            cleaned = [cell.strip() for cell in row]
            if not found_header:
                if "DataRate" in cleaned:
                    metadata_header = cleaned
                    continue
                if metadata_header and cleaned:
                    try:
                        declared_fps = finite_float(cleaned[metadata_header.index("DataRate")])
                    except (ValueError, IndexError):
                        pass
                    metadata_header = None
                if len(cleaned) >= 2 and cleaned[0] == "Frame#" and cleaned[1] == "Time":
                    found_header = True
                continue
            frame_index = parse_frame_index(cleaned[0] if cleaned else None)
            if frame_index is None:
                continue
            raw = cleaned[1] if len(cleaned) > 1 else ""
            reason = missing_reason(raw, zero_is_missing=False)
            samples.append(
                Sample(
                    reader.line_num,
                    frame_index,
                    raw,
                    finite_float(raw) if not reason else None,
                    reason,
                )
            )
    return samples, declared_fps


def timestamp_header(row: list[str]) -> int | None:
    lowered = [cell.strip().lower() for cell in row]
    return lowered.index("timestamp") if "timestamp" in lowered else None


def frame_column(current: list[str], previous: list[str] | None) -> int:
    candidates = {"frame#", "frame", "frameindex", "frame_index", "frameid", "frame_id"}
    for row in (current, previous or []):
        lowered = [cell.strip().lower() for cell in row]
        for index, value in enumerate(lowered):
            if value in candidates:
                return index
    return 0


def hierarchical_samples(path: Path, delimiter: str) -> tuple[list[Sample], float | None]:
    samples: list[Sample] = []
    declared_fps: float | None = None
    timestamp_index: int | None = None
    frame_index_column = 0
    previous_row: list[str] | None = None
    metadata_header: list[str] | None = None
    zero_is_missing = True
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        for row in reader:
            cleaned = [cell.strip() for cell in row]
            if timestamp_index is None:
                if cleaned and cleaned[0].lower() == "numframes":
                    metadata_header = [cell.lower() for cell in cleaned]
                    previous_row = cleaned
                    continue
                if metadata_header and cleaned and finite_float(cleaned[0]) is not None:
                    for key in ("dataframerate", "datarate", "camerarate"):
                        if key in metadata_header:
                            declared_fps = finite_float(cleaned[metadata_header.index(key)])
                            break
                    metadata_header = None
                candidate = timestamp_header(cleaned)
                if candidate is not None:
                    timestamp_index = candidate
                    frame_index_column = frame_column(cleaned, previous_row)
                    previous_row = cleaned
                    continue
                previous_row = cleaned
                continue
            raw_frame = cleaned[frame_index_column] if frame_index_column < len(cleaned) else ""
            frame_value = parse_frame_index(raw_frame)
            if frame_value is None:
                continue
            raw_timestamp = cleaned[timestamp_index] if timestamp_index < len(cleaned) else ""
            reason = missing_reason(raw_timestamp, zero_is_missing=zero_is_missing)
            samples.append(Sample(reader.line_num, frame_value, raw_timestamp, None, reason))
    normalize_samples(samples, "timestamp")
    return samples, declared_fps


def source_data(
    session_dir: Path,
    summary: dict[str, str],
) -> SourceData:
    relative_path = summary["relative_path"]
    path = session_dir / Path(relative_path)
    kind = summary["kind"]
    frame_count = parse_int(summary["frame_count"])
    if kind == "third_person_video":
        samples, declared_fps = video_samples(path)
    elif kind == "bvh":
        samples, declared_fps = bvh_samples(path, frame_count)
    elif kind == "trc":
        samples, declared_fps = trc_samples(path)
    elif kind == "xrs":
        samples, declared_fps = hierarchical_samples(path, "\t")
    elif kind == "csv":
        samples, declared_fps = hierarchical_samples(path, ",")
    else:
        raise ValueError(f"unsupported target kind: {kind}")
    return SourceData(
        session=summary["session"],
        relative_path=relative_path,
        kind=kind,
        stream=summary["stream"],
        frame_count=frame_count,
        declared_fps=declared_fps,
        samples=samples,
        expected_timestamp_count=int(summary["timestamp_count"] or 0),
        expected_missing_count=int(summary["missing_timestamp_count"] or 0),
        expected_diff_count=int(summary["diff_count"] or 0),
        expected_skipped_diff_count=int(summary.get("skipped_diff_count") or 0),
    )


def frame_delta(previous: Sample | None, current: Sample | None) -> float | None:
    if previous is None or current is None:
        return None
    if previous.frame_index is None or current.frame_index is None:
        return None
    return float(current.frame_index) - float(previous.frame_index)


def neighboring(samples: list[Sample], index: int) -> tuple[Sample | None, Sample, Sample | None]:
    return (
        samples[index - 1] if index > 0 else None,
        samples[index],
        samples[index + 1] if index + 1 < len(samples) else None,
    )


def inspect_source(source: SourceData, file_index: int) -> SourceResult:
    samples = source.samples
    valid = [sample for sample in samples if sample.timestamp_s is not None]
    missing = [sample for sample in samples if sample.timestamp_s is None]
    adjacent_valid = [
        (index, previous, current)
        for index, (previous, current) in enumerate(pairwise(samples), start=1)
        if previous.timestamp_s is not None and current.timestamp_s is not None
    ]
    skipped_intervals = max(len(samples) - 1, 0) - len(adjacent_valid)
    if len(valid) != source.expected_timestamp_count:
        raise RuntimeError(
            f"valid timestamp mismatch for {source.session}/{source.relative_path}: "
            f"{len(valid)} != {source.expected_timestamp_count}"
        )
    if len(missing) != source.expected_missing_count:
        raise RuntimeError(
            f"missing timestamp mismatch for {source.session}/{source.relative_path}: "
            f"{len(missing)} != {source.expected_missing_count}"
        )
    if len(adjacent_valid) != source.expected_diff_count:
        raise RuntimeError(
            f"adjacent-row diff count mismatch for {source.session}/{source.relative_path}: "
            f"{len(adjacent_valid)} != {source.expected_diff_count}"
        )
    if skipped_intervals != source.expected_skipped_diff_count:
        raise RuntimeError(
            f"skipped diff count mismatch for {source.session}/{source.relative_path}: "
            f"{skipped_intervals} != {source.expected_skipped_diff_count}"
        )

    events: list[Event] = []
    for index, sample in enumerate(samples):
        if sample.timestamp_s is None:
            previous, current, following = neighboring(samples, index)
            events.append(
                Event(
                    source.session,
                    file_index,
                    source.kind,
                    "timestamp_missing",
                    previous,
                    current,
                    following,
                    detail=sample.missing_reason,
                )
            )

    frame_issue_count = 0
    for index, (previous, current) in enumerate(pairwise(samples), start=1):
        delta = frame_delta(previous, current)
        if delta is None or abs(delta - 1.0) < 1e-9:
            continue
        if abs(delta) < 1e-9:
            event_type = "frame_index_duplicate"
        elif delta < 0:
            event_type = "frame_index_reversed"
        else:
            event_type = "frame_index_gap"
        _, _, following = neighboring(samples, index)
        events.append(
            Event(
                source.session,
                file_index,
                source.kind,
                event_type,
                previous,
                current,
                following,
                frame_span=delta,
                estimated_extra_frames=max(0, round(delta) - 1) if delta > 1 else None,
            )
        )
        frame_issue_count += 1

    fps, expected_min, expected_max = expectation(source.kind)
    period_ms = 1000.0 / fps
    interval_diffs: list[float] = []
    abnormal_interval_count = 0
    for position, previous, current in adjacent_valid:
        assert previous.timestamp_s is not None and current.timestamp_s is not None
        diff_ms = (current.timestamp_s - previous.timestamp_s) * 1000.0
        interval_diffs.append(diff_ms)
        if expected_min <= diff_ms <= expected_max:
            continue
        if abs(diff_ms) < 1e-12:
            event_type = "timestamp_duplicate"
        elif diff_ms < 0:
            event_type = "timestamp_reversed"
        elif diff_ms < expected_min:
            event_type = "timestamp_too_short"
        else:
            event_type = "timestamp_too_long"
        following = samples[position + 1] if position + 1 < len(samples) else None
        span = frame_delta(previous, current)
        implied_span = diff_ms / period_ms
        reference_span = max(span or 1.0, 1.0)
        estimated_extra = (
            max(0, round(implied_span) - round(reference_span)) if diff_ms > expected_max else None
        )
        events.append(
            Event(
                source.session,
                file_index,
                source.kind,
                event_type,
                previous,
                current,
                following,
                diff_ms,
                expected_min,
                expected_max,
                span,
                implied_span,
                estimated_extra,
            )
        )
        abnormal_interval_count += 1

    events.sort(
        key=lambda event: (
            event.current.source_row
            if event.current and event.current.source_row is not None
            else -1,
            float(event.current.frame_index)
            if event.current and event.current.frame_index is not None
            else -1,
            EVENT_TYPE_INDEX[event.event_type],
        )
    )
    return SourceResult(
        source=source,
        interval_count=len(adjacent_valid),
        skipped_interval_count=skipped_intervals,
        abnormal_interval_count=abnormal_interval_count,
        missing_count=len(missing),
        frame_index_issue_count=frame_issue_count,
        min_diff_ms=min(interval_diffs) if interval_diffs else None,
        max_diff_ms=max(interval_diffs) if interval_diffs else None,
        events=events,
    )


def format_number(value: float | None, digits: int = 6) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def format_frame(value: float | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:g}"


def sample_value(sample: Sample | None, attribute: str) -> object | None:
    return getattr(sample, attribute) if sample is not None else None


def problem_text(event: Event) -> str:
    fps, _, _ = expectation(event.kind)
    if event.event_type == "timestamp_missing":
        reason = {
            "empty": "为空",
            "zero": "为 0（按 NOKOV 导出约定视为缺失）",
            "invalid": "不是有限数值",
        }.get(event.detail, "无效")
        return (
            f"当前行 Timestamp {reason}，涉及该行的相邻区间均不计算；"
            "不会跨过该行连接前后两个有效 Timestamp。"
        )
    if event.event_type == "frame_index_gap":
        missing = event.estimated_extra_frames or 0
        return (
            f"frame_index 从 {format_frame(sample_value(event.previous, 'frame_index'))} 跳到 "
            f"{format_frame(sample_value(event.current, 'frame_index'))}，中间缺少 {missing} 个序号；"
            "仅凭该文件无法区分采集漏帧与导出裁剪。"
        )
    if event.event_type == "frame_index_duplicate":
        return "相邻两行使用相同 frame_index，属于帧号重复。"
    if event.event_type == "frame_index_reversed":
        return "当前 frame_index 小于上一行，帧序发生倒退。"
    diff = event.diff_ms or 0.0
    expected = f"{event.expected_min_ms:.3f}-{event.expected_max_ms:.3f} ms"
    frame_span = event.frame_span if event.frame_span is not None else 1.0
    implied = event.implied_frame_span if event.implied_frame_span is not None else 0.0
    if event.event_type == "timestamp_duplicate":
        return f"相邻数据行的 Timestamp 完全相同，diff=0；{fps:g} FPS 正常单帧应为 {expected}。"
    if event.event_type == "timestamp_reversed":
        return f"Timestamp 倒退 {abs(diff):.6f} ms；{fps:g} FPS 正常单帧应为 {expected}。"
    if event.event_type == "timestamp_too_short":
        return (
            f"实际 diff={diff:.6f} ms，小于 {fps:g} FPS 正常单帧范围 {expected}；"
            f"时间仅相当于 {implied:.3f} 帧，而 frame_index 前进 {frame_span:g}，"
            "疑似 PTS 抖动、重复采样或时钟异常。"
        )
    extra = event.estimated_extra_frames or 0
    if frame_span > 1:
        return (
            f"实际 diff={diff:.6f} ms，大于 {fps:g} FPS 正常单帧范围 {expected}；"
            f"相邻数据行的 frame_index 跨 {frame_span:g} 帧，区间内可能存在采集漏帧；"
            f"时间约相当于 {implied:.3f} 帧，超出 frame_index 跨度约 {extra} 帧。"
        )
    return (
        f"实际 diff={diff:.6f} ms，大于 {fps:g} FPS 正常单帧范围 {expected}；"
        f"时间约相当于 {implied:.3f} 帧，但 frame_index 仅前进 {frame_span:g}，"
        f"疑似漏 {extra} 帧、PTS 抖动或时钟跳变。"
    )


def write_event_tsv(path: Path, session_results: dict[str, list[SourceResult]]) -> int:
    columns = [
        "event_id",
        "session",
        "format",
        "relative_path",
        "stream",
        "event_type",
        "previous_source_row",
        "current_source_row",
        "next_source_row",
        "previous_frame_index",
        "current_frame_index",
        "next_frame_index",
        "previous_timestamp_s",
        "current_timestamp_s",
        "next_timestamp_s",
        "diff_ms",
        "expected_min_ms",
        "expected_max_ms",
        "frame_index_span",
        "implied_frame_span",
        "estimated_extra_frames",
        "problem",
    ]
    event_id = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(columns)
        for session in sorted(session_results):
            for result in session_results[session]:
                source = result.source
                for event in result.events:
                    event_id += 1
                    event.event_id = event_id
                    writer.writerow(
                        [
                            event_id,
                            session,
                            source.kind,
                            source.relative_path,
                            source.stream,
                            event.event_type,
                            sample_value(event.previous, "source_row"),
                            sample_value(event.current, "source_row"),
                            sample_value(event.following, "source_row"),
                            format_frame(sample_value(event.previous, "frame_index")),
                            format_frame(sample_value(event.current, "frame_index")),
                            format_frame(sample_value(event.following, "frame_index")),
                            format_number(sample_value(event.previous, "timestamp_s"), 9),
                            format_number(sample_value(event.current, "timestamp_s"), 9),
                            format_number(sample_value(event.following, "timestamp_s"), 9),
                            format_number(event.diff_ms),
                            format_number(event.expected_min_ms, 3),
                            format_number(event.expected_max_ms, 3),
                            format_number(event.frame_span, 3),
                            format_number(event.implied_frame_span, 6),
                            event.estimated_extra_frames,
                            problem_text(event),
                        ]
                    )
    return event_id


def compact_counts(rows: Iterable[dict[str, str]]) -> str:
    counter = Counter(parse_int(row["frame_count"]) for row in rows if row["frame_count"])
    counter.pop(None, None)
    return (
        ", ".join(
            f"{value} x{count}" if count > 1 else str(value)
            for value, count in sorted(counter.items())
        )
        or "missing"
    )


def representative_count(rows: list[dict[str, str]], prefer_body: bool = False) -> int | None:
    candidates = rows
    if prefer_body:
        body = [row for row in rows if "body" in (row["relative_path"] + row["stream"]).lower()]
        if body:
            candidates = body
    counts = [parse_int(row["frame_count"]) for row in candidates]
    finite = [value for value in counts if value is not None]
    return Counter(finite).most_common(1)[0][0] if finite else None


def own_video_count(rows: list[dict[str, str]]) -> int | None:
    own = [row for row in rows if row["kind"] == "robocap_video"]
    exact_left = [
        row
        for row in own
        if re.search(r"video_left\.(mp4|mov|avi|mkv)$", row["relative_path"], re.IGNORECASE)
    ]
    return representative_count(exact_left or own)


def difference(actual: int | None, expected: int | None) -> int | None:
    return actual - expected if actual is not None and expected is not None else None


def mocap_timing_result(
    results: list[SourceResult], frame_count: int | None
) -> SourceResult | None:
    candidates = [result for result in results if result.source.kind != "third_person_video"]
    if not candidates:
        return None
    kind_priority = {"csv": 0, "xrs": 1, "trc": 2, "bvh": 3}

    def priority(result: SourceResult) -> tuple[object, ...]:
        source = result.source
        path = source.relative_path.lower()
        return (
            frame_count is not None and source.frame_count != frame_count,
            "body" not in path,
            kind_priority.get(source.kind, 99),
            "tracker" in path,
            path,
        )

    return min(candidates, key=priority)


def estimated_dropped_frames(result: SourceResult | None) -> int | None:
    if result is None:
        return None
    return sum(
        event.estimated_extra_frames or 0
        for event in result.events
        if event.event_type == "timestamp_too_long"
    )


def build_session_summaries(
    records: list[dict[str, str]],
    session_results: dict[str, list[SourceResult]],
) -> list[dict[str, object]]:
    records_by_session: dict[str, list[dict[str, str]]] = defaultdict(list)
    for record in records:
        records_by_session[record["session"]].append(record)
    summaries: list[dict[str, object]] = []
    for session in sorted(records_by_session):
        session_records = records_by_session[session]
        own_n = own_video_count(session_records)
        mocap_rows = [row for row in session_records if row["kind"] in {"bvh", "trc", "csv", "xrs"}]
        third_rows = [row for row in session_records if row["kind"] == "third_person_video"]
        mocap_actual = representative_count(mocap_rows, prefer_body=True)
        third_actual = representative_count(third_rows)
        expected_mocap = 8 * (own_n + 1) if own_n is not None else None
        expected_third = own_n + 1 if own_n is not None else None
        results = session_results.get(session, [])
        abnormal_intervals = sum(result.abnormal_interval_count for result in results)
        skipped_intervals = sum(result.skipped_interval_count for result in results)
        missing = sum(result.missing_count for result in results)
        frame_issues = sum(result.frame_index_issue_count for result in results)
        total_events = sum(len(result.events) for result in results)
        mocap_delta = difference(mocap_actual, expected_mocap)
        third_delta = difference(third_actual, expected_third)
        timing_result = mocap_timing_result(results, mocap_actual)
        estimated_mocap_dropped = estimated_dropped_frames(timing_result)
        expected_mocap_dropped = max(-mocap_delta, 0) if mocap_delta is not None else None
        mocap_drop_match = (
            estimated_mocap_dropped == expected_mocap_dropped
            if estimated_mocap_dropped is not None
            and expected_mocap_dropped is not None
            and (mocap_delta or 0) <= 0
            else False
            if mocap_delta is not None and mocap_delta > 0
            else None
        )
        if own_n is None or mocap_actual is None:
            frame_status = "unknown"
        elif mocap_delta != 0 or (third_actual is not None and third_delta != 0):
            frame_status = "fail"
        elif third_actual is None:
            frame_status = "partial"
        else:
            frame_status = "pass"
        summaries.append(
            {
                "session": session,
                "frameStatus": frame_status,
                "ownFrames": own_n,
                "mocapFrames": mocap_actual,
                "mocapCounts": compact_counts(mocap_rows),
                "expectedMocap": expected_mocap,
                "mocapDelta": mocap_delta,
                "mocapTimingSource": (
                    timing_result.source.relative_path if timing_result is not None else None
                ),
                "estimatedMocapDropped": estimated_mocap_dropped,
                "expectedMocapDropped": expected_mocap_dropped,
                "mocapDropMatch": mocap_drop_match,
                "thirdFrames": third_actual,
                "thirdCounts": compact_counts(third_rows),
                "expectedThird": expected_third,
                "thirdDelta": third_delta,
                "abnormalIntervals": abnormal_intervals,
                "skippedIntervals": skipped_intervals,
                "missingTimestamps": missing,
                "frameIndexIssues": frame_issues,
                "totalEvents": total_events,
            }
        )
    status_rank = {"pass": 0, "partial": 1, "fail": 2, "unknown": 3}
    summaries.sort(
        key=lambda item: (
            status_rank[str(item["frameStatus"])],
            abs(int(item["mocapDelta"])) if item["mocapDelta"] is not None else math.inf,
            str(item["session"]),
        )
    )
    return summaries


def compact_event(event: Event) -> list[object]:
    return [
        event.event_id,
        event.file_index,
        EVENT_TYPE_INDEX[event.event_type],
        sample_value(event.previous, "source_row"),
        sample_value(event.current, "source_row"),
        sample_value(event.following, "source_row"),
        sample_value(event.previous, "frame_index"),
        sample_value(event.current, "frame_index"),
        sample_value(event.following, "frame_index"),
        sample_value(event.previous, "timestamp_s"),
        sample_value(event.current, "timestamp_s"),
        sample_value(event.following, "timestamp_s"),
        event.diff_ms,
        event.expected_min_ms,
        event.expected_max_ms,
        event.frame_span,
        event.implied_frame_span,
        event.estimated_extra_frames,
        event.detail,
    ]


def build_session_payloads(
    session_results: dict[str, list[SourceResult]],
) -> dict[str, dict[str, object]]:
    payloads: dict[str, dict[str, object]] = {}
    for session, results in sorted(session_results.items()):
        files: list[dict[str, object]] = []
        events: list[list[object]] = []
        for file_index, result in enumerate(results):
            source = result.source
            files.append(
                {
                    "path": source.relative_path,
                    "kind": source.kind,
                    "stream": source.stream,
                    "frames": source.frame_count,
                    "declaredFps": source.declared_fps,
                    "intervals": result.interval_count,
                    "skippedIntervals": result.skipped_interval_count,
                    "abnormalIntervals": result.abnormal_interval_count,
                    "missing": result.missing_count,
                    "frameIssues": result.frame_index_issue_count,
                    "minDiffMs": result.min_diff_ms,
                    "maxDiffMs": result.max_diff_ms,
                }
            )
            for event in result.events:
                event.file_index = file_index
                events.append(compact_event(event))
        payloads[session] = {"files": files, "events": events}
    return payloads


def embedded_session_blocks(payloads: dict[str, dict[str, object]]) -> str:
    blocks: list[str] = []
    for session, payload in sorted(payloads.items()):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        block_id = html.escape(f"session-data-{session}", quote=True)
        blocks.append(f'<script type="application/json" id="{block_id}">{data}</script>')
    return "\n  ".join(blocks)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NOKOV 帧数与时间戳异常明细</title>
  <style>
    :root {
      --bg: #f3f5f7; --surface: #fff; --text: #17202a; --muted: #64717d;
      --border: #d5dce2; --strong: #aeb8c2; --header: #253445;
      --ok: #087f5b; --warn: #a35d00; --bad: #b42318;
      font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; color: var(--text); background: var(--bg); font-size: 13px; letter-spacing: 0; }
    button, input, select { font: inherit; letter-spacing: 0; }
    header { padding: 16px 20px 13px; color: #fff; background: var(--header); }
    h1 { margin: 0; font-size: 21px; }
    .subtitle { max-width: 1300px; margin-top: 7px; color: #dbe3ea; font-size: 11px; line-height: 1.6; }
    .stats { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 10px; font-variant-numeric: tabular-nums; }
    .stats strong { color: #fff; }
    main { padding: 16px 20px 34px; }
    .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 10px; }
    input, select { height: 34px; padding: 0 9px; color: var(--text); background: #fff; border: 1px solid var(--strong); border-radius: 4px; }
    input[type="search"] { width: 250px; }
    button { height: 34px; padding: 0 11px; color: #fff; background: #253445; border: 1px solid #253445; border-radius: 4px; cursor: pointer; }
    button:disabled { cursor: default; opacity: .4; }
    button.secondary { color: var(--text); background: #fff; border-color: var(--strong); }
    a { color: #0057a8; }
    .table-wrap { width: 100%; overflow: auto; background: var(--surface); border: 1px solid var(--strong); }
    table { width: 100%; min-width: 1450px; border-collapse: separate; border-spacing: 0; font-variant-numeric: tabular-nums; }
    th, td { padding: 8px 9px; text-align: right; vertical-align: top; border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); white-space: nowrap; }
    th { position: sticky; top: 0; z-index: 2; color: #34414e; background: #edf1f4; font-size: 11px; }
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
    tbody tr:hover td { background: #f7f9fa; }
    tbody tr.selected td { background: #eaf2f8; }
    .status { font-weight: 800; }
    .status.pass { color: var(--ok); }
    .status.partial { color: var(--warn); }
    .status.fail { color: var(--bad); }
    .status.unknown { color: var(--muted); }
    .negative, .alert { color: var(--bad); font-weight: 700; }
    .positive { color: var(--warn); font-weight: 700; }
    .match { color: var(--ok); font-weight: 700; }
    .mismatch { color: var(--bad); font-weight: 700; }
    .muted { color: var(--muted); }
    .section-title { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px; margin: 24px 0 9px; }
    .section-title h2 { margin: 0; font-size: 17px; }
    .section-title p { margin: 0; color: var(--muted); font-size: 11px; }
    #detailEmpty { padding: 34px 12px; color: var(--muted); text-align: center; background: #fff; border: 1px solid var(--border); }
    #detailRoot[hidden], #detailEmpty[hidden] { display: none; }
    .file-table { min-width: 1100px; }
    .file-table th:first-child, .file-table td:first-child { text-align: left; white-space: normal; overflow-wrap: anywhere; }
    .detail-toolbar { margin-top: 14px; }
    .pager { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; margin: 9px 0; }
    .pager-controls { display: flex; align-items: center; gap: 5px; }
    .pager input { width: 72px; text-align: center; }
    .event-table { min-width: 1900px; }
    .event-table th, .event-table td { font-size: 10px; }
    .event-table td.file { width: 260px; max-width: 260px; text-align: left; white-space: normal; overflow-wrap: anywhere; }
    .event-table td.problem { width: 520px; max-width: 520px; text-align: left; white-space: normal; line-height: 1.45; }
    .event-table tr.timestamp_missing td, .event-table tr.timestamp_too_long td,
    .event-table tr.timestamp_reversed td, .event-table tr.timestamp_duplicate td { background: #fff7f6; }
    .range { color: var(--muted); }
    @media (max-width: 760px) {
      header, main { padding-left: 12px; padding-right: 12px; }
      input[type="search"] { width: 100%; }
      .toolbar > * { flex: 1 1 auto; }
    }
  </style>
</head>
<body>
  <header>
    <h1>NOKOV 帧数与时间戳异常明细</h1>
    <div class="subtitle">
      固定验收基线：视频 30 FPS（单帧 32.5-34.5 ms，整数时间戳应以 33 ms 为主、34 ms 为辅）；
      动捕 240 FPS（单帧 3.5-5.5 ms，整数时间戳应以 4 ms 为主、5 ms 为辅）。
      diff 只在相邻数据行的 Timestamp 均有效时计算；缺失行前后的时间戳不会跨行相减。
      动捕推算丢帧 = max(round(diff / 4.166667 ms) - 1, 0)；每个 Session 仅使用主 Body CSV，
      避免把同一采集的 Tracker、TRC、BVH 重复累计，并与 8*(n+1)-动捕实际帧数做精确比较。
      异常明细和完整逐点数据均已嵌入此 HTML。
    </div>
    <div id="globalStats" class="stats"></div>
  </header>
  <main>
    <div class="toolbar">
      <input id="sessionSearch" type="search" placeholder="筛选 Session" aria-label="筛选 Session">
      <select id="frameStatus" aria-label="帧数关系">
        <option value="all">全部帧数状态</option><option value="pass">符合</option>
        <option value="partial">缺第三人称</option><option value="fail">不符合</option>
        <option value="unknown">无法判断</option>
      </select>
      <select id="sessionAnomaly" aria-label="异常状态">
        <option value="all">全部异常状态</option><option value="yes">有异常</option><option value="no">无异常</option>
      </select>
    </div>
    <div class="table-wrap">
      <table id="overviewTable">
        <thead><tr>
          <th>帧数关系</th><th>Session</th><th>Robocap n</th><th>动捕帧数</th>
          <th>8*(n+1)</th><th>动捕差异</th><th title="仅累计主 Body CSV 的相邻有效 Timestamp 异常">异常 diff 推算丢帧</th><th>动捕缺少帧数</th><th>是否对上</th>
          <th>第三人称</th><th>n+1</th><th>第三人称差异</th><th>异常 timestamp 间隔</th>
          <th>跳过 diff 区间</th><th>缺失 timestamp</th><th>frame_index 问题</th><th>逐点事件</th><th></th>
        </tr></thead>
        <tbody id="overviewRows"></tbody>
      </table>
    </div>

    <div class="section-title">
      <h2 id="detailTitle">异常点明细</h2>
      <p id="detailMeta">选择上方 Session 查看每一个异常点</p>
    </div>
    <div id="detailEmpty">尚未选择 Session。</div>
    <div id="detailRoot" hidden>
      <div class="table-wrap">
        <table class="file-table">
          <thead><tr><th>文件</th><th>格式</th><th>流</th><th>帧/行</th><th>声明 FPS</th><th>diff</th><th>跳过 diff</th><th>异常 diff</th><th>缺失 ts</th><th>frame_index 问题</th><th>diff 范围</th></tr></thead>
          <tbody id="fileRows"></tbody>
        </table>
      </div>
      <div class="toolbar detail-toolbar">
        <select id="kindFilter" aria-label="数据格式"><option value="all">全部格式</option></select>
        <select id="typeFilter" aria-label="异常类型"><option value="all">全部异常类型</option></select>
        <input id="eventSearch" type="search" placeholder="文件 / 行号 / frame_index" aria-label="筛选异常点">
        <select id="pageSize" aria-label="每页数量"><option>100</option><option>250</option><option>500</option><option>1000</option></select>
      </div>
      <div class="pager">
        <span id="pageMeta"></span>
        <div class="pager-controls">
          <button id="firstPage" class="secondary" type="button" title="第一页">|&lt;</button>
          <button id="prevPage" class="secondary" type="button" title="上一页">&lt;</button>
          <label>页 <input id="pageInput" type="number" min="1" value="1"></label>
          <span id="pageCount"></span>
          <button id="nextPage" class="secondary" type="button" title="下一页">&gt;</button>
          <button id="lastPage" class="secondary" type="button" title="最后一页">&gt;|</button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="event-table">
          <thead><tr>
            <th>ID</th><th>格式 / 文件</th><th>异常类型</th><th>上行 / 上帧</th><th>当前行 / 当前帧</th><th>下行 / 下帧</th>
            <th>上 Timestamp</th><th>当前 Timestamp</th><th>下 Timestamp</th><th>diff</th><th>正常范围</th><th>详细问题</th>
          </tr></thead>
          <tbody id="eventRows"></tbody>
        </table>
      </div>
      <div class="pager"><span id="pageMetaBottom"></span></div>
    </div>
  </main>
  __SESSION_DATA__
  <script>
    const report = __REPORT__;
    const eventTypes = __EVENT_TYPES__;
    const typeLabels = {
      timestamp_missing: 'Timestamp 缺失', timestamp_too_short: 'Timestamp 过短',
      timestamp_too_long: 'Timestamp 过长', timestamp_duplicate: 'Timestamp 重复',
      timestamp_reversed: 'Timestamp 倒退', frame_index_gap: 'frame_index 跳号',
      frame_index_duplicate: 'frame_index 重复', frame_index_reversed: 'frame_index 倒退',
    };
    const cache = new Map();
    let currentSession = null;
    let currentData = null;
    let filteredEvents = [];
    let currentPage = 1;

    function el(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = text;
      return node;
    }
    function number(value) { return value == null ? '-' : new Intl.NumberFormat('zh-CN').format(value); }
    function fixed(value, digits = 6) { return value == null ? '-' : Number(value).toFixed(digits); }
    function delta(value) { return value == null ? '-' : `${value > 0 ? '+' : ''}${number(value)}`; }
    function deltaClass(value) { return value == null || value === 0 ? '' : value < 0 ? 'negative' : 'positive'; }
    function position(row, frame) { return `${row == null ? 'row -' : `row ${row}`} / ${frame == null ? 'frame -' : `frame ${frame}`}`; }
    function timestamp(value) { return value == null ? '-' : `${Number(value).toFixed(9)} s`; }
    function eventType(event) { return eventTypes[event[2]]; }
    function eventFile(event) { return currentData.files[event[1]]; }

    function problem(event) {
      const type = eventType(event);
      const file = eventFile(event);
      const fps = file.kind === 'third_person_video' ? 30 : 240;
      const diff = event[12], low = event[13], high = event[14], span = event[15] == null ? 1 : event[15];
      const implied = event[16] == null ? 0 : event[16], extra = event[17] || 0;
      if (type === 'timestamp_missing') {
        const reason = { empty: '为空', zero: '为 0（按 NOKOV 导出约定视为缺失）', invalid: '不是有限数值' }[event[18]] || '无效';
        return `当前行 Timestamp ${reason}，涉及该行的相邻区间均不计算；不会跨过该行连接前后两个有效 Timestamp。`;
      }
      if (type === 'frame_index_gap') return `frame_index 从 ${event[6]} 跳到 ${event[7]}，中间缺少 ${extra} 个序号；仅凭该文件无法区分采集漏帧与导出裁剪。`;
      if (type === 'frame_index_duplicate') return '相邻两行使用相同 frame_index，属于帧号重复。';
      if (type === 'frame_index_reversed') return '当前 frame_index 小于上一行，帧序发生倒退。';
      const expected = `${fixed(low, 3)}-${fixed(high, 3)} ms`;
      if (type === 'timestamp_duplicate') return `相邻数据行的 Timestamp 完全相同，diff=0；${fps} FPS 正常单帧应为 ${expected}。`;
      if (type === 'timestamp_reversed') return `Timestamp 倒退 ${fixed(Math.abs(diff))} ms；${fps} FPS 正常单帧应为 ${expected}。`;
      if (type === 'timestamp_too_short') return `实际 diff=${fixed(diff)} ms，小于 ${fps} FPS 正常单帧范围 ${expected}；时间仅相当于 ${fixed(implied, 3)} 帧，而 frame_index 前进 ${span}，疑似 PTS 抖动、重复采样或时钟异常。`;
      if (span > 1) return `实际 diff=${fixed(diff)} ms，大于 ${fps} FPS 正常单帧范围 ${expected}；相邻数据行的 frame_index 跨 ${span} 帧，区间内可能存在采集漏帧；时间约相当于 ${fixed(implied, 3)} 帧，超出 frame_index 跨度约 ${extra} 帧。`;
      return `实际 diff=${fixed(diff)} ms，大于 ${fps} FPS 正常单帧范围 ${expected}；时间约相当于 ${fixed(implied, 3)} 帧，但 frame_index 仅前进 ${span}，疑似漏 ${extra} 帧、PTS 抖动或时钟跳变。`;
    }

    function renderStats() {
      const totals = report.sessions.reduce((out, item) => {
        out.events += item.totalEvents; out.intervals += item.abnormalIntervals;
        out.skipped += item.skippedIntervals;
        out.missing += item.missingTimestamps; out.frame += item.frameIndexIssues;
        if (item.totalEvents) out.sessions += 1;
        return out;
      }, { events: 0, intervals: 0, skipped: 0, missing: 0, frame: 0, sessions: 0 });
      const root = document.getElementById('globalStats');
      [
        ['Session', report.sessions.length], ['含异常 Session', totals.sessions],
        ['异常 timestamp 间隔', totals.intervals], ['跳过 diff 区间', totals.skipped],
        ['缺失 timestamp', totals.missing],
        ['frame_index 问题', totals.frame], ['逐点事件', totals.events],
      ].forEach(([label, value]) => { const span = el('span'); span.append(document.createTextNode(`${label}: `), el('strong', '', number(value))); root.appendChild(span); });
    }

    function statusLabel(status) {
      return { pass: 'OK', partial: 'PARTIAL', fail: 'FAIL', unknown: 'UNKNOWN' }[status];
    }
    function renderOverview() {
      const query = document.getElementById('sessionSearch').value.trim().toLowerCase();
      const status = document.getElementById('frameStatus').value;
      const anomaly = document.getElementById('sessionAnomaly').value;
      const root = document.getElementById('overviewRows'); root.replaceChildren();
      report.sessions.filter(item => {
        if (query && !item.session.toLowerCase().includes(query)) return false;
        if (status !== 'all' && item.frameStatus !== status) return false;
        if (anomaly === 'yes' && item.totalEvents === 0) return false;
        if (anomaly === 'no' && item.totalEvents !== 0) return false;
        return true;
      }).forEach(item => {
        const tr = document.createElement('tr');
        tr.dataset.session = item.session;
        if (item.session === currentSession) tr.classList.add('selected');
        const statusCell = el('td'); statusCell.appendChild(el('span', `status ${item.frameStatus}`, statusLabel(item.frameStatus))); tr.appendChild(statusCell);
        const sessionCell = el('td', '', item.session); tr.appendChild(sessionCell);
        const cells = [
          { value: number(item.ownFrames) },
          { value: number(item.mocapFrames), title: item.mocapCounts },
          { value: number(item.expectedMocap) },
          { value: delta(item.mocapDelta), className: deltaClass(item.mocapDelta) },
          { value: number(item.estimatedMocapDropped), title: item.mocapTimingSource || '' },
          { value: number(item.expectedMocapDropped) },
          { value: item.mocapDropMatch == null ? '-' : item.mocapDropMatch ? '是' : '否', className: item.mocapDropMatch == null ? '' : item.mocapDropMatch ? 'match' : 'mismatch' },
          { value: number(item.thirdFrames), title: item.thirdCounts },
          { value: number(item.expectedThird) },
          { value: delta(item.thirdDelta), className: deltaClass(item.thirdDelta) },
          { value: number(item.abnormalIntervals), className: item.abnormalIntervals ? 'alert' : '' },
          { value: number(item.skippedIntervals), className: item.skippedIntervals ? 'alert' : '' },
          { value: number(item.missingTimestamps), className: item.missingTimestamps ? 'alert' : '' },
          { value: number(item.frameIndexIssues), className: item.frameIndexIssues ? 'alert' : '' },
          { value: number(item.totalEvents), className: item.totalEvents ? 'alert' : '' },
        ];
        cells.forEach(cell => {
          const td = el('td', cell.className || '', cell.value);
          if (cell.title) td.title = cell.title;
          tr.appendChild(td);
        });
        const action = el('td'); const button = el('button', '', '查看'); button.type = 'button'; button.addEventListener('click', () => loadSession(item)); action.appendChild(button); tr.appendChild(action);
        root.appendChild(tr);
      });
    }

    function readEmbeddedSession(session) {
      const block = document.getElementById(`session-data-${session}`);
      if (!block) return null;
      const payload = JSON.parse(block.textContent);
      cache.set(session, payload);
      block.remove();
      return payload;
    }

    function loadSession(item) {
      currentSession = item.session; currentData = null;
      document.getElementById('detailTitle').textContent = `${item.session} / 异常点明细`;
      document.getElementById('detailMeta').textContent = '正在读取该 Session 的逐点数据...';
      document.getElementById('detailEmpty').hidden = false;
      document.getElementById('detailEmpty').textContent = '正在加载...';
      document.getElementById('detailRoot').hidden = true;
      renderOverview();
      if (cache.has(item.session)) { showSession(item, cache.get(item.session)); return; }
      const payload = readEmbeddedSession(item.session);
      if (payload) { showSession(item, payload); return; }
      document.getElementById('detailEmpty').textContent = `HTML 内没有 ${item.session} 的明细数据`;
    }

    function showSession(item, payload) {
      currentData = payload; currentPage = 1;
      document.getElementById('detailEmpty').hidden = true;
      document.getElementById('detailRoot').hidden = false;
      document.getElementById('detailMeta').textContent = `${number(payload.files.length)} 个源文件 / ${number(payload.events.length)} 个逐点事件`;
      renderFiles(); populateFilters(); applyEventFilters(); renderOverview();
      document.getElementById('detailTitle').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function renderFiles() {
      const root = document.getElementById('fileRows'); root.replaceChildren();
      currentData.files.forEach(file => {
        const tr = document.createElement('tr');
        const range = file.minDiffMs == null ? '-' : `${fixed(file.minDiffMs, 3)} - ${fixed(file.maxDiffMs, 3)} ms`;
        [file.path, file.kind, file.stream, number(file.frames), fixed(file.declaredFps, 3), number(file.intervals), number(file.skippedIntervals), number(file.abnormalIntervals), number(file.missing), number(file.frameIssues), range]
          .forEach((value, index) => tr.appendChild(el('td', index >= 6 && index <= 9 && value !== '0' ? 'alert' : '', value)));
        root.appendChild(tr);
      });
    }

    function populateFilters() {
      const kind = document.getElementById('kindFilter'); kind.replaceChildren(el('option', '', '全部格式')); kind.firstChild.value = 'all';
      [...new Set(currentData.files.map(file => file.kind))].sort().forEach(value => { const option = el('option', '', value); option.value = value; kind.appendChild(option); });
      const type = document.getElementById('typeFilter'); type.replaceChildren(el('option', '', '全部异常类型')); type.firstChild.value = 'all';
      [...new Set(currentData.events.map(event => eventType(event)))].sort().forEach(value => { const option = el('option', '', typeLabels[value]); option.value = value; type.appendChild(option); });
      document.getElementById('eventSearch').value = '';
    }

    function applyEventFilters() {
      if (!currentData) return;
      const kind = document.getElementById('kindFilter').value;
      const type = document.getElementById('typeFilter').value;
      const query = document.getElementById('eventSearch').value.trim().toLowerCase();
      filteredEvents = currentData.events.filter(event => {
        const file = eventFile(event), eventName = eventType(event);
        if (kind !== 'all' && file.kind !== kind) return false;
        if (type !== 'all' && eventName !== type) return false;
        if (!query) return true;
        const haystack = `${file.path} ${file.stream} ${event[3]} ${event[4]} ${event[5]} ${event[6]} ${event[7]} ${event[8]} ${typeLabels[eventName]}`.toLowerCase();
        return haystack.includes(query);
      });
      currentPage = 1; renderEvents();
    }

    function renderEvents() {
      const pageSize = Number(document.getElementById('pageSize').value);
      const pages = Math.max(1, Math.ceil(filteredEvents.length / pageSize));
      currentPage = Math.min(Math.max(1, currentPage), pages);
      const start = (currentPage - 1) * pageSize, end = Math.min(start + pageSize, filteredEvents.length);
      const root = document.getElementById('eventRows'); root.replaceChildren();
      const fragment = document.createDocumentFragment();
      filteredEvents.slice(start, end).forEach(event => {
        const file = eventFile(event), type = eventType(event), tr = document.createElement('tr'); tr.className = type;
        tr.appendChild(el('td', '', number(event[0])));
        const fileCell = el('td', 'file'); fileCell.append(el('strong', '', file.kind), document.createElement('br'), document.createTextNode(file.path)); tr.appendChild(fileCell);
        tr.appendChild(el('td', 'alert', typeLabels[type]));
        tr.appendChild(el('td', '', position(event[3], event[6])));
        tr.appendChild(el('td', '', position(event[4], event[7])));
        tr.appendChild(el('td', '', position(event[5], event[8])));
        tr.appendChild(el('td', '', timestamp(event[9])));
        tr.appendChild(el('td', '', timestamp(event[10])));
        tr.appendChild(el('td', '', timestamp(event[11])));
        tr.appendChild(el('td', 'alert', event[12] == null ? '-' : `${fixed(event[12])} ms`));
        tr.appendChild(el('td', 'range', event[13] == null ? '-' : `${fixed(event[13], 3)}-${fixed(event[14], 3)} ms`));
        tr.appendChild(el('td', 'problem', problem(event)));
        fragment.appendChild(tr);
      });
      root.appendChild(fragment);
      const rangeText = filteredEvents.length ? `${number(start + 1)}-${number(end)}` : '0';
      const meta = `显示 ${rangeText} / ${number(filteredEvents.length)} 个异常点`;
      document.getElementById('pageMeta').textContent = meta; document.getElementById('pageMetaBottom').textContent = meta;
      document.getElementById('pageCount').textContent = `/ ${pages}`; document.getElementById('pageInput').value = currentPage; document.getElementById('pageInput').max = pages;
      document.getElementById('firstPage').disabled = currentPage === 1; document.getElementById('prevPage').disabled = currentPage === 1;
      document.getElementById('nextPage').disabled = currentPage === pages; document.getElementById('lastPage').disabled = currentPage === pages;
    }

    ['sessionSearch', 'frameStatus', 'sessionAnomaly'].forEach(id => document.getElementById(id).addEventListener(id === 'sessionSearch' ? 'input' : 'change', renderOverview));
    ['kindFilter', 'typeFilter'].forEach(id => document.getElementById(id).addEventListener('change', applyEventFilters));
    document.getElementById('eventSearch').addEventListener('input', applyEventFilters);
    document.getElementById('pageSize').addEventListener('change', () => { currentPage = 1; renderEvents(); });
    document.getElementById('firstPage').addEventListener('click', () => { currentPage = 1; renderEvents(); });
    document.getElementById('prevPage').addEventListener('click', () => { currentPage -= 1; renderEvents(); });
    document.getElementById('nextPage').addEventListener('click', () => { currentPage += 1; renderEvents(); });
    document.getElementById('lastPage').addEventListener('click', () => { currentPage = Number(document.getElementById('pageInput').max); renderEvents(); });
    document.getElementById('pageInput').addEventListener('change', event => { currentPage = Number(event.target.value) || 1; renderEvents(); });
    renderStats(); renderOverview();
  </script>
</body>
</html>
"""


def write_html(
    path: Path,
    summaries: list[dict[str, object]],
    total_events: int,
    session_payloads: dict[str, dict[str, object]],
) -> None:
    payload = {
        "sessions": summaries,
        "totalEvents": total_events,
    }
    document = HTML_TEMPLATE.replace(
        "__REPORT__",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"),
        1,
    ).replace("__EVENT_TYPES__", json.dumps(EVENT_TYPES, ensure_ascii=False), 1)
    document = document.replace("__SESSION_DATA__", embedded_session_blocks(session_payloads), 1)
    path.write_text(document, encoding="utf-8", newline="")


def main() -> int:
    args = parse_args()
    analysis_dir = args.analysis_dir.resolve()
    roots = [args.root.resolve(), *(path.resolve() for path in args.extra_root)]
    sessions = session_directories(roots)
    records = [
        record
        for record in load_summary(analysis_dir / "all_data_frame_diff_summary.tsv")
        if record["session"] in sessions
    ]
    output = (
        args.output.resolve()
        if args.output is not None
        else analysis_dir / "timestamp_anomaly_detail_table.html"
    )
    targets = [
        record for record in records if record["kind"] in TARGET_KINDS and record["timestamp_basis"]
    ]
    session_results: dict[str, list[SourceResult]] = defaultdict(list)
    for record in targets:
        session = record["session"]
        if session not in sessions:
            raise RuntimeError(f"session source directory not found: {session}")
        source = source_data(sessions[session], record)
        result = inspect_source(source, len(session_results[session]))
        session_results[session].append(result)
        print(
            f"{session}\t{source.kind}\t{source.relative_path}\t"
            f"events={len(result.events)}\tabnormal_intervals={result.abnormal_interval_count}\t"
            f"skipped_intervals={result.skipped_interval_count}\tmissing={result.missing_count}"
        )

    for results in session_results.values():
        results.sort(key=lambda result: (result.source.kind, result.source.relative_path))
    session_payloads = build_session_payloads(session_results)
    total_events = sum(len(payload["events"]) for payload in session_payloads.values())
    if args.tsv_output is not None:
        tsv_output = args.tsv_output.resolve()
        tsv_events = write_event_tsv(tsv_output, session_results)
        if tsv_events != total_events:
            raise RuntimeError(
                f"TSV event count mismatch: expected {total_events}, wrote {tsv_events}"
            )
        print(f"tsv_output={tsv_output}")
    summaries = build_session_summaries(records, session_results)
    write_html(output, summaries, total_events, session_payloads)
    print(f"sessions={len(summaries)}")
    print(f"source_streams={sum(len(results) for results in session_results.values())}")
    print(f"events={total_events}")
    print(f"output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
