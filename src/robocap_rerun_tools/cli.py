from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv"}
TEXT_SUFFIXES = {".csv", ".tsv", ".trc", ".bvh", ".xrs"}
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
    ratio: float


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
                "stream=nb_frames,r_frame_rate,avg_frame_rate,duration",
                "-show_entries",
                "format=duration,tags",
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


def video_summary(path: Path, ffprobe: str) -> StreamSummary:
    data, ffprobe_error = ffprobe_video(path, ffprobe)
    if not data:
        try:
            import rerun as rr

            frame_timestamps_ns = list(rr.AssetVideo(path=path).read_frame_timestamps_nanos())
        except Exception as exc:
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
            )
        times_s = [float(value) / 1e9 for value in frame_timestamps_ns]
        summary = summarize_times(path, "video", times_s)
        if summary.abnormal_reason:
            reason = f"{ffprobe_error}; Rerun fallback used; {summary.abnormal_reason}"
        else:
            reason = f"{ffprobe_error}; Rerun fallback used"
        return StreamSummary(
            path,
            summary.kind,
            summary.frame_count,
            summary.fps,
            summary.start_s,
            summary.end_s,
            summary.median_dt_ms,
            summary.min_dt_ms,
            summary.max_dt_ms,
            summary.abnormal_count,
            reason,
        )
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    fps = ratio_to_float(stream.get("avg_frame_rate")) or ratio_to_float(stream.get("r_frame_rate"))
    duration = float(stream.get("duration") or fmt.get("duration") or 0.0) or None
    frame_count = int(stream["nb_frames"]) if str(stream.get("nb_frames", "")).isdigit() else None
    if frame_count is None and duration and fps:
        frame_count = int(round(duration * fps))
    median_dt = 1000.0 / fps if fps else None
    end_s = (
        duration
        if duration is not None
        else ((frame_count - 1) / fps if frame_count and fps and frame_count > 0 else None)
    )
    return StreamSummary(
        path, "video", frame_count, fps, 0.0, end_s, median_dt, median_dt, median_dt, 0, ""
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
            0,
            "",
        )
    diffs_ms = [(b - a) * 1000.0 for a, b in zip(times_s, times_s[1:]) if b >= a]
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
            1,
            "non-monotonic timestamps",
        )
    med = statistics.median(diffs_ms)
    fps = 1000.0 / med if med > 0 else None
    tolerance = max(2.0, med * 0.2)
    abnormal = [d for d in diffs_ms if abs(d - med) > tolerance]
    reason = ""
    if abnormal:
        reason = f"{len(abnormal)} intervals differ from median by > max(2ms, 20%)"
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
        len(abnormal),
        reason,
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
    return StreamSummary(path, "bvh", frames, fps, 0.0, end_s, dt_ms, dt_ms, dt_ms, 0, "")


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
            hierarchical = hierarchical_csv_times(path)
            if hierarchical is None:
                return StreamSummary(
                    path, "csv", None, None, None, None, None, None, None, 1, "no known time column"
                )
            return summarize_times(path, "csv", hierarchical)
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
    diffs = [b - a for a, b in zip(vals, vals[1:]) if b > a]
    median_diff = statistics.median(diffs) if diffs else 0.0
    max_abs = max(abs(v) for v in vals)
    if median_diff >= 1_000_000:
        return [v / 1e9 for v in vals]
    if median_diff >= 1_000:
        return [v / 1e6 for v in vals]
    if max_abs > 1e9 and median_diff >= 0.5:
        return [v / 1000.0 for v in vals]
    return vals


def hierarchical_csv_times(path: Path) -> list[float] | None:
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
    vals: list[float] = []
    for row in rows[header_index + 1 :]:
        if len(row) <= time_index:
            continue
        try:
            vals.append(float(row[time_index]))
        except ValueError:
            continue
    if not vals:
        return None
    return normalize_time_values(vals, "timestamp")


def xrs_summary(path: Path) -> StreamSummary:
    header_index = None
    time_index = None
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            cells = stripped.split()
            rows.append(cells)
            lowered = [cell.lower() for cell in cells]
            if "timestamp" in lowered:
                header_index = len(rows) - 1
                time_index = lowered.index("timestamp")
                if time_index == 0:
                    time_index = 1
    if header_index is None or time_index is None:
        return StreamSummary(
            path, "xrs", None, None, None, None, None, None, None, 1, "missing XRS Timestamp header"
        )
    vals: list[float] = []
    for row in rows[header_index + 1 :]:
        if len(row) <= time_index:
            continue
        try:
            vals.append(float(row[time_index]))
        except ValueError:
            continue
    if not vals:
        return StreamSummary(
            path, "xrs", 0, None, None, None, None, None, None, 1, "no XRS timestamp rows"
        )
    return summarize_times(path, "xrs", normalize_time_values(vals, "timestamp"))


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


def discover_files(session_dir: Path) -> list[Path]:
    ignored_names = {"manifest.json", "manifest.tsv"}
    return sorted(
        p
        for p in session_dir.rglob("*")
        if p.is_file()
        and p.name.lower() not in ignored_names
        and "_artifacts" not in p.relative_to(session_dir).parts
        and (p.suffix.lower() in VIDEO_SUFFIXES or p.suffix.lower() in TEXT_SUFFIXES)
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


def fmt(value: float | int | None, digits: int = 6) -> str:
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
    return FrameRatioEstimate(
        source=source,
        report_path=report_path,
        gt_sample_count=len(gt_fps),
        robocap_sample_count=len(robocap_fps),
        gt_fps_mean=gt_mean,
        robocap_fps_mean=robocap_mean,
        gt_fps_rounded_10=gt_rounded,
        robocap_fps_rounded_10=robocap_rounded,
        ratio=gt_rounded / robocap_rounded,
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


def parse_inspection_markdown_fps(report_path: Path) -> list[FpsRecord]:
    records: list[FpsRecord] = []
    headers: list[str] | None = None
    for line in report_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.lstrip().startswith("|"):
            if headers is not None:
                break
            continue
        cells = next(csv.reader([line], delimiter="|"))
        if cells and not cells[0].strip():
            cells = cells[1:]
        if cells and not cells[-1].strip():
            cells = cells[:-1]
        cells = [cell.strip() for cell in cells]
        lowered = [cell.lower() for cell in cells]
        if headers is None:
            if {"file", "kind", "fps"}.issubset(lowered):
                headers = lowered
            continue
        if all(cell.replace(":", "").replace("-", "") == "" for cell in cells):
            continue
        row = dict(zip(headers, cells))
        path = row.get("file", "").strip().strip("`")
        kind = row.get("kind", "").strip().lower()
        try:
            fps = float(row.get("fps", ""))
        except ValueError:
            continue
        if not path or not kind or not math.isfinite(fps) or fps <= 0:
            continue
        source = row.get("source", "").strip().lower()
        if source not in {"gt", "robocap", "other"}:
            source = infer_fps_source(path, kind)
        records.append(FpsRecord(path=path, kind=kind, source=source, fps=fps))
    return records


def load_frame_ratio_estimate(report_path: Path) -> FrameRatioEstimate | None:
    try:
        records = parse_inspection_markdown_fps(report_path)
    except OSError:
        return None
    return estimate_frame_ratio(records, "frame_rate_report.md", report_path)


def inspection_output_dir(session_dir: Path, segment: str | None) -> Path:
    return session_dir / "_artifacts" / (segment or "all") / "inspection"


def inspection_files(session_dir: Path, segment: str | None) -> list[Path]:
    files = discover_files(session_dir)
    if segment:
        files = [
            path
            for path in files
            if segment in path.name or path.suffix.lower() not in VIDEO_SUFFIXES
        ]
    return files


def write_inspection(
    session_dir: Path, segment: str | None, summaries: list[StreamSummary], out_dir: Path
) -> FrameRatioEstimate | None:
    report_path = out_dir / "frame_rate_report.md"
    fps_records = [
        record
        for item in summaries
        if (record := fps_record_from_summary(item, session_dir)) is not None
    ]
    ratio_estimate = estimate_frame_ratio(
        fps_records, "frame_rate_report.md", report_path=report_path
    )
    rows = [
        [
            "path",
            "kind",
            "source",
            "frames",
            "fps",
            "start_s",
            "end_s",
            "duration_s",
            "median_dt_ms",
            "min_dt_ms",
            "max_dt_ms",
            "abnormal_intervals",
            "abnormal_reason",
        ]
    ]
    md = [
        f"# Robocap/NOKOV inspection",
        "",
        f"- session: `{session_dir}`",
        f"- segment: `{segment or 'auto/all'}`",
        "",
        "| file | kind | source | frames | fps | start_s | end_s | median_dt_ms | abnormal |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        rel = (
            item.path.relative_to(session_dir)
            if item.path.is_relative_to(session_dir)
            else item.path
        )
        duration = (
            item.end_s - item.start_s
            if item.start_s is not None and item.end_s is not None
            else None
        )
        source = infer_fps_source(rel, item.kind)
        rows.append(
            [
                str(rel),
                item.kind,
                source,
                fmt(item.frame_count),
                fmt(item.fps, 9),
                fmt(item.start_s, 6),
                fmt(item.end_s, 6),
                fmt(duration, 6),
                fmt(item.median_dt_ms, 6),
                fmt(item.min_dt_ms, 6),
                fmt(item.max_dt_ms, 6),
                str(item.abnormal_count),
                item.abnormal_reason,
            ]
        )
        md.append(
            f"| `{rel}` | {item.kind} | {source} | {fmt(item.frame_count)} | "
            f"{fmt(item.fps, 9)} | "
            f"{fmt(item.start_s, 3)} | {fmt(item.end_s, 3)} | {fmt(item.median_dt_ms, 3)} | "
            f"{item.abnormal_count} |"
        )
    md.extend(["", "## Auto frame ratio", ""])
    if ratio_estimate is None:
        md.append("- unavailable: need at least one GT FPS and one Robocap video FPS")
    else:
        md.extend(
            [
                f"- gt_fps_samples: {ratio_estimate.gt_sample_count}",
                f"- gt_fps_mean: {ratio_estimate.gt_fps_mean:.9f}",
                f"- gt_fps_rounded_10: {ratio_estimate.gt_fps_rounded_10}",
                f"- robocap_fps_samples: {ratio_estimate.robocap_sample_count}",
                f"- robocap_fps_mean: {ratio_estimate.robocap_fps_mean:.9f}",
                f"- robocap_fps_rounded_10: {ratio_estimate.robocap_fps_rounded_10}",
                f"- auto_ratio: {ratio_estimate.ratio:.9f}",
                "- formula: `rounded GT FPS / rounded Robocap FPS`",
            ]
        )
    abnormal = [s for s in summaries if s.abnormal_count or s.abnormal_reason]
    md.extend(["", "## Abnormal intervals", ""])
    if abnormal:
        for item in abnormal:
            rel = (
                item.path.relative_to(session_dir)
                if item.path.is_relative_to(session_dir)
                else item.path
            )
            md.append(f"- `{rel}`: {item.abnormal_reason or 'summary unavailable'}")
    else:
        md.append("- No abnormal text-stream intervals detected by the median-delta rule.")
    write_text(report_path, "\n".join(md) + "\n")
    with (out_dir / "frame_rate_report.tsv").open("w", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter="\t").writerows(rows)
    return ratio_estimate


def resolve_session_auto_ratio(
    session_dir: Path, segment: str | None, ffprobe: str
) -> FrameRatioEstimate | None:
    out_dir = inspection_output_dir(session_dir, segment)
    report_path = out_dir / "frame_rate_report.md"
    if report_path.exists():
        estimate = load_frame_ratio_estimate(report_path)
        if estimate is not None:
            return estimate
    files = inspection_files(session_dir, segment)
    summaries = [summarize_file(path, ffprobe) for path in files]
    return write_inspection(session_dir, segment, summaries, out_dir)


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
    ]
    if estimate.report_path is not None:
        lines.insert(1, f"- auto_ratio_report: `{estimate.report_path}`")
    return lines


def auto_ratio_console_summary(estimate: FrameRatioEstimate) -> str:
    return (
        f"Auto ratio {estimate.ratio:.9f}: GT mean {estimate.gt_fps_mean:.6f} "
        f"-> {estimate.gt_fps_rounded_10}, Robocap mean "
        f"{estimate.robocap_fps_mean:.6f} -> {estimate.robocap_fps_rounded_10} "
        f"({estimate.source})"
    )


def video_to_gt_frame_float(video_frame: int, ratio: float, video_frame_offset: int) -> float:
    return (video_frame + video_frame_offset) * ratio


def video_to_gt_frame(video_frame: int, ratio: float, video_frame_offset: int) -> int:
    return int(round(video_to_gt_frame_float(video_frame, ratio, video_frame_offset)))


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
    max_rows = min(frame_count, max(0, math.ceil(nokov_count / ratio - offset))) if ratio > 0 else 0
    for video_frame in range(max_rows):
        expected_float = video_frame * ratio
        expected = int(round(expected_float))
        offset_video_frame = video_frame + offset
        offset_float = video_to_gt_frame_float(video_frame, ratio, offset)
        offset_frame = int(round(offset_float))
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
        f"- equivalent_nokov_offset_float: {offset * ratio:.9f}",
        f"- comparable_video_frames: {max_rows}",
    ]
    if ratio_estimate is not None:
        md.extend(auto_ratio_markdown_lines(ratio_estimate))
    md.extend(
        [
            "",
            "The mapping is: `video frame N -> NOKOV frame round((N + offset) * main_ratio)`.",
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
            "Frame mode NOKOV/video ratio. Default auto reads frame_rate_report.md, averages GT "
            "and Robocap FPS separately, rounds both means to the nearest 10, then divides them."
        ),
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help=(
            "Frame mode offset in Robocap video frames. Mapping: GT frame = "
            "round((video frame + offset) * ratio)."
        ),
    )
    parser.add_argument("--reference-video", default="left")
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
            resolved_ratio = f"{ratio_estimate.ratio:.9f}"

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
    summaries = [summarize_file(path, ffprobe) for path in files]
    out_dir = args.output or inspection_output_dir(args.session_dir, args.segment)
    write_inspection(args.session_dir, args.segment, summaries, out_dir)
    print(f"Wrote inspection reports to {out_dir}")
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
        "inspect", help="Write frame/FPS and abnormal interval reports."
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
