from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sqlite3
import statistics
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path

from .alignment import FrameAlignment, round_positive_ratio

VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv"}
TEXT_SUFFIXES = {".csv", ".tsv", ".trc", ".bvh", ".xrs"}
DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
GT_FPS_KINDS = {"bvh", "csv", "trc", "tsv", "xrs"}


@dataclass(frozen=True)
class StreamSummary:
    path: Path
    kind: str
    frame_count: int | None
    fps: float | None
    start_s: float | None
    end_s: float | None
    median_dt_ms: float | None
    min_dt_ms: float | None
    max_dt_ms: float | None
    abnormal_count: int
    abnormal_reason: str
    dropped_frames: int | None = None
    stream: str = ""
    time_basis: str = ""


@dataclass(frozen=True)
class FpsRecord:
    path: str
    kind: str
    source: str
    fps: float


@dataclass(frozen=True)
class FrameRatioEstimate:
    source: str
    report_path: Path | None
    gt_sample_count: int
    robocap_sample_count: int
    gt_fps_mean: float
    robocap_fps_mean: float
    gt_fps_rounded_10: int
    robocap_fps_rounded_10: int
    ratio_before_rounding: float
    ratio: int


@dataclass(frozen=True)
class FrameMissingDetail:
    path: Path
    kind: str
    expected_frames: int | None
    actual_frames: int | None
    missing_ids: tuple[int, ...]
    missing_timestamp_ids: tuple[int, ...] = ()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def open_csv(path: Path):
    return path.open("r", encoding="utf-8-sig", errors="replace", newline="")


def run_json(command: list[str]) -> dict:
    proc = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(proc.stdout)


def resolve_ffprobe(ffprobe: str = "ffprobe", ffmpeg: str | None = None) -> str:
    explicit = Path(ffprobe)
    if explicit.name != ffprobe and explicit.exists():
        return str(explicit)
    found = shutil.which(ffprobe)
    if found:
        return found
    if ffmpeg:
        ffmpeg_path = Path(ffmpeg)
        if ffmpeg_path.name == ffmpeg:
            resolved_ffmpeg = shutil.which(ffmpeg)
            ffmpeg_path = Path(resolved_ffmpeg) if resolved_ffmpeg else ffmpeg_path
        sibling = ffmpeg_path.with_name("ffprobe.exe")
        if sibling.exists():
            return str(sibling)
        sibling = ffmpeg_path.with_name("ffprobe")
        if sibling.exists():
            return str(sibling)
    found_ffmpeg = shutil.which("ffmpeg")
    if found_ffmpeg:
        sibling = Path(found_ffmpeg).with_name("ffprobe.exe")
        if sibling.exists():
            return str(sibling)
    return ffprobe


def ffprobe_video(path: Path, ffprobe: str) -> tuple[dict | None, str | None]:
    try:
        return run_json(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_frames,r_frame_rate,avg_frame_rate,duration:format=duration:format_tags=comment",
                "-of",
                "json",
                str(path),
            ]
        ), None
    except FileNotFoundError as exc:
        return None, f"ffprobe executable not found: {ffprobe} ({exc})"
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        return None, f"ffprobe exited {exc.returncode}: {stderr or exc}"
    except json.JSONDecodeError as exc:
        return None, f"ffprobe returned invalid JSON: {exc}"


def parse_ratio(value: str | None) -> tuple[int, int] | None:
    if not value or value == "0/0":
        return None
    num_s, den_s = value.split("/", 1)
    num = float(num_s)
    den = float(den_s)
    if den == 0:
        return None
    return int(num), int(den)


def ratio_to_float(value: str | None) -> float | None:
    pair = parse_ratio(value)
    if pair is None:
        return None
    num, den = pair
    return num / den


def video_stream_name(path: Path) -> str:
    name = path.name.lower()
    parent_names = {parent.name.lower() for parent in path.parents}
    if name.startswith("robocap_"):
        return "robocap_video"
    if name.startswith("robowrist_") or any(name.startswith("robowrist_") for name in parent_names):
        return "robowrist_video"
    return "third_person_video"


def video_capture_start_s(data: dict | None) -> float | None:
    if not data:
        return None
    comment = (data.get("format") or {}).get("tags", {}).get("comment")
    try:
        return float(comment) / 1e6
    except (TypeError, ValueError):
        return None


def with_summary_details(
    summary: StreamSummary,
    *,
    stream: str | None = None,
    time_basis: str | None = None,
    reasons: Iterable[str] = (),
    abnormal_count_delta: int = 0,
) -> StreamSummary:
    reason_parts = [summary.abnormal_reason] if summary.abnormal_reason else []
    reason_parts.extend(reason for reason in reasons if reason)
    return replace(
        summary,
        abnormal_count=summary.abnormal_count + abnormal_count_delta,
        abnormal_reason="; ".join(reason_parts),
        stream=summary.stream if stream is None else stream,
        time_basis=summary.time_basis if time_basis is None else time_basis,
    )


def ffprobe_video_summary(path: Path, data: dict, capture_start_s: float | None) -> StreamSummary:
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    fps = ratio_to_float(stream.get("avg_frame_rate")) or ratio_to_float(stream.get("r_frame_rate"))
    duration = float(stream.get("duration") or fmt.get("duration") or 0.0) or None
    frame_count = int(stream["nb_frames"]) if str(stream.get("nb_frames", "")).isdigit() else None
    if frame_count is None and duration and fps:
        frame_count = round(duration * fps)
    median_dt = 1000.0 / fps if fps else None
    start_s = capture_start_s if capture_start_s is not None else 0.0
    end_s = (
        start_s + duration
        if duration is not None
        else (
            start_s + (frame_count - 1) / fps if frame_count and fps and frame_count > 0 else None
        )
    )
    return StreamSummary(
        path,
        "video",
        frame_count,
        fps,
        start_s,
        end_s,
        median_dt,
        median_dt,
        median_dt,
        0,
        "",
    )


def video_summary(path: Path, ffprobe: str) -> StreamSummary:
    data, ffprobe_error = ffprobe_video(path, ffprobe)
    capture_start_s = video_capture_start_s(data)
    stream_name = video_stream_name(path)
    time_basis = "capture_time (MP4 comment us)" if capture_start_s is not None else "media_time"
    try:
        import rerun as rr

        frame_timestamps_ns = list(rr.AssetVideo(path=path).read_frame_timestamps_nanos())
    except Exception as exc:  # noqa: BLE001 - Rerun codec backends raise multiple exception types
        if not data:
            return StreamSummary(
                path,
                "video",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                f"{ffprobe_error}; Rerun video probe failed: {exc}",
                stream=stream_name,
                time_basis=time_basis,
            )
        fallback = ffprobe_video_summary(path, data, capture_start_s)
        reasons = [f"Rerun frame timestamp probe failed: {exc}; interval anomalies unavailable"]
        if capture_start_s is None:
            reasons.append("missing MP4 comment capture timestamp; times are media-relative")
        return with_summary_details(
            fallback,
            stream=stream_name,
            time_basis=time_basis,
            reasons=reasons,
            abnormal_count_delta=1,
        )

    offset_s = capture_start_s or 0.0
    times_s = [offset_s + float(value) / 1e9 for value in frame_timestamps_ns]
    summary = summarize_times(path, "video", times_s)
    reasons: list[str] = []
    abnormal_count_delta = 0
    if ffprobe_error:
        reasons.append(ffprobe_error)
    if capture_start_s is None:
        reasons.append("missing MP4 comment capture timestamp; times are media-relative")
    if data:
        metadata_summary = ffprobe_video_summary(path, data, capture_start_s)
        if metadata_summary.fps is not None:
            summary = replace(summary, fps=metadata_summary.fps)
        if (
            metadata_summary.frame_count is not None
            and metadata_summary.frame_count != summary.frame_count
        ):
            reasons.append(
                f"ffprobe frame count {metadata_summary.frame_count} differs from decoded timestamp "
                f"count {summary.frame_count}"
            )
            abnormal_count_delta += 1
    return with_summary_details(
        summary,
        stream=stream_name,
        time_basis=time_basis,
        reasons=reasons,
        abnormal_count_delta=abnormal_count_delta,
    )


def numeric(values: Iterable[str]) -> list[float]:
    out: list[float] = []
    for value in values:
        try:
            if value != "":
                out.append(float(value))
        except ValueError:
            continue
    return out


def summarize_times(path: Path, kind: str, times_s: list[float]) -> StreamSummary:
    times_s = [t for t in times_s if math.isfinite(t)]
    if len(times_s) < 2:
        reason = "no timestamps" if not times_s else "only one timestamp; interval/FPS unavailable"
        return StreamSummary(
            path,
            kind,
            len(times_s),
            None,
            times_s[0] if times_s else None,
            times_s[-1] if times_s else None,
            None,
            None,
            None,
            1,
            reason,
        )
    all_diffs_ms = [(b - a) * 1000.0 for a, b in pairwise(times_s)]
    non_positive = [diff for diff in all_diffs_ms if diff <= 0]
    diffs_ms = [diff for diff in all_diffs_ms if diff > 0]
    if not diffs_ms:
        return StreamSummary(
            path,
            kind,
            len(times_s),
            None,
            times_s[0],
            times_s[-1],
            None,
            None,
            None,
            len(non_positive),
            f"{len(non_positive)} non-increasing timestamp intervals",
        )
    med = statistics.median(diffs_ms)
    fps = 1000.0 / med if med > 0 else None
    tolerance = max(2.0, med * 0.25)
    dropped_frame_intervals = []
    other_abnormal = []
    for d in diffs_ms:
        if d <= med * 1.5:
            if abs(d - med) > tolerance:
                other_abnormal.append(d)
            continue
        multiples = round(d / med)
        if multiples >= 2 and abs(d - multiples * med) <= tolerance:
            dropped_frame_intervals.append(d)
        else:
            other_abnormal.append(d)

    reasons = []
    if non_positive:
        reasons.append(f"{len(non_positive)} non-increasing timestamp intervals")
    if dropped_frame_intervals:
        dropped_count = len(dropped_frame_intervals)
        estimated_dropped = sum(max(1, round(d / med) - 1) for d in dropped_frame_intervals)
        reasons.append(
            f"{dropped_count} likely dropped-frame intervals (estimated {estimated_dropped} missing frames)"
        )
    if other_abnormal:
        reasons.append(
            f"{len(other_abnormal)} intervals differ from median by > max(2ms, 25%) (non-dropped jitter)"
        )
    return StreamSummary(
        path,
        kind,
        len(times_s),
        fps,
        times_s[0],
        times_s[-1],
        med,
        min(diffs_ms),
        max(diffs_ms),
        len(non_positive) + len(dropped_frame_intervals) + len(other_abnormal),
        "; ".join(reasons),
        dropped_frames=estimated_dropped if dropped_frame_intervals else 0,
    )


def trc_summary(path: Path) -> StreamSummary:
    with open_csv(path) as f:
        rows = list(csv.reader(f, delimiter="\t"))
    header_idx = next(
        (
            i
            for i, row in enumerate(rows)
            if len(row) >= 2 and row[0] == "Frame#" and row[1] == "Time"
        ),
        None,
    )
    if header_idx is None:
        return StreamSummary(
            path,
            "trc",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            1,
            "missing TRC Frame#/Time header",
        )
    times = []
    for row in rows[header_idx + 2 :]:
        if len(row) >= 2:
            try:
                times.append(float(row[1]))
            except ValueError:
                continue
    return summarize_times(path, "trc", times)


def bvh_summary(path: Path) -> StreamSummary:
    frames = None
    frame_time = None
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            lower = stripped.lower()
            if lower.startswith("frames:"):
                frames = int(stripped.split(":", 1)[1].strip())
            elif lower.startswith("frame time:"):
                frame_time = float(stripped.split(":", 1)[1].strip())
                break
    if frames is None or frame_time is None:
        return StreamSummary(
            path,
            "bvh",
            frames,
            None,
            None,
            None,
            None,
            None,
            None,
            1,
            "missing BVH Frames/Frame Time",
        )
    fps = 1.0 / frame_time if frame_time > 0 else None
    end_s = (frames - 1) * frame_time if frames > 0 else None
    dt_ms = frame_time * 1000.0
    return StreamSummary(
        path,
        "bvh",
        frames,
        fps,
        0.0,
        end_s,
        dt_ms,
        dt_ms,
        dt_ms,
        0,
        "",
    )


def csv_summary(path: Path) -> StreamSummary:
    with open_csv(path) as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            first_line = sample.splitlines()[0] if sample.splitlines() else ""
            dialect = (
                csv.excel_tab if path.suffix.lower() == ".tsv" or "\t" in first_line else csv.excel
            )
        reader = csv.DictReader(f, dialect=dialect)
        if not reader.fieldnames:
            return StreamSummary(
                path, "csv", None, None, None, None, None, None, None, 1, "missing header"
            )
        time_col = choose_time_column(reader.fieldnames)
        if time_col is None:
            metadata = hierarchical_csv_metadata(path)
            if metadata is None:
                return StreamSummary(
                    path, "csv", None, None, None, None, None, None, None, 1, "no known time column"
                )
            hierarchical_times, missing_timestamps, num_frames = metadata
            summary = summarize_times(path, "csv", hierarchical_times)
            if num_frames is not None:
                summary = replace(summary, frame_count=num_frames)
            reasons = (
                [f"{missing_timestamps} rows have missing/zero Timestamp values"]
                if missing_timestamps
                else []
            )
            return with_summary_details(summary, reasons=reasons)
        raw = [row.get(time_col, "") for row in reader]
    vals = numeric(raw)
    times = normalize_time_values(vals, time_col)
    return summarize_times(path, "csv", times)


def normalize_time_values(vals: list[float], column_name: str) -> list[float]:
    if not vals:
        return []
    lowered = column_name.lower()
    if lowered.endswith("_ns") or "nanosecond" in lowered:
        return [v / 1e9 for v in vals]
    diffs = [b - a for a, b in pairwise(vals) if b > a]
    median_diff = statistics.median(diffs) if diffs else 0.0
    max_abs = max(abs(v) for v in vals)
    if median_diff >= 1_000_000:
        return [v / 1e9 for v in vals]
    if median_diff >= 1_000:
        return [v / 1e6 for v in vals]
    if max_abs > 1e9 and median_diff >= 0.5:
        return [v / 1000.0 for v in vals]
    return vals


def hierarchical_csv_metadata(path: Path) -> tuple[list[float], int, int | None] | None:
    with open_csv(path) as f:
        rows = list(csv.reader(f))
    header_index = None
    time_index = None
    for index, row in enumerate(rows):
        cleaned = [cell.strip() for cell in row]
        lowered = [cell.lower() for cell in cleaned]
        if "timestamp" in lowered:
            header_index = index
            time_index = lowered.index("timestamp")
            break
    if header_index is None or time_index is None:
        return None
    num_frames = None
    for index, row in enumerate(rows[:header_index]):
        if row and row[0].strip() == "NumFrames":
            for candidate in rows[index + 1 :]:
                if candidate and candidate[0].strip().isdigit():
                    num_frames = int(candidate[0].strip())
                    break
            break
    vals: list[float] = []
    missing_timestamps = 0
    for row in rows[header_index + 1 :]:
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) <= time_index:
            missing_timestamps += 1
            continue
        try:
            value = float(row[time_index])
        except ValueError:
            missing_timestamps += 1
            continue
        if value == 0 or not math.isfinite(value):
            missing_timestamps += 1
            continue
        vals.append(value)
    if not vals:
        return None
    return normalize_time_values(vals, "timestamp"), missing_timestamps, num_frames


def hierarchical_csv_times(path: Path) -> list[float] | None:
    metadata = hierarchical_csv_metadata(path)
    return metadata[0] if metadata is not None else None


def trc_frame_ids(path: Path) -> list[int] | None:
    with open_csv(path) as f:
        rows = list(csv.reader(f, delimiter="\t"))
    header_idx = next(
        (
            i
            for i, row in enumerate(rows)
            if len(row) >= 2 and row[0].strip() == "Frame#" and row[1].strip() == "Time"
        ),
        None,
    )
    if header_idx is None:
        return None
    ids: list[int] = []
    for row in rows[header_idx + 2 :]:
        if not row or not row[0].strip():
            continue
        try:
            ids.append(int(float(row[0])))
        except ValueError:
            continue
    return ids or None


def csv_frame_rows(path: Path) -> tuple[list[int], list[int]] | None:
    with open_csv(path) as f:
        rows = list(csv.reader(f))
    frame_header_idx = next(
        (i for i, row in enumerate(rows) if row and row[0].strip().lower() == "frame#"),
        None,
    )
    if frame_header_idx is None:
        return None
    timestamp_col = None
    if frame_header_idx + 1 < len(rows):
        timestamp_col = next(
            (
                j
                for j, cell in enumerate(rows[frame_header_idx + 1])
                if cell.strip().lower() == "timestamp"
            ),
            None,
        )
    ids: list[int] = []
    missing_timestamp_ids: list[int] = []
    for row in rows[frame_header_idx + 2 :]:
        if not row or not row[0].strip():
            continue
        try:
            frame_id = int(float(row[0]))
        except ValueError:
            continue
        ids.append(frame_id)
        if timestamp_col is not None:
            try:
                value = float(row[timestamp_col]) if len(row) > timestamp_col else 0.0
            except ValueError:
                value = 0.0
            if value == 0 or not math.isfinite(value):
                missing_timestamp_ids.append(frame_id)
    if not ids:
        return None
    return ids, missing_timestamp_ids


def frame_missing_details(
    session_dir: Path,
    summaries: Iterable[StreamSummary],
    reference_n: int | None,
    ratio: int | None = 8,
) -> list[FrameMissingDetail]:
    if reference_n is None:
        return []
    details: list[FrameMissingDetail] = []
    for item in summaries:
        rel = (
            item.path.relative_to(session_dir)
            if item.path.is_relative_to(session_dir)
            else item.path
        )
        source = infer_fps_source(rel, item.kind)
        if source != "gt" or item.kind.lower() not in {"bvh", "trc", "csv", "xrs"}:
            continue
        expected = expected_frame_count(source, item.kind, item.stream, reference_n, ratio)
        if expected is None:
            continue
        actual_ids: list[int] | None = None
        missing_timestamp_ids: list[int] = []
        try:
            if item.kind.lower() == "trc":
                actual_ids = trc_frame_ids(item.path)
            elif item.kind.lower() == "csv":
                csv_rows = csv_frame_rows(item.path)
                if csv_rows is not None:
                    actual_ids, missing_timestamp_ids = csv_rows
                elif item.frame_count is not None and item.path.exists():
                    actual_ids = list(range(1, item.frame_count + 1))
            elif item.kind.lower() == "xrs":
                xrs_rows = xrs_frame_rows(item.path)
                if xrs_rows is not None:
                    actual_ids, missing_timestamp_ids = xrs_rows
                elif item.frame_count is not None and item.path.exists():
                    actual_ids = list(range(1, item.frame_count + 1))
            elif item.kind.lower() == "bvh" and item.frame_count is not None:
                actual_ids = list(range(1, item.frame_count + 1))
        except OSError:
            actual_ids = None
        if not actual_ids:
            continue
        present = set(actual_ids)
        missing_ids = tuple(
            frame_id for frame_id in range(1, expected + 1) if frame_id not in present
        )
        details.append(
            FrameMissingDetail(
                item.path,
                item.kind,
                expected,
                item.frame_count,
                missing_ids,
                tuple(missing_timestamp_ids),
            )
        )
    return details


def format_frame_ranges(frame_ids: Iterable[int], max_chars: int = 2500) -> str:
    values = sorted(set(frame_ids))
    if not values:
        return ""
    ranges: list[str] = []
    start = prev = values[0]
    for value in values[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = value
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    text = ", ".join(ranges)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip(", ") + " ..."
    return text


def write_missing_frame_reports(
    session_dir: Path,
    details: list[FrameMissingDetail],
    out_dir: Path,
    ratio: int | None = 8,
) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    missing_rows: list[list[str | int]] = []
    timestamp_rows: list[list[str | int]] = []
    ratio_label = "8" if ratio is None else str(ratio)
    for detail in details:
        rel = (
            detail.path.relative_to(session_dir)
            if detail.path.is_relative_to(session_dir)
            else detail.path
        )
        for frame_id in detail.missing_ids:
            missing_rows.append(
                [str(rel), detail.kind, frame_id, f"missing from expected {ratio_label}*(n+1)"]
            )
        for frame_id in detail.missing_timestamp_ids:
            timestamp_rows.append([str(rel), detail.kind, frame_id, "Timestamp missing/zero"])
    with (out_dir / "missing_frames.tsv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["path", "kind", "frame_id", "note"])
        writer.writerows(missing_rows)
    with (out_dir / "missing_timestamp_frames.tsv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["path", "kind", "frame_id", "note"])
        writer.writerows(timestamp_rows)
    md: list[str] = []
    md.extend(["", f"## Missing frame IDs (expected {ratio_label}*(n+1))", ""])
    if details:
        for detail in details:
            rel = (
                detail.path.relative_to(session_dir)
                if detail.path.is_relative_to(session_dir)
                else detail.path
            )
            if detail.missing_ids:
                md.append(
                    f"- {rel}: expected {detail.expected_frames}, "
                    f"actual {detail.actual_frames}, missing {len(detail.missing_ids)}: "
                    f"{format_frame_ranges(detail.missing_ids)}"
                )
            else:
                md.append(f"- {rel}: no missing frame IDs")
    else:
        md.append("- None")
    timestamp_details = [d for d in details if d.missing_timestamp_ids]
    md.extend(["", "## Missing timestamps", ""])
    if timestamp_details:
        for detail in timestamp_details:
            rel = (
                detail.path.relative_to(session_dir)
                if detail.path.is_relative_to(session_dir)
                else detail.path
            )
            md.append(
                f"- {rel}: {len(detail.missing_timestamp_ids)} frames have "
                f"missing/zero Timestamp; frame IDs: "
                f"{format_frame_ranges(detail.missing_timestamp_ids)}"
            )
    else:
        md.append("- None")
    return md


def xrs_metadata(
    path: Path,
) -> tuple[list[float], int, int | None, list[int], list[int]] | None:
    with open_csv(path) as f:
        rows = list(csv.reader(f, delimiter="\t"))
    header_index = None
    timestamp_index = None
    for index, row in enumerate(rows):
        lowered = [cell.strip().lower() for cell in row]
        if "timestamp" in lowered:
            header_index = index
            timestamp_index = lowered.index("timestamp")
            break
    if header_index is None or timestamp_index is None:
        return None
    frame_col = 0
    if header_index > 0:
        for j, cell in enumerate(rows[header_index - 1]):
            if cell.strip().lower() == "frame#":
                frame_col = j
                break
    num_frames = None
    for index, row in enumerate(rows[:header_index]):
        if row and row[0].strip() == "NumFrames":
            for candidate in rows[index + 1 :]:
                if candidate and candidate[0].strip().isdigit():
                    num_frames = int(candidate[0].strip())
                    break
            break
    vals: list[float] = []
    missing_timestamps = 0
    frame_ids: list[int] = []
    missing_timestamp_ids: list[int] = []
    for row in rows[header_index + 1 :]:
        if not row or all(not cell.strip() for cell in row):
            continue
        try:
            frame_id = int(float(row[frame_col]))
        except (IndexError, ValueError):
            frame_id = None
        if frame_id is not None:
            frame_ids.append(frame_id)
        try:
            value = float(row[timestamp_index]) if len(row) > timestamp_index else 0.0
        except ValueError:
            value = 0.0
        if value == 0 or not math.isfinite(value):
            missing_timestamps += 1
            if frame_id is not None:
                missing_timestamp_ids.append(frame_id)
        else:
            vals.append(value)
    if not vals and num_frames is None:
        return vals, missing_timestamps, num_frames, frame_ids, missing_timestamp_ids
    return (
        normalize_time_values(vals, "timestamp"),
        missing_timestamps,
        num_frames,
        frame_ids,
        missing_timestamp_ids,
    )


def xrs_frame_rows(path: Path) -> tuple[list[int], list[int]] | None:
    metadata = xrs_metadata(path)
    if metadata is None:
        return None
    return metadata[3], metadata[4]


def xrs_summary(path: Path) -> StreamSummary:
    metadata = xrs_metadata(path)
    if metadata is None:
        return StreamSummary(
            path, "xrs", None, None, None, None, None, None, None, 1, "missing XRS Timestamp header"
        )
    times, missing_timestamps, num_frames, _, _ = metadata
    if not times:
        reason = "no XRS timestamp rows"
        if missing_timestamps:
            reason += f"; {missing_timestamps} rows have missing/zero Timestamp values"
        return StreamSummary(
            path,
            "xrs",
            num_frames,
            None,
            None,
            None,
            None,
            None,
            None,
            1,
            reason,
        )
    summary = summarize_times(path, "xrs", times)
    if num_frames is not None:
        summary = replace(summary, frame_count=num_frames)
    reasons = []
    if missing_timestamps:
        reasons.append(f"{missing_timestamps} rows have missing/zero Timestamp values")
    return with_summary_details(summary, reasons=reasons)


def choose_time_column(fieldnames: list[str]) -> str | None:
    lower_map = {name.lower().strip(): name for name in fieldnames}
    exact = (
        "capture_time",
        "capture_time_ns",
        "timestamp",
        "timestamps",
        "time",
        "log_time",
        "log_time_ns",
    )
    for key in exact:
        if key in lower_map:
            return lower_map[key]
    normalized = {
        name.lower().replace(" ", "").replace("_", "").replace("-", "").strip(): name
        for name in fieldnames
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
    for name in fieldnames:
        lowered = name.lower()
        if "time" in lowered and all(skip not in lowered for skip in ("runtime", "timeout")):
            return name
    return None


def quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sensor_stream_kind(table_name: str) -> str:
    lowered = table_name.lower()
    if "gyro" in lowered:
        return "imu_gyro"
    if "acc" in lowered:
        return "imu_acc"
    if "mag" in lowered:
        return "mag"
    return "sensor"


def sqlite_sensor_summaries(path: Path) -> list[StreamSummary]:
    try:
        connection = sqlite3.connect(path)
    except sqlite3.Error as exc:
        return [
            StreamSummary(
                path,
                "database",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                f"failed to open SQLite database: {exc}",
                stream="database",
            )
        ]

    summaries: list[StreamSummary] = []
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if not str(row[0]).lower().startswith("sqlite_")
        ]
        for table in tables:
            quoted_table = quote_sqlite_identifier(table)
            try:
                column_rows = list(connection.execute(f"PRAGMA table_info({quoted_table})"))
                columns = [str(row[1]) for row in column_rows]
                time_column = choose_time_column(columns)
                if time_column is None:
                    continue
                primary_keys = [
                    (int(row[5]), str(row[1])) for row in column_rows if int(row[5] or 0) > 0
                ]
                if primary_keys:
                    order_by = ", ".join(
                        quote_sqlite_identifier(name) for _, name in sorted(primary_keys)
                    )
                else:
                    order_by = "rowid"
                rows = list(
                    connection.execute(
                        f"SELECT {quote_sqlite_identifier(time_column)} FROM {quoted_table} "
                        f"ORDER BY {order_by}"
                    )
                )
            except sqlite3.Error as exc:
                summaries.append(
                    StreamSummary(
                        path,
                        sensor_stream_kind(table),
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        1,
                        f"failed to inspect SQLite table {table}: {exc}",
                        stream=table,
                    )
                )
                continue

            values: list[float] = []
            invalid_count = 0
            for (value,) in rows:
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    invalid_count += 1
            times_s = normalize_time_values(values, time_column)
            summary = summarize_times(path, sensor_stream_kind(table), times_s)
            reasons = (
                [f"{invalid_count} rows have invalid or NULL {time_column} values"]
                if invalid_count
                else []
            )
            summaries.append(
                with_summary_details(
                    summary,
                    stream=table,
                    time_basis=f"{time_column} (auto-normalized to seconds)",
                    reasons=reasons,
                    abnormal_count_delta=invalid_count,
                )
            )
    except sqlite3.Error as exc:
        return [
            StreamSummary(
                path,
                "database",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                1,
                f"failed to enumerate SQLite tables: {exc}",
                stream="database",
            )
        ]
    finally:
        connection.close()

    if summaries:
        return summaries
    return [
        StreamSummary(
            path,
            "database",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            1,
            "no timestamped sensor tables found",
            stream="database",
        )
    ]


def discover_files(session_dir: Path) -> list[Path]:
    ignored_names = {"manifest.json", "manifest.tsv"}
    return sorted(
        p
        for p in session_dir.rglob("*")
        if p.is_file()
        and p.name.lower() not in ignored_names
        and "_artifacts" not in p.relative_to(session_dir).parts
        and (
            p.suffix.lower() in VIDEO_SUFFIXES
            or p.suffix.lower() in TEXT_SUFFIXES
            or p.suffix.lower() in DATABASE_SUFFIXES
        )
    )


def summarize_file(path: Path, ffprobe: str) -> StreamSummary:
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return video_summary(path, ffprobe)
    if suffix == ".trc":
        return trc_summary(path)
    if suffix == ".bvh":
        return bvh_summary(path)
    if suffix == ".xrs":
        return xrs_summary(path)
    if suffix in {".csv", ".tsv"}:
        return csv_summary(path)
    return StreamSummary(path, suffix.lstrip("."), None, None, None, None, None, None, None, 0, "")


def summarize_path(path: Path, ffprobe: str) -> list[StreamSummary]:
    if path.suffix.lower() in DATABASE_SUFFIXES:
        return sqlite_sensor_summaries(path)
    return [summarize_file(path, ffprobe)]


def fmt(value: float | None, digits: int = 6) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def infer_fps_source(path: str | Path, kind: str) -> str:
    normalized = str(path).replace("\\", "/")
    parts = [part.lower() for part in normalized.split("/") if part]
    name = parts[-1] if parts else ""
    if name.startswith("robocap_"):
        return "robocap"
    if kind.lower() in GT_FPS_KINDS:
        return "gt"
    if any(
        part.startswith("test") or part in {"gt", "ground_truth", "ground-truth", "mocap", "nokov"}
        for part in parts[:-1]
    ):
        return "gt"
    return "other"


def nearest_multiple_of_ten(value: float) -> int:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"FPS must be finite and positive, got {value!r}.")
    return int(math.floor(value / 10.0 + 0.5) * 10)


def estimate_frame_ratio(
    records: Iterable[FpsRecord], source: str, report_path: Path | None = None
) -> FrameRatioEstimate | None:
    rows = list(records)
    gt_fps = [
        row.fps
        for row in rows
        if row.source == "gt"
        and row.kind in GT_FPS_KINDS
        and math.isfinite(row.fps)
        and row.fps > 0
    ]
    robocap_fps = [
        row.fps
        for row in rows
        if row.source == "robocap"
        and row.kind == "video"
        and math.isfinite(row.fps)
        and row.fps > 0
    ]
    if not gt_fps or not robocap_fps:
        return None
    gt_mean = statistics.fmean(gt_fps)
    robocap_mean = statistics.fmean(robocap_fps)
    gt_rounded = nearest_multiple_of_ten(gt_mean)
    robocap_rounded = nearest_multiple_of_ten(robocap_mean)
    if gt_rounded <= 0 or robocap_rounded <= 0:
        return None
    ratio_before_rounding = gt_rounded / robocap_rounded
    return FrameRatioEstimate(
        source=source,
        report_path=report_path,
        gt_sample_count=len(gt_fps),
        robocap_sample_count=len(robocap_fps),
        gt_fps_mean=gt_mean,
        robocap_fps_mean=robocap_mean,
        gt_fps_rounded_10=gt_rounded,
        robocap_fps_rounded_10=robocap_rounded,
        ratio_before_rounding=ratio_before_rounding,
        ratio=round_positive_ratio(ratio_before_rounding),
    )


def fps_record_from_summary(item: StreamSummary, session_dir: Path) -> FpsRecord | None:
    if item.fps is None or not math.isfinite(item.fps) or item.fps <= 0:
        return None
    path = (
        item.path.relative_to(session_dir) if item.path.is_relative_to(session_dir) else item.path
    )
    return FpsRecord(
        path=str(path),
        kind=item.kind.lower(),
        source=infer_fps_source(path, item.kind),
        fps=float(f"{item.fps:.9f}"),
    )


def inspection_output_dir(session_dir: Path, segment: str | None) -> Path:
    return session_dir / "_artifacts" / (segment or "all") / "inspection"


def inspection_file_matches_segment(path: Path, segment: str | None) -> bool:
    if not segment or path.suffix.lower() not in VIDEO_SUFFIXES | DATABASE_SUFFIXES:
        return True
    name = path.name.lower()
    if not name.startswith(("robocap_", "robowrist_")):
        return True
    return segment.lower() in name


def inspection_files(session_dir: Path, segment: str | None) -> list[Path]:
    return [
        path
        for path in discover_files(session_dir)
        if inspection_file_matches_segment(path, segment)
    ]


def reference_robocap_frame_count(
    session_dir: Path, summaries: Iterable[StreamSummary]
) -> int | None:
    robocap_videos: list[StreamSummary] = []
    for item in summaries:
        if item.kind != "video" or item.frame_count is None:
            continue
        rel = (
            item.path.relative_to(session_dir)
            if item.path.is_relative_to(session_dir)
            else item.path
        )
        if infer_fps_source(rel, item.kind) == "robocap":
            robocap_videos.append(item)
    if not robocap_videos:
        return None
    preferred = [
        item
        for item in robocap_videos
        if item.path.name.lower().endswith("video_left.mp4") or item.path.name.lower() == "left.mp4"
    ]
    return (preferred or robocap_videos)[0].frame_count


def expected_frame_count(
    source: str,
    kind: str,
    stream: str,
    reference_robocap_frames: int | None,
    ratio: int | None = 8,
) -> int | None:
    if reference_robocap_frames is None or reference_robocap_frames < 0:
        return None
    if stream == "third_person_video":
        return reference_robocap_frames + 1
    if source == "robocap" and kind == "video":
        return reference_robocap_frames
    if source == "gt" and kind.lower() in GT_FPS_KINDS:
        effective_ratio = ratio if ratio is not None else 8
        return effective_ratio * (reference_robocap_frames + 1)
    return None


def resolve_session_auto_ratio(
    session_dir: Path, segment: str | None, ffprobe: str
) -> FrameRatioEstimate | None:
    files = inspection_files(session_dir, segment)
    summaries = [summary for path in files for summary in summarize_path(path, ffprobe)]
    fps_records = [
        record
        for item in summaries
        if (record := fps_record_from_summary(item, session_dir)) is not None
    ]
    return estimate_frame_ratio(fps_records, "live session scan")


def find_first(session_dir: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(session_dir.rglob(pattern))
        if matches:
            return matches[0]
    return None


def load_trc_times(path: Path) -> list[float]:
    summary = trc_summary(path)
    if summary.frame_count is None or summary.median_dt_ms is None:
        return []
    with open_csv(path) as f:
        rows = list(csv.reader(f, delimiter="\t"))
    header_idx = next(
        i for i, row in enumerate(rows) if len(row) >= 2 and row[0] == "Frame#" and row[1] == "Time"
    )
    times = []
    for row in rows[header_idx + 2 :]:
        if len(row) >= 2:
            try:
                times.append(float(row[1]))
            except ValueError:
                continue
    return times


def load_bvh_times(path: Path) -> list[float]:
    summary = bvh_summary(path)
    if summary.frame_count is None or summary.median_dt_ms is None:
        return []
    dt = summary.median_dt_ms / 1000.0
    return [i * dt for i in range(summary.frame_count)]


def direct_frame_ratio_estimate(
    video: StreamSummary, nokov: StreamSummary
) -> FrameRatioEstimate | None:
    records: list[FpsRecord] = []
    if video.fps is not None:
        records.append(FpsRecord(str(video.path), "video", "robocap", float(video.fps)))
    if nokov.fps is not None:
        records.append(FpsRecord(str(nokov.path), nokov.kind.lower(), "gt", float(nokov.fps)))
    return estimate_frame_ratio(records, "direct_sources")


def resolve_auto_ratio(video: StreamSummary, nokov: StreamSummary) -> float:
    estimate = direct_frame_ratio_estimate(video, nokov)
    return estimate.ratio if estimate is not None else 8.0


def auto_ratio_markdown_lines(estimate: FrameRatioEstimate) -> list[str]:
    lines = [
        f"- auto_ratio_source: {estimate.source}",
        f"- gt_fps_samples: {estimate.gt_sample_count}",
        f"- gt_fps_mean: {estimate.gt_fps_mean:.9f}",
        f"- gt_fps_rounded_10: {estimate.gt_fps_rounded_10}",
        f"- robocap_fps_samples: {estimate.robocap_sample_count}",
        f"- robocap_fps_mean: {estimate.robocap_fps_mean:.9f}",
        f"- robocap_fps_rounded_10: {estimate.robocap_fps_rounded_10}",
        f"- auto_ratio_before_rounding: {estimate.ratio_before_rounding:.9f}",
        f"- auto_ratio_rounded_integer: {estimate.ratio}",
    ]
    if estimate.report_path is not None:
        lines.insert(1, f"- auto_ratio_report: `{estimate.report_path}`")
    return lines


def auto_ratio_console_summary(estimate: FrameRatioEstimate) -> str:
    return (
        f"Auto ratio {estimate.ratio} (before final rounding "
        f"{estimate.ratio_before_rounding:.9f}): GT mean {estimate.gt_fps_mean:.6f} "
        f"-> {estimate.gt_fps_rounded_10}, Robocap mean "
        f"{estimate.robocap_fps_mean:.6f} -> {estimate.robocap_fps_rounded_10} "
        f"({estimate.source})"
    )


def video_to_gt_frame_float(video_frame: int, ratio: float, video_frame_offset: int) -> float:
    return FrameAlignment(ratio, video_frame_offset).video_to_gt_frame_float(video_frame)


def video_to_gt_frame(video_frame: int, ratio: float, video_frame_offset: int) -> int:
    return FrameAlignment(ratio, video_frame_offset).video_to_gt_frame(video_frame)


def find_nokov_source(session_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    return find_first(
        session_dir,
        [
            "test*/*-hand.bvh",
            "test*/*-Body0_Left.bvh",
            "test*/*.bvh",
            "test*/*-Tracker0.trc",
            "test*/*-hand.trc",
            "test*/*.trc",
            "*.bvh",
            "*.trc",
        ],
    )


def write_offset_report(
    session_dir: Path,
    segment: str | None,
    ratio_arg: str,
    offset: int,
    nokov_source: Path | None,
    out_dir: Path,
    ffprobe: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = find_first(session_dir, ["*video_left.mp4", "*left*.mp4", "*.mp4"])
    nokov_path = find_nokov_source(session_dir, nokov_source)
    if video_path is None or nokov_path is None:
        raise FileNotFoundError(
            "Need at least one video and one TRC/BVH file for offset inspection."
        )
    video = video_summary(video_path, ffprobe)
    nokov = (
        bvh_summary(nokov_path) if nokov_path.suffix.lower() == ".bvh" else trc_summary(nokov_path)
    )
    ratio_estimate = None
    if ratio_arg == "auto":
        ratio_estimate = resolve_session_auto_ratio(session_dir, segment, ffprobe)
        if ratio_estimate is None:
            ratio_estimate = direct_frame_ratio_estimate(video, nokov)
        ratio = ratio_estimate.ratio if ratio_estimate is not None else 8.0
    else:
        ratio = float(ratio_arg)
    alignment = FrameAlignment(ratio, offset)
    frame_count = video.frame_count or 0
    nokov_count = nokov.frame_count or 0
    rows = [
        [
            "video_frame",
            "expected_nokov_frame_float",
            "expected_nokov_frame",
            "offset_video_frame",
            "offset_nokov_frame_float",
            "offset_nokov_frame",
            "expected_delta_frames",
            "expected_minus_8x",
        ]
    ]
    max_rows = min(
        frame_count,
        max(0, math.ceil((nokov_count - alignment.gt_frame_offset) / ratio)),
    )
    for video_frame in range(max_rows):
        expected_float = video_frame * ratio
        expected = round(expected_float)
        offset_video_frame = video_frame + offset
        offset_float = alignment.video_to_gt_frame_float(video_frame)
        offset_frame = alignment.video_to_gt_frame(video_frame)
        rows.append(
            [
                str(video_frame),
                f"{expected_float:.9f}",
                str(expected),
                str(offset_video_frame),
                f"{offset_float:.9f}",
                str(offset_frame),
                str(offset_frame - expected),
                f"{offset_frame - offset_video_frame * 8:.9f}",
            ]
        )
    with (out_dir / "video_to_nokov_frame_alignment.tsv").open(
        "w", encoding="utf-8", newline=""
    ) as f:
        csv.writer(f, delimiter="\t").writerows(rows)
    md = [
        "# Offset inspection",
        "",
        f"- session: `{session_dir}`",
        f"- segment: `{segment or 'auto'}`",
        f"- video: `{video_path}`",
        f"- nokov: `{nokov_path}`",
        f"- video_fps: {fmt(video.fps, 9)}",
        f"- nokov_fps: {fmt(nokov.fps, 9)}",
        f"- main_ratio: {ratio:.9f}",
        f"- offset: {offset}",
        "- offset_unit: robocap_video_frames",
        f"- source_script_gt_frame_offset: {alignment.gt_frame_offset}",
        f"- relative_shift: {alignment.relative_shift_description()}",
        f"- comparable_video_frames: {max_rows}",
    ]
    if ratio_estimate is not None:
        md.extend(auto_ratio_markdown_lines(ratio_estimate))
    md.extend(
        [
            "",
            "The source-script mapping is: `NOKOV frame = round(video frame * ratio) + GT frame offset`.",
            "The user-facing conversion is: `GT frame offset = round(Robocap frame offset * ratio)`.",
            "`expected_minus_8x` is kept because a fixed 8x mapping is the historical baseline.",
        ]
    )
    write_text(out_dir / "offset_inspection.md", "\n".join(md) + "\n")


def write_offset_sweep(
    session_dir: Path,
    segment: str | None,
    ratio_arg: str,
    offset_min: int,
    offset_max: int,
    nokov_source: Path | None,
    out_dir: Path,
    ffprobe: str,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = find_first(session_dir, ["*video_left.mp4", "*left*.mp4", "*.mp4"])
    nokov_path = find_nokov_source(session_dir, nokov_source)
    if video_path is None or nokov_path is None:
        raise FileNotFoundError("Need at least one video and one TRC/BVH file for offset sweep.")
    video = video_summary(video_path, ffprobe)
    nokov = (
        bvh_summary(nokov_path) if nokov_path.suffix.lower() == ".bvh" else trc_summary(nokov_path)
    )
    ratio_estimate = None
    if ratio_arg == "auto":
        ratio_estimate = resolve_session_auto_ratio(session_dir, segment, ffprobe)
        if ratio_estimate is None:
            ratio_estimate = direct_frame_ratio_estimate(video, nokov)
        ratio = ratio_estimate.ratio if ratio_estimate is not None else 8.0
    else:
        ratio = float(ratio_arg)
    rows = [
        [
            "offset",
            "main_ratio",
            "video_fps",
            "nokov_fps",
            "last_video_frame",
            "last_nokov_frame",
            "tail_margin_frames",
        ]
    ]
    frame_count = video.frame_count or 0
    nokov_count = nokov.frame_count or 0
    for offset in range(offset_min, offset_max + 1):
        last_video = max(0, frame_count - 1)
        last_nokov = video_to_gt_frame(last_video, ratio, offset)
        rows.append(
            [
                str(offset),
                f"{ratio:.9f}",
                fmt(video.fps, 9),
                fmt(nokov.fps, 9),
                str(last_video),
                str(last_nokov),
                str(nokov_count - 1 - last_nokov),
            ]
        )
    with (out_dir / "offset_sweep.tsv").open("w", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter="\t").writerows(rows)
    write_text(
        out_dir / "offset_sweep.md",
        "\n".join(
            [
                "# Offset sweep",
                "",
                f"- session: `{session_dir}`",
                f"- segment: `{segment or 'auto'}`",
                f"- ratio: {ratio:.9f}",
                f"- offsets: {offset_min}..{offset_max}",
                "- offset_unit: robocap_video_frames",
                *(auto_ratio_markdown_lines(ratio_estimate) if ratio_estimate is not None else []),
                "",
                "Use this table to check whether a candidate offset runs beyond the NOKOV frame range.",
            ]
        )
        + "\n",
    )


def add_common_export_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--segment", default=None)
    parser.add_argument("--save", type=Path, default=None)
    parser.add_argument("--mode", choices=("time", "frame"), default="time")
    parser.add_argument(
        "--ratio",
        default="auto",
        help=(
            "Frame mode NOKOV/video ratio. Default auto scans the current session, averages GT "
            "and Robocap FPS separately, rounds both means to the nearest 10, divides them, then "
            "rounds the ratio to the nearest positive integer."
        ),
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help=(
            "Signed Robocap-video-frame offset. Positive advances NOKOV/GT relative to "
            "Robocap video; negative delays it. Internally convert with GT frame offset = "
            "round(Robocap frame offset * ratio), then use the source-script mapping."
        ),
    )
    parser.add_argument("--reference-video", default="left")
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
    parser.add_argument("--use-proxy", action="store_true")
    parser.add_argument("--proxy-height", type=int, default=540)
    parser.add_argument("--display", action="store_true", help="Use display blueprint preset.")
    parser.add_argument("--gt-dir", type=Path, default=None)
    parser.add_argument("--gt-file", type=Path, action="append", default=None)
    parser.add_argument("--gt-third-person-video", type=Path, default=None)
    parser.add_argument("--mano-model-dir", type=Path, default=Path("Z:/MODELS/hand_models/mano"))
    parser.add_argument(
        "--retarget-model", choices=("none", "mano", "smpl", "smplh"), default="mano"
    )
    parser.add_argument(
        "--no-mano-mesh",
        dest="retarget_model",
        action="store_const",
        const="none",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--gt-coordinate-scale", type=float, default=0.001)
    parser.add_argument("--bvh-coordinate-scale", type=float, default=0.01)
    parser.add_argument("--no-robowrist", action="store_true")
    parser.add_argument("--no-mag", action="store_true")
    parser.add_argument("--no-imu", action="store_true")
    parser.add_argument(
        "--interpolate-dropped-frames",
        action="store_true",
        help=(
            "Linearly fill NOKOV/GT dropped-frame gaps at the fixed 240 FPS source rate "
            "before alignment; interpolated 3D frames are red and labeled with frame indexes."
        ),
    )
    parser.add_argument("--spawn", action="store_true")
    parser.add_argument("--inspect", action="store_true")


def command_export(args: argparse.Namespace) -> int:
    from robocap_rerun_tools import exporter

    ratio_estimate = None
    resolved_ratio = args.ratio
    if args.mode == "frame" and args.ratio == "auto":
        ratio_estimate = resolve_session_auto_ratio(
            args.session_dir, args.segment, resolve_ffprobe()
        )
        if ratio_estimate is not None:
            resolved_ratio = str(ratio_estimate.ratio)

    argv = [
        "--session-dir",
        str(args.session_dir),
        "--gt-alignment-mode",
        args.mode,
        "--gt-frame-reference-video",
        args.reference_video,
        "--gt-video-frame-offset",
        str(args.offset),
        "--mano-model-dir",
        str(args.mano_model_dir),
        "--gt-coordinate-scale",
        str(args.gt_coordinate_scale),
        "--bvh-coordinate-scale",
        str(args.bvh_coordinate_scale),
        "--proxy-height",
        str(args.proxy_height),
        "--retarget-model",
        args.retarget_model,
    ]
    if args.segment:
        argv.extend(["--segment", args.segment])
    if args.save:
        argv.extend(["--save", str(args.save)])
    if args.robocap_start_frame is not None:
        argv.extend(["--robocap-start-frame", str(args.robocap_start_frame)])
    if args.robocap_end_frame is not None:
        argv.extend(["--robocap-end-frame", str(args.robocap_end_frame)])
    if args.gt_dir:
        argv.extend(["--gt-dir", str(args.gt_dir)])
    for gt_file in args.gt_file or []:
        argv.extend(["--gt-file", str(gt_file)])
    if args.gt_third_person_video:
        argv.extend(["--gt-third-person-video", str(args.gt_third_person_video)])
    if args.mode == "frame" and resolved_ratio != "auto":
        argv.extend(["--gt-frame-ratio", resolved_ratio])
    if args.use_proxy:
        argv.append("--use-proxy")
    if args.display:
        argv.extend(["--blueprint-preset", "display"])
    if args.no_robowrist:
        argv.append("--no-robowrist")
    if args.no_mag:
        argv.append("--no-mag")
    if args.no_imu:
        argv.append("--no-imu")
    if args.interpolate_dropped_frames:
        argv.append("--interpolate-dropped-frames")
    if args.spawn:
        argv.append("--spawn")
    if args.inspect:
        argv.append("--inspect")
    if ratio_estimate is not None:
        print(auto_ratio_console_summary(ratio_estimate))
    old_argv = sys.argv
    try:
        sys.argv = [old_argv[0], *argv]
        exporter.main()
    finally:
        sys.argv = old_argv
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    files = inspection_files(args.session_dir, args.segment)
    ffprobe = resolve_ffprobe(args.ffprobe, args.ffmpeg)
    summaries = [summary for path in files for summary in summarize_path(path, ffprobe)]
    out_dir = args.output or inspection_output_dir(args.session_dir, args.segment)
    fps_records = [
        record
        for item in summaries
        if (record := fps_record_from_summary(item, args.session_dir)) is not None
    ]
    ratio_estimate = estimate_frame_ratio(fps_records, "timestamp anomaly inspection")
    from .timestamp_anomaly import write_timestamp_anomaly_report

    anomaly_report = write_timestamp_anomaly_report(
        args.session_dir,
        args.segment,
        summaries,
        out_dir,
        ratio_estimate,
    )
    print(f"Wrote timestamp anomaly inspection to {anomaly_report}")
    return 0


def command_inspect_offset(args: argparse.Namespace) -> int:
    out_dir = (
        args.output
        or args.session_dir
        / "_artifacts"
        / (args.segment or "segment")
        / f"offset{args.offset}_inspection"
    )
    write_offset_report(
        args.session_dir,
        args.segment,
        args.ratio,
        args.offset,
        args.nokov_source,
        out_dir,
        resolve_ffprobe(args.ffprobe, args.ffmpeg),
    )
    print(f"Wrote offset inspection to {out_dir}")
    return 0


def command_sweep_offset(args: argparse.Namespace) -> int:
    out_dir = (
        args.output
        or args.session_dir / "_artifacts" / (args.segment or "segment") / "offset_sweep"
    )
    write_offset_sweep(
        args.session_dir,
        args.segment,
        args.ratio,
        args.offset_min,
        args.offset_max,
        args.nokov_source,
        out_dir,
        resolve_ffprobe(args.ffprobe, args.ffmpeg),
    )
    print(f"Wrote offset sweep to {out_dir}")
    return 0


def command_package_data(args: argparse.Namespace) -> int:
    from robocap_rerun_tools.data_packager import package_session

    package_session(args)
    return 0


def refresh_modelscope_inspection(args: argparse.Namespace) -> None:
    if not args.refresh_inspection or args.dry_run:
        return
    command_inspect(
        argparse.Namespace(
            session_dir=args.session_dir,
            segment=args.segment,
            output=None,
            ffprobe=args.ffprobe,
            ffmpeg=args.ffmpeg,
        )
    )


def command_modelscope_stage(args: argparse.Namespace) -> int:
    from robocap_rerun_tools.modelscope_publisher import (
        ModelScopePublisherError,
        stage_session,
    )

    try:
        refresh_modelscope_inspection(args)
        staged = stage_session(
            args.session_dir,
            args.primitive_id,
            dataset_root=args.dataset_root,
            session_id=args.session_id,
            segment=args.segment,
            raw_video=args.raw_video,
            ffmpeg=args.ffmpeg,
            proxy_height=args.proxy_height,
            proxy_crf=args.proxy_crf,
            proxy_bitrate=args.proxy_bitrate,
            include_rrd=args.include_rrd,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, OSError, ValueError, ModelScopePublisherError) as exc:
        print(f"ModelScope staging failed: {exc}", file=sys.stderr)
        return 2
    action = "Would stage" if staged.dry_run else "Staged"
    print(f"{action} {staged.primitive_id}/{staged.session_id}")
    print(f"Dataset root: {staged.dataset_root}")
    print(f"Session path: {staged.session_dir}")
    print(f"Files: {staged.file_count}; bytes: {staged.total_bytes}")
    print(f"Inspection HTML: {staged.inspection_html}")
    if not staged.dry_run:
        print(f"Metadata: {staged.metadata_path}")
    return 0


def command_modelscope_auth(_args: argparse.Namespace) -> int:
    from robocap_rerun_tools.modelscope_publisher import (
        ModelScopePublisherError,
        load_modelscope_settings,
        token_status,
        verify_modelscope_auth,
    )

    try:
        settings = load_modelscope_settings()
        print(token_status(settings))
        username = verify_modelscope_auth(settings)
    except (OSError, ValueError, ModelScopePublisherError) as exc:
        print(f"ModelScope authentication failed: {exc}", file=sys.stderr)
        return 2
    print(f"Authenticated as: {username}")
    return 0


def command_modelscope_upload(args: argparse.Namespace) -> int:
    from robocap_rerun_tools.modelscope_publisher import (
        ModelScopePublisherError,
        load_staged_dataset,
        upload_staged_dataset,
    )

    try:
        staged = load_staged_dataset(args.dataset_root)
        result = upload_staged_dataset(
            staged,
            args.repo_id,
            revision=args.revision,
            create_if_missing=args.create_if_missing,
            visibility=args.visibility,
            license_name=args.license_name,
            commit_message=args.commit_message,
            max_workers=args.max_workers,
            use_cache=args.use_cache,
        )
    except (FileNotFoundError, OSError, ValueError, ModelScopePublisherError) as exc:
        print(f"ModelScope upload failed: {exc}", file=sys.stderr)
        return 2
    print(f"Uploaded dataset root: {result.uploaded_path}")
    print(f"Prepared sessions: {result.session_count}")
    print(f"Repository: {result.repo_url}")
    print(f"Revision: {result.revision}")
    print(f"Authenticated as: {result.username}")
    return 0


def command_web(args: argparse.Namespace) -> int:
    from robocap_rerun_tools.web_app import main as web_main

    return web_main(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robocap-rerun")
    sub = parser.add_subparsers(dest="command", required=True)

    export_parser = sub.add_parser("export", help="Generate a time-aligned or frame-aligned RRD.")
    add_common_export_args(export_parser)
    export_parser.set_defaults(func=command_export)

    inspect_parser = sub.add_parser(
        "inspect", help="Write one standalone timestamp anomaly HTML report."
    )
    inspect_parser.add_argument("session_dir", type=Path)
    inspect_parser.add_argument("--segment", default=None)
    inspect_parser.add_argument("--output", type=Path, default=None)
    inspect_parser.add_argument("--ffprobe", default="ffprobe")
    inspect_parser.add_argument("--ffmpeg", default="ffmpeg")
    inspect_parser.set_defaults(func=command_inspect)

    offset_parser = sub.add_parser(
        "inspect-offset", help="Write one video-to-NOKOV frame mapping table."
    )
    offset_parser.add_argument("session_dir", type=Path)
    offset_parser.add_argument("--segment", default=None)
    offset_parser.add_argument("--ratio", default="auto")
    offset_parser.add_argument("--offset", type=int, required=True)
    offset_parser.add_argument("--nokov-source", type=Path, default=None)
    offset_parser.add_argument("--output", type=Path, default=None)
    offset_parser.add_argument("--ffprobe", default="ffprobe")
    offset_parser.add_argument("--ffmpeg", default="ffmpeg")
    offset_parser.set_defaults(func=command_inspect_offset)

    sweep_parser = sub.add_parser("sweep-offset", help="Write an offset range sanity table.")
    sweep_parser.add_argument("session_dir", type=Path)
    sweep_parser.add_argument("--segment", default=None)
    sweep_parser.add_argument("--ratio", default="auto")
    sweep_parser.add_argument("--offset-min", type=int, required=True)
    sweep_parser.add_argument("--offset-max", type=int, required=True)
    sweep_parser.add_argument("--nokov-source", type=Path, default=None)
    sweep_parser.add_argument("--output", type=Path, default=None)
    sweep_parser.add_argument("--ffprobe", default="ffprobe")
    sweep_parser.add_argument("--ffmpeg", default="ffmpeg")
    sweep_parser.set_defaults(func=command_sweep_offset)

    package_parser = sub.add_parser(
        "package-data", help="Package one session, using compressed proxy videos by default."
    )
    package_parser.add_argument("session_dir", type=Path)
    package_parser.add_argument("--output", type=Path, default=None)
    package_parser.add_argument("--segment", default=None)
    package_parser.add_argument(
        "--raw-video",
        action="store_true",
        help="Copy original videos instead of compressed proxy MP4.",
    )
    package_parser.add_argument("--proxy-height", type=int, default=540)
    package_parser.add_argument("--proxy-crf", type=int, default=28)
    package_parser.add_argument("--proxy-bitrate", default="1400k")
    package_parser.add_argument("--ffmpeg", default="ffmpeg")
    package_parser.add_argument("--include-artifacts", action="store_true")
    package_parser.add_argument("--include-rrd", action="store_true")
    package_parser.add_argument("--dry-run", action="store_true")
    package_parser.set_defaults(func=command_package_data)

    modelscope_stage_parser = sub.add_parser(
        "modelscope-stage",
        help="Prepare one session in a PXX/session_id ModelScope dataset tree.",
    )
    modelscope_stage_parser.add_argument("session_dir", type=Path)
    modelscope_stage_parser.add_argument("--primitive-id", required=True)
    modelscope_stage_parser.add_argument("--dataset-root", type=Path, default=None)
    modelscope_stage_parser.add_argument("--session-id", default=None)
    modelscope_stage_parser.add_argument("--segment", default=None)
    modelscope_stage_parser.add_argument(
        "--raw-video",
        action="store_true",
        help="Copy original videos instead of compressed proxy MP4.",
    )
    modelscope_stage_parser.add_argument("--proxy-height", type=int, default=540)
    modelscope_stage_parser.add_argument("--proxy-crf", type=int, default=28)
    modelscope_stage_parser.add_argument("--proxy-bitrate", default="1400k")
    modelscope_stage_parser.add_argument("--ffmpeg", default="ffmpeg")
    modelscope_stage_parser.add_argument("--ffprobe", default="ffprobe")
    modelscope_stage_parser.add_argument("--include-rrd", action="store_true")
    modelscope_stage_parser.add_argument(
        "--refresh-inspection",
        action="store_true",
        help="Regenerate the timestamp inspection HTML before staging.",
    )
    modelscope_stage_parser.add_argument("--dry-run", action="store_true")
    modelscope_stage_parser.set_defaults(func=command_modelscope_stage)

    modelscope_auth_parser = sub.add_parser(
        "modelscope-auth", help="Validate the ModelScope token loaded from the local .env file."
    )
    modelscope_auth_parser.set_defaults(func=command_modelscope_auth)

    modelscope_upload_parser = sub.add_parser(
        "modelscope-upload", help="Upload all sessions in one prepared dataset root."
    )
    modelscope_upload_parser.add_argument("dataset_root", type=Path)
    modelscope_upload_parser.add_argument("--repo-id", required=True)
    modelscope_upload_parser.add_argument("--revision", default="master")
    modelscope_upload_parser.add_argument("--create-if-missing", action="store_true")
    modelscope_upload_parser.add_argument(
        "--visibility", choices=("private", "internal", "public"), default="private"
    )
    modelscope_upload_parser.add_argument("--license", dest="license_name", default=None)
    modelscope_upload_parser.add_argument("--commit-message", default=None)
    modelscope_upload_parser.add_argument("--max-workers", type=int, default=None)
    modelscope_upload_parser.add_argument(
        "--no-cache", dest="use_cache", action="store_false", help="Disable resumable upload cache."
    )
    modelscope_upload_parser.set_defaults(func=command_modelscope_upload, use_cache=True)

    web_parser = sub.add_parser("web", help="Start a local browser UI.")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=7860)
    web_parser.add_argument("--open", action="store_true", help="Open the browser automatically.")
    web_parser.set_defaults(func=command_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
