from __future__ import annotations

import csv
import json
import math
import sqlite3
import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import rerun as rr

if TYPE_CHECKING:
    from .cli import FrameRatioEstimate, StreamSummary


MOCAP_KINDS = {"bvh", "csv", "trc", "tsv", "xrs"}
SENSOR_KINDS = {"imu_acc", "imu_gyro", "mag", "sensor"}
VIDEO_EXPECTED_FPS = 30.0
FRAME_COUNT_RATIO = 8
SUPPORTED_FRAME_COUNT_RATIOS = (4, 8)
MOCAP_EXPECTED_FPS = VIDEO_EXPECTED_FPS * FRAME_COUNT_RATIO
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
class TimestampSample:
    source_row: int | None
    frame_index: int | float | None
    raw_timestamp: str
    timestamp_s: float | None
    missing_reason: str = ""


@dataclass(slots=True)
class TimestampEvent:
    event_type: str
    previous: TimestampSample | None
    current: TimestampSample
    following: TimestampSample | None
    diff_ms: float | None = None
    expected_min_ms: float | None = None
    expected_max_ms: float | None = None
    frame_span: float | None = None
    implied_frame_span: float | None = None
    estimated_dropped_frames: int | None = None
    detail: str = ""
    event_id: int = 0


@dataclass(slots=True)
class TimestampSourceResult:
    path: Path
    relative_path: str
    kind: str
    stream: str
    frame_count: int | None
    expected_fps: float
    samples: list[TimestampSample]
    diff_count: int
    skipped_diff_count: int
    abnormal_diff_count: int
    missing_timestamp_count: int
    frame_index_issue_count: int
    min_diff_ms: float | None
    max_diff_ms: float | None
    events: list[TimestampEvent] = field(default_factory=list)


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


def normalize_optional_times(values: list[float | None], column_name: str) -> list[float | None]:
    valid = [value for value in values if value is not None]
    if not valid:
        return [None] * len(values)
    lowered = column_name.lower()
    diffs = [second - first for first, second in pairwise(valid) if second > first]
    median_diff = statistics.median(diffs) if diffs else 0.0
    max_abs = max(abs(value) for value in valid)
    if lowered.endswith("_ns") or "nanosecond" in lowered or median_diff >= 1_000_000:
        divisor = 1e9
    elif median_diff >= 1_000:
        divisor = 1e6
    elif "timestamp" in lowered.replace(" ", "") and median_diff >= 1.0 or max_abs > 1e9 and median_diff >= 0.5:
        divisor = 1e3
    else:
        divisor = 1.0
    return [value / divisor if value is not None else None for value in values]


def missing_reason(raw: str, *, zero_is_missing: bool) -> str:
    stripped = raw.strip()
    if not stripped:
        return "empty"
    value = finite_float(stripped)
    if value is None:
        return "invalid"
    if zero_is_missing and value == 0:
        return "zero"
    return ""


def frame_delta(previous: TimestampSample | None, current: TimestampSample | None) -> float | None:
    if (
        previous is None
        or current is None
        or previous.frame_index is None
        or current.frame_index is None
    ):
        return None
    return float(current.frame_index) - float(previous.frame_index)


def neighboring(
    samples: list[TimestampSample], index: int
) -> tuple[TimestampSample | None, TimestampSample, TimestampSample | None]:
    return (
        samples[index - 1] if index > 0 else None,
        samples[index],
        samples[index + 1] if index + 1 < len(samples) else None,
    )


def expected_interval_range(fps: float) -> tuple[float, float, float]:
    period_ms = 1000.0 / fps
    return period_ms, max(0.0, math.floor(period_ms) - 0.5), math.ceil(period_ms) + 0.5


def validate_mocap_ratio(value: int) -> int:
    ratio = int(value)
    if ratio not in SUPPORTED_FRAME_COUNT_RATIOS:
        supported = ", ".join(str(item) for item in SUPPORTED_FRAME_COUNT_RATIOS)
        raise ValueError(f"Mocap inspection ratio must be one of: {supported}.")
    return ratio


def mocap_expected_fps(mocap_ratio: int) -> float:
    return VIDEO_EXPECTED_FPS * validate_mocap_ratio(mocap_ratio)


def round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def inspect_samples(
    path: Path,
    relative_path: str,
    kind: str,
    stream: str,
    frame_count: int | None,
    expected_fps: float,
    samples: list[TimestampSample],
) -> TimestampSourceResult:
    period_ms, expected_min, expected_max = expected_interval_range(expected_fps)
    events: list[TimestampEvent] = []
    for index, sample in enumerate(samples):
        if sample.timestamp_s is None:
            previous, current, following = neighboring(samples, index)
            reason = {
                "empty": "Timestamp is empty",
                "zero": "Timestamp is zero and treated as missing",
                "invalid": "Timestamp is not a finite number",
            }.get(sample.missing_reason, "Timestamp is invalid")
            events.append(
                TimestampEvent(
                    "timestamp_missing",
                    previous,
                    current,
                    following,
                    detail=(
                        f"{reason}; both adjacent intervals touching this row are skipped. "
                        "The valid timestamps on opposite sides are never subtracted."
                    ),
                )
            )

    frame_issue_count = 0
    for index, (previous, current) in enumerate(pairwise(samples), start=1):
        span = frame_delta(previous, current)
        if span is None or abs(span - 1.0) < 1e-9:
            continue
        if abs(span) < 1e-9:
            event_type = "frame_index_duplicate"
            detail = "Adjacent data rows use the same frame_index."
        elif span < 0:
            event_type = "frame_index_reversed"
            detail = "The current frame_index is lower than the previous data row."
        else:
            event_type = "frame_index_gap"
            detail = (
                f"frame_index advances by {span:g}; {max(0, round(span) - 1)} index values "
                "are absent between adjacent data rows."
            )
        events.append(
            TimestampEvent(
                event_type,
                previous,
                current,
                samples[index + 1] if index + 1 < len(samples) else None,
                frame_span=span,
                estimated_dropped_frames=max(0, round(span) - 1) if span > 1 else None,
                detail=detail,
            )
        )
        frame_issue_count += 1

    diffs_ms: list[float] = []
    abnormal_diff_count = 0
    valid_diff_count = 0
    skipped_diff_count = 0
    for index, (previous, current) in enumerate(pairwise(samples), start=1):
        if previous.timestamp_s is None or current.timestamp_s is None:
            skipped_diff_count += 1
            continue
        valid_diff_count += 1
        diff_ms = (current.timestamp_s - previous.timestamp_s) * 1000.0
        diffs_ms.append(diff_ms)
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
        span = frame_delta(previous, current)
        reference_span = max(span or 1.0, 1.0)
        implied_span = diff_ms / period_ms
        dropped = (
            max(0, round_half_up(implied_span) - round_half_up(reference_span))
            if diff_ms > expected_max
            else None
        )
        if event_type == "timestamp_too_long":
            detail = (
                f"Adjacent-row diff is {diff_ms:.6f} ms; expected {expected_min:.3f}-"
                f"{expected_max:.3f} ms at {expected_fps:g} FPS. This is approximately "
                f"{implied_span:.3f} frame periods and estimates {dropped or 0} dropped frames."
            )
        elif event_type == "timestamp_too_short":
            detail = (
                f"Adjacent-row diff is {diff_ms:.6f} ms; expected {expected_min:.3f}-"
                f"{expected_max:.3f} ms at {expected_fps:g} FPS. Suspected timestamp jitter, "
                "duplicate sampling, or clock error."
            )
        elif event_type == "timestamp_duplicate":
            detail = "Adjacent data rows have identical timestamps."
        else:
            detail = f"Timestamp moves backwards by {abs(diff_ms):.6f} ms."
        events.append(
            TimestampEvent(
                event_type,
                previous,
                current,
                samples[index + 1] if index + 1 < len(samples) else None,
                diff_ms=diff_ms,
                expected_min_ms=expected_min,
                expected_max_ms=expected_max,
                frame_span=span,
                implied_frame_span=implied_span,
                estimated_dropped_frames=dropped,
                detail=detail,
            )
        )
        abnormal_diff_count += 1

    events.sort(
        key=lambda event: (
            event.current.source_row if event.current.source_row is not None else -1,
            float(event.current.frame_index) if event.current.frame_index is not None else -1,
            EVENT_TYPE_INDEX[event.event_type],
        )
    )
    for event_id, event in enumerate(events):
        event.event_id = event_id
    return TimestampSourceResult(
        path=path,
        relative_path=relative_path,
        kind=kind,
        stream=stream,
        frame_count=frame_count,
        expected_fps=expected_fps,
        samples=samples,
        diff_count=valid_diff_count,
        skipped_diff_count=skipped_diff_count,
        abnormal_diff_count=abnormal_diff_count,
        missing_timestamp_count=sum(sample.timestamp_s is None for sample in samples),
        frame_index_issue_count=frame_issue_count,
        min_diff_ms=min(diffs_ms) if diffs_ms else None,
        max_diff_ms=max(diffs_ms) if diffs_ms else None,
        events=events,
    )


def video_samples(path: Path) -> list[TimestampSample]:
    timestamps_ns = list(rr.AssetVideo(path=path).read_frame_timestamps_nanos())
    return [
        TimestampSample(None, index, str(value), value / 1e9)
        for index, value in enumerate(timestamps_ns)
    ]


def bvh_samples(path: Path, frame_count: int | None) -> list[TimestampSample]:
    frame_time: float | None = None
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            if line.strip().lower().startswith("frame time:"):
                frame_time = finite_float(line.split(":", 1)[1].strip())
                break
    if frame_count is None or frame_time is None:
        return []
    return [
        TimestampSample(None, index, f"{index * frame_time:.12g}", index * frame_time)
        for index in range(frame_count)
    ]


def trc_samples(path: Path) -> list[TimestampSample]:
    samples: list[TimestampSample] = []
    header_found = False
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row in reader:
            cleaned = [cell.strip() for cell in row]
            if not header_found:
                if len(cleaned) >= 2 and cleaned[0] == "Frame#" and cleaned[1] == "Time":
                    header_found = True
                continue
            frame = parse_frame_index(cleaned[0] if cleaned else None)
            if frame is None:
                continue
            raw = cleaned[1] if len(cleaned) > 1 else ""
            reason = missing_reason(raw, zero_is_missing=False)
            samples.append(
                TimestampSample(
                    reader.line_num,
                    frame,
                    raw,
                    finite_float(raw) if not reason else None,
                    reason,
                )
            )
    return samples


def timestamp_column(row: list[str]) -> int | None:
    lowered = [cell.strip().lower() for cell in row]
    priorities = (
        "timestamp",
        "timestamps",
        "capture_time_ns",
        "capture_time",
        "time (seconds)",
        "time",
    )
    for name in priorities:
        if name in lowered:
            return lowered.index(name)
    return None


def frame_column(header: list[str], previous: list[str] | None) -> int:
    candidates = {"frame#", "frame", "frameindex", "frame_index", "frameid", "frame_id"}
    for row in (header, previous or []):
        lowered = [cell.strip().lower() for cell in row]
        for index, value in enumerate(lowered):
            if value in candidates:
                return index
    return 0


def delimited_samples(path: Path, delimiter: str) -> list[TimestampSample]:
    rows: list[tuple[int, list[str]]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        rows = [(reader.line_num, [cell.strip() for cell in row]) for row in reader]
    header_index: int | None = None
    time_index: int | None = None
    for index, (_, row) in enumerate(rows):
        candidate = timestamp_column(row)
        if candidate is not None:
            header_index = index
            time_index = candidate
            break
    if header_index is None or time_index is None:
        return []
    header = rows[header_index][1]
    previous = rows[header_index - 1][1] if header_index else None
    frame_index_column = frame_column(header, previous)
    column_name = header[time_index] if time_index < len(header) else "timestamp"
    zero_is_missing = "timestamp" in column_name.lower().replace(" ", "")
    pending: list[tuple[int, int | float, str, str, float | None]] = []
    for source_row, row in rows[header_index + 1 :]:
        frame = parse_frame_index(
            row[frame_index_column] if frame_index_column < len(row) else None
        )
        if frame is None:
            continue
        raw = row[time_index] if time_index < len(row) else ""
        reason = missing_reason(raw, zero_is_missing=zero_is_missing)
        pending.append((source_row, frame, raw, reason, finite_float(raw) if not reason else None))
    normalized = normalize_optional_times([item[4] for item in pending], column_name)
    return [
        TimestampSample(source_row, frame, raw, value, reason)
        for (source_row, frame, raw, reason, _), value in zip(pending, normalized, strict=True)
    ]


def named_time_column(columns: list[str]) -> str | None:
    lower_map = {name.lower().strip(): name for name in columns}
    for key in (
        "capture_time",
        "capture_time_ns",
        "timestamp",
        "timestamps",
        "time",
        "log_time",
        "log_time_ns",
    ):
        if key in lower_map:
            return lower_map[key]
    normalized = {
        name.lower().replace(" ", "").replace("_", "").replace("-", "").strip(): name
        for name in columns
    }
    for key in (
        "capturetimens",
        "capturetime",
        "timestampns",
        "timestamp",
        "timesec",
        "timeseconds",
        "time",
    ):
        if key in normalized:
            return normalized[key]
    return next(
        (
            name
            for name in columns
            if "time" in name.lower()
            and all(skip not in name.lower() for skip in ("runtime", "timeout"))
        ),
        None,
    )


def quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sqlite_samples(path: Path, table: str) -> list[TimestampSample]:
    with sqlite3.connect(path) as connection:
        quoted_table = quote_sqlite_identifier(table)
        column_rows = list(connection.execute(f"PRAGMA table_info({quoted_table})"))
        columns = [str(row[1]) for row in column_rows]
        time_column = named_time_column(columns)
        if time_column is None:
            return []
        frame_candidates = {
            "frame#",
            "frame",
            "frameindex",
            "frame_index",
            "frameid",
            "frame_id",
            "log_tick",
        }
        frame_column_name = next(
            (name for name in columns if name.lower().strip() in frame_candidates), None
        )
        primary_keys = [(int(row[5]), str(row[1])) for row in column_rows if int(row[5] or 0) > 0]
        order_by = (
            ", ".join(quote_sqlite_identifier(name) for _, name in sorted(primary_keys))
            if primary_keys
            else "rowid"
        )
        selected = quote_sqlite_identifier(time_column)
        if frame_column_name is not None:
            selected += f", {quote_sqlite_identifier(frame_column_name)}"
        rows = list(
            connection.execute(f"SELECT {selected} FROM {quoted_table} ORDER BY {order_by}")
        )

    pending: list[tuple[int, int | float, str, str, float | None]] = []
    for row_number, row in enumerate(rows, start=1):
        raw_value = row[0]
        raw = "" if raw_value is None else str(raw_value)
        reason = missing_reason(raw, zero_is_missing=False)
        frame = parse_frame_index(row[1]) if frame_column_name is not None else row_number - 1
        if frame is None:
            frame = row_number - 1
        pending.append(
            (row_number, frame, raw, reason, finite_float(raw_value) if not reason else None)
        )
    normalized = normalize_optional_times([item[4] for item in pending], time_column)
    return [
        TimestampSample(source_row, frame, raw, value, reason)
        for (source_row, frame, raw, reason, _), value in zip(pending, normalized, strict=True)
    ]


def relative_path(path: Path, session_dir: Path) -> str:
    try:
        return path.relative_to(session_dir).as_posix()
    except ValueError:
        return path.as_posix()


def target_summary(summary: StreamSummary) -> bool:
    return summary.kind == "video" or summary.kind.lower() in MOCAP_KINDS | SENSOR_KINDS


def expected_fps_for_summary(
    summary: StreamSummary, mocap_ratio: int = FRAME_COUNT_RATIO
) -> float:
    if summary.kind == "video":
        return VIDEO_EXPECTED_FPS
    if summary.kind.lower() in MOCAP_KINDS:
        return mocap_expected_fps(mocap_ratio)
    if summary.fps is not None and math.isfinite(summary.fps) and summary.fps > 0:
        return float(max(1, round(summary.fps)))
    return VIDEO_EXPECTED_FPS


def inspect_summary(
    session_dir: Path,
    summary: StreamSummary,
    mocap_ratio: int = FRAME_COUNT_RATIO,
) -> TimestampSourceResult:
    path = summary.path
    kind = summary.kind.lower()
    if kind == "video":
        samples = video_samples(path)
        report_kind = summary.stream or "video"
    elif kind == "bvh":
        samples = bvh_samples(path, summary.frame_count)
        report_kind = kind
    elif kind == "trc":
        samples = trc_samples(path)
        report_kind = kind
    elif kind == "xrs":
        samples = delimited_samples(path, "\t")
        report_kind = kind
    elif kind in {"csv", "tsv"}:
        delimiter = "\t" if kind == "tsv" else ","
        samples = delimited_samples(path, delimiter)
        report_kind = kind
    elif kind in SENSOR_KINDS:
        samples = sqlite_samples(path, summary.stream)
        report_kind = kind
    else:
        raise ValueError(f"unsupported timestamp report kind: {kind}")
    return inspect_samples(
        path,
        relative_path(path, session_dir),
        report_kind,
        summary.stream or path.stem,
        summary.frame_count,
        expected_fps_for_summary(summary, mocap_ratio),
        samples,
    )


def representative_count(
    summaries: Iterable[StreamSummary], *, prefer_body: bool = False
) -> int | None:
    rows = list(summaries)
    if prefer_body:
        body = [item for item in rows if "body" in item.path.name.lower()]
        if body:
            rows = body
    counts = [item.frame_count for item in rows if item.frame_count is not None]
    return Counter(counts).most_common(1)[0][0] if counts else None


def reference_robocap_frames(summaries: Iterable[StreamSummary]) -> int | None:
    videos = [
        item
        for item in summaries
        if item.kind == "video"
        and item.frame_count is not None
        and item.path.name.lower().startswith("robocap_")
    ]
    preferred = [item for item in videos if item.path.name.lower().endswith("video_left.mp4")]
    return (preferred or videos)[0].frame_count if videos else None


def primary_mocap_result(
    results: list[TimestampSourceResult], frame_count: int | None
) -> TimestampSourceResult | None:
    candidates = [result for result in results if result.kind != "third_person_video"]
    if not candidates:
        return None
    kind_priority = {"csv": 0, "xrs": 1, "tsv": 2, "trc": 3, "bvh": 4}
    return min(
        candidates,
        key=lambda result: (
            frame_count is not None and result.frame_count != frame_count,
            "body" not in result.path.name.lower(),
            kind_priority.get(result.kind, 99),
            "tracker" in result.path.name.lower(),
            result.relative_path.lower(),
        ),
    )


def estimated_dropped_frames(result: TimestampSourceResult | None) -> int | None:
    if result is None:
        return None
    return sum(
        event.estimated_dropped_frames or 0
        for event in result.events
        if event.event_type == "timestamp_too_long"
    )


def sample_value(sample: TimestampSample | None, attribute: str) -> object | None:
    return getattr(sample, attribute) if sample is not None else None


def compact_event(event: TimestampEvent, file_index: int) -> list[object]:
    return [
        event.event_id,
        file_index,
        EVENT_TYPE_INDEX[event.event_type],
        sample_value(event.previous, "source_row"),
        event.current.source_row,
        sample_value(event.following, "source_row"),
        sample_value(event.previous, "frame_index"),
        event.current.frame_index,
        sample_value(event.following, "frame_index"),
        sample_value(event.previous, "timestamp_s"),
        event.current.timestamp_s,
        sample_value(event.following, "timestamp_s"),
        event.diff_ms,
        event.expected_min_ms,
        event.expected_max_ms,
        event.estimated_dropped_frames,
        event.detail,
    ]


def report_payload(
    session_dir: Path,
    segment: str | None,
    summaries: list[StreamSummary],
    results: list[TimestampSourceResult],
    ratio_estimate: FrameRatioEstimate | None,
    mocap_ratio: int = FRAME_COUNT_RATIO,
) -> dict[str, object]:
    reference_n = reference_robocap_frames(summaries)
    ratio = validate_mocap_ratio(mocap_ratio)
    mocap_summaries = [item for item in summaries if item.kind.lower() in MOCAP_KINDS]
    third_summaries = [
        item for item in summaries if item.kind == "video" and item.stream == "third_person_video"
    ]
    mocap_frames = representative_count(mocap_summaries, prefer_body=True)
    third_frames = representative_count(third_summaries)
    expected_mocap = ratio * (reference_n + 1) if reference_n is not None else None
    expected_third = reference_n + 1 if reference_n is not None else None
    mocap_delta = (
        mocap_frames - expected_mocap
        if mocap_frames is not None and expected_mocap is not None
        else None
    )
    third_delta = (
        third_frames - expected_third
        if third_frames is not None and expected_third is not None
        else None
    )
    primary = primary_mocap_result(results, mocap_frames)
    expected_mocap_fps = mocap_expected_fps(ratio)
    estimated_dropped = estimated_dropped_frames(primary)
    expected_dropped = max(-mocap_delta, 0) if mocap_delta is not None else None
    dropped_match = (
        estimated_dropped == expected_dropped
        if estimated_dropped is not None
        and expected_dropped is not None
        and (mocap_delta or 0) <= 0
        else False
        if mocap_delta is not None and mocap_delta > 0
        else None
    )
    files: list[dict[str, object]] = []
    events: list[list[object]] = []
    for file_index, result in enumerate(results):
        files.append(
            {
                "path": result.relative_path,
                "kind": result.kind,
                "stream": result.stream,
                "frames": result.frame_count,
                "expectedFps": result.expected_fps,
                "diffs": result.diff_count,
                "skipped": result.skipped_diff_count,
                "abnormal": result.abnormal_diff_count,
                "missing": result.missing_timestamp_count,
                "frameIssues": result.frame_index_issue_count,
                "minDiffMs": result.min_diff_ms,
                "maxDiffMs": result.max_diff_ms,
            }
        )
        events.extend(compact_event(event, file_index) for event in result.events)
    return {
        "session": session_dir.name,
        "sessionPath": str(session_dir),
        "segment": segment or "auto/all",
        "ratio": ratio,
        "autoRatio": ratio_estimate.ratio if ratio_estimate is not None else None,
        "referenceFrames": reference_n,
        "mocapFrames": mocap_frames,
        "expectedMocap": expected_mocap,
        "mocapDelta": mocap_delta,
        "estimatedDropped": estimated_dropped,
        "expectedDropped": expected_dropped,
        "droppedMatch": dropped_match,
        "timingSource": primary.relative_path if primary is not None else None,
        "mocapExpectedFps": expected_mocap_fps,
        "thirdFrames": third_frames,
        "expectedThird": expected_third,
        "thirdDelta": third_delta,
        "abnormalDiffs": sum(result.abnormal_diff_count for result in results),
        "skippedDiffs": sum(result.skipped_diff_count for result in results),
        "missingTimestamps": sum(result.missing_timestamp_count for result in results),
        "frameIssues": sum(result.frame_index_issue_count for result in results),
        "files": files,
        "events": events,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Timestamp anomaly inspection</title>
  <style>
    :root { --bg:#f3f5f7; --surface:#fff; --text:#17202a; --muted:#66717d; --border:#d5dce2; --strong:#aeb8c2; --header:#253445; --ok:#087f5b; --warn:#a35d00; --bad:#b42318; font-family:Inter,"Segoe UI","Microsoft YaHei",sans-serif; }
    * { box-sizing:border-box; } body { margin:0; color:var(--text); background:var(--bg); font-size:13px; letter-spacing:0; }
    button,input,select { font:inherit; letter-spacing:0; } header { padding:16px 20px 14px; color:#fff; background:var(--header); }
    h1 { margin:0; font-size:21px; } .subtitle { max-width:1500px; margin-top:7px; color:#dbe3ea; font-size:11px; line-height:1.6; }
    .stats { display:flex; flex-wrap:wrap; gap:6px 18px; margin-top:10px; font-variant-numeric:tabular-nums; } .stats strong { color:#fff; }
    main { padding:16px 20px 28px; } .toolbar { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:10px; }
    input,select,button { min-height:34px; padding:6px 10px; border:1px solid var(--strong); border-radius:4px; background:#fff; }
    input[type="search"] { width:min(420px,100%); } button { color:#fff; background:#253445; cursor:pointer; }
    .table-wrap { width:100%; overflow:auto; background:var(--surface); border:1px solid var(--strong); }
    table { width:100%; min-width:1400px; border-collapse:separate; border-spacing:0; font-variant-numeric:tabular-nums; }
    th,td { padding:8px 9px; text-align:right; vertical-align:top; border-right:1px solid var(--border); border-bottom:1px solid var(--border); white-space:nowrap; }
    th { position:sticky; top:0; z-index:2; color:#34414e; background:#edf1f4; font-size:11px; } th:first-child,td:first-child { text-align:left; }
    .summary { margin-bottom:16px; } .summary table { min-width:1200px; } .match { color:var(--ok); font-weight:700; } .mismatch,.alert { color:var(--bad); font-weight:700; }
    h2 { margin:22px 0 9px; font-size:17px; } .file-table { min-width:1200px; } .file-table td:first-child { white-space:normal; overflow-wrap:anywhere; }
    .pager { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:8px; margin:9px 0; }
    .pager-controls { display:flex; align-items:center; gap:5px; } .pager input { width:72px; text-align:center; }
    .event-table { min-width:2050px; } .event-table th,.event-table td { font-size:10px; } .event-table td.file { width:260px; max-width:260px; text-align:left; white-space:normal; overflow-wrap:anywhere; }
    .event-table td.problem { width:500px; max-width:500px; text-align:left; white-space:normal; line-height:1.45; }
    .timestamp_missing td { background:#fff9ed; } .timestamp_too_long td,.timestamp_duplicate td,.timestamp_reversed td,.frame_index_gap td,.frame_index_duplicate td,.frame_index_reversed td { background:#fff5f4; }
    .timestamp_too_short td { background:#f4f8fc; } .muted { color:var(--muted); }
    @media (max-width:700px) { header,main { padding-left:12px; padding-right:12px; } input[type="search"] { width:100%; } .toolbar>* { flex:1 1 auto; } }
  </style>
</head>
<body>
  <header>
    <h1>时间戳异常检查 / Timestamp anomaly inspection</h1>
    <div class="subtitle" id="subtitle"></div>
    <div class="stats" id="stats"></div>
  </header>
  <main>
    <div class="summary table-wrap"><table><thead><tr><th>Session</th><th>Robocap n</th><th>动捕实际</th><th id="expectedMocapHeader">ratio*(n+1)</th><th>动捕差异</th><th>异常 diff 推算丢帧</th><th>动捕缺少帧数</th><th>是否对上</th><th>第三人称实际</th><th>n+1</th><th>第三人称差异</th></tr></thead><tbody id="summaryRow"></tbody></table></div>
    <h2>数据流 / Streams</h2>
    <div class="table-wrap"><table class="file-table"><thead><tr><th>文件</th><th>格式</th><th>流</th><th>帧/行</th><th>期望 FPS</th><th>有效 diff</th><th>跳过 diff</th><th>异常 diff</th><th>缺失 Timestamp</th><th>frame_index 问题</th><th>diff 范围</th></tr></thead><tbody id="fileRows"></tbody></table></div>
    <h2>逐点异常 / Point-level anomalies</h2>
    <div class="toolbar"><select id="kindFilter"><option value="all">全部格式</option></select><select id="typeFilter"><option value="all">全部异常类型</option></select><input id="search" type="search" placeholder="文件 / 行号 / frame_index"><select id="pageSize"><option>100</option><option>250</option><option>500</option><option>1000</option></select></div>
    <div class="pager"><span id="pageMeta"></span><div class="pager-controls"><button id="first">|&lt;</button><button id="prev">&lt;</button><label>页 <input id="page" type="number" min="1" value="1"></label><span id="pageCount"></span><button id="next">&gt;</button><button id="last">&gt;|</button></div></div>
    <div class="table-wrap"><table class="event-table"><thead><tr><th>ID</th><th>格式 / 文件</th><th>异常类型</th><th>上行 / 上帧</th><th>当前行 / 当前帧</th><th>下行 / 下帧</th><th>上 Timestamp</th><th>当前 Timestamp</th><th>下 Timestamp</th><th>diff</th><th>正常范围</th><th>推算丢帧</th><th>详细问题</th></tr></thead><tbody id="eventRows"></tbody></table></div>
  </main>
  <script>
    const report=__REPORT__; const eventTypes=__EVENT_TYPES__;
    const labels={timestamp_missing:'Timestamp 缺失',timestamp_too_short:'Timestamp 过短',timestamp_too_long:'Timestamp 过长',timestamp_duplicate:'Timestamp 重复',timestamp_reversed:'Timestamp 倒退',frame_index_gap:'frame_index 跳号',frame_index_duplicate:'frame_index 重复',frame_index_reversed:'frame_index 倒退'};
    let filtered=[],page=1; const nf=new Intl.NumberFormat('zh-CN');
    const el=(tag,cls,text)=>{const node=document.createElement(tag);if(cls)node.className=cls;if(text!=null)node.textContent=text;return node;};
    const number=value=>value==null?'-':nf.format(value); const fixed=(value,digits=6)=>value==null?'-':Number(value).toFixed(digits); const delta=value=>value==null?'-':`${value>0?'+':''}${number(value)}`;
    const position=(row,frame)=>`${row==null?'row -':`row ${row}`} / ${frame==null?'frame -':`frame ${frame}`}`; const timestamp=value=>value==null?'-':`${Number(value).toFixed(9)} s`;
    function renderHeader(){document.getElementById('subtitle').textContent=`${report.sessionPath} / ${report.segment}。检查基线：动捕 ${number(report.mocapExpectedFps)} FPS、视频 30 FPS、帧数关系 ${report.ratio}*(n+1)。diff 只计算相邻且 Timestamp 均有效的数据行；缺失行前后不会跨行相减。动捕推算丢帧 = max(round(diff / ${(1000/report.mocapExpectedFps).toFixed(6)} ms) - 1, 0)，仅累计主 Body 时间戳流 ${report.timingSource||'-'}。`;document.getElementById('expectedMocapHeader').textContent=`${report.ratio}*(n+1)`; const stats=[['异常 diff',report.abnormalDiffs],['跳过 diff',report.skippedDiffs],['缺失 Timestamp',report.missingTimestamps],['frame_index 问题',report.frameIssues],['逐点事件',report.events.length]]; const root=document.getElementById('stats');stats.forEach(([label,value])=>{const span=el('span');span.append(document.createTextNode(`${label}: `),el('strong','',number(value)));root.appendChild(span);});}
    function renderSummary(){const tr=document.createElement('tr');const match=report.droppedMatch==null?'-':report.droppedMatch?'是':'否';const cells=[[report.session,''],[number(report.referenceFrames),''],[number(report.mocapFrames),''],[number(report.expectedMocap),''],[delta(report.mocapDelta),report.mocapDelta?'alert':''],[number(report.estimatedDropped),''],[number(report.expectedDropped),''],[match,report.droppedMatch==null?'':report.droppedMatch?'match':'mismatch'],[number(report.thirdFrames),''],[number(report.expectedThird),''],[delta(report.thirdDelta),report.thirdDelta?'alert':'']];cells.forEach(([value,cls])=>tr.appendChild(el('td',cls,value)));document.getElementById('summaryRow').appendChild(tr);}
    function renderFiles(){const root=document.getElementById('fileRows');report.files.forEach(file=>{const tr=document.createElement('tr');const range=file.minDiffMs==null?'-':`${fixed(file.minDiffMs,3)} - ${fixed(file.maxDiffMs,3)} ms`;[file.path,file.kind,file.stream,number(file.frames),fixed(file.expectedFps,3),number(file.diffs),number(file.skipped),number(file.abnormal),number(file.missing),number(file.frameIssues),range].forEach((value,index)=>tr.appendChild(el('td',index>=6&&index<=9&&value!=='0'?'alert':'',value)));root.appendChild(tr);});}
    function eventType(event){return eventTypes[event[2]];} function eventFile(event){return report.files[event[1]];}
    function populateFilters(){const kind=document.getElementById('kindFilter');[...new Set(report.files.map(file=>file.kind))].sort().forEach(value=>{const option=el('option','',value);option.value=value;kind.appendChild(option);});const type=document.getElementById('typeFilter');[...new Set(report.events.map(eventType))].sort().forEach(value=>{const option=el('option','',labels[value]);option.value=value;type.appendChild(option);});}
    function applyFilters(){const kind=document.getElementById('kindFilter').value;const type=document.getElementById('typeFilter').value;const query=document.getElementById('search').value.trim().toLowerCase();filtered=report.events.filter(event=>{const file=eventFile(event);if(kind!=='all'&&file.kind!==kind)return false;if(type!=='all'&&eventType(event)!==type)return false;if(!query)return true;return `${file.path} ${event[3]} ${event[4]} ${event[5]} ${event[6]} ${event[7]} ${event[8]}`.toLowerCase().includes(query);});page=1;renderEvents();}
    function renderEvents(){const size=Number(document.getElementById('pageSize').value);const pages=Math.max(1,Math.ceil(filtered.length/size));page=Math.max(1,Math.min(page,pages));const start=(page-1)*size,end=Math.min(start+size,filtered.length);const root=document.getElementById('eventRows');root.replaceChildren();filtered.slice(start,end).forEach(event=>{const file=eventFile(event),type=eventType(event),tr=document.createElement('tr');tr.className=type;tr.appendChild(el('td','',number(event[0])));const fileCell=el('td','file');fileCell.append(el('strong','',file.kind),document.createElement('br'),document.createTextNode(file.path));tr.appendChild(fileCell);[labels[type],position(event[3],event[6]),position(event[4],event[7]),position(event[5],event[8]),timestamp(event[9]),timestamp(event[10]),timestamp(event[11]),event[12]==null?'-':`${fixed(event[12])} ms`,event[13]==null?'-':`${fixed(event[13],3)}-${fixed(event[14],3)} ms`,number(event[15])].forEach(value=>tr.appendChild(el('td','',value)));tr.appendChild(el('td','problem',event[16]));root.appendChild(tr);});document.getElementById('pageMeta').textContent=filtered.length?`显示 ${number(start+1)}-${number(end)} / ${number(filtered.length)} 个异常点`:'没有符合条件的异常点';document.getElementById('page').value=String(page);document.getElementById('page').max=String(pages);document.getElementById('pageCount').textContent=`/ ${number(pages)}`;}
    ['kindFilter','typeFilter','pageSize'].forEach(id=>document.getElementById(id).addEventListener('change',applyFilters));document.getElementById('search').addEventListener('input',applyFilters);document.getElementById('first').addEventListener('click',()=>{page=1;renderEvents();});document.getElementById('prev').addEventListener('click',()=>{page-=1;renderEvents();});document.getElementById('next').addEventListener('click',()=>{page+=1;renderEvents();});document.getElementById('last').addEventListener('click',()=>{page=Number(document.getElementById('page').max);renderEvents();});document.getElementById('page').addEventListener('change',event=>{page=Number(event.target.value)||1;renderEvents();});
    renderHeader();renderSummary();renderFiles();populateFilters();applyFilters();
  </script>
</body>
</html>
"""


def write_timestamp_anomaly_report(
    session_dir: Path,
    segment: str | None,
    summaries: list[StreamSummary],
    out_dir: Path,
    ratio_estimate: FrameRatioEstimate | None,
    mocap_ratio: int = FRAME_COUNT_RATIO,
) -> Path:
    session_dir = session_dir.resolve()
    ratio = validate_mocap_ratio(mocap_ratio)
    selected = [summary for summary in summaries if target_summary(summary)]
    results: list[TimestampSourceResult] = []
    for summary in selected:
        try:
            results.append(inspect_summary(session_dir, summary, ratio))
        except Exception as exc:  # noqa: BLE001 - keep one bad stream from hiding the report
            results.append(
                TimestampSourceResult(
                    path=summary.path,
                    relative_path=relative_path(summary.path, session_dir),
                    kind=summary.kind,
                    stream=summary.stream or summary.path.stem,
                    frame_count=summary.frame_count,
                    expected_fps=expected_fps_for_summary(summary, ratio),
                    samples=[],
                    diff_count=0,
                    skipped_diff_count=0,
                    abnormal_diff_count=0,
                    missing_timestamp_count=0,
                    frame_index_issue_count=0,
                    min_diff_ms=None,
                    max_diff_ms=None,
                    events=[],
                )
            )
            results[-1].stream = f"{results[-1].stream} (parse error: {exc})"
    results.sort(key=lambda result: (result.kind, result.relative_path.lower()))
    payload = report_payload(session_dir, segment, summaries, results, ratio_estimate, ratio)
    document = HTML_TEMPLATE.replace(
        "__REPORT__",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"),
        1,
    ).replace("__EVENT_TYPES__", json.dumps(EVENT_TYPES, ensure_ascii=False), 1)
    output = out_dir / "timestamp_anomaly_detail_table.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8", newline="")
    return output
