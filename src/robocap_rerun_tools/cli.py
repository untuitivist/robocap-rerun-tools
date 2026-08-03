from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv"}
TEXT_SUFFIXES = {".csv", ".tsv", ".trc", ".bvh"}


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


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def open_csv(path: Path):
    return path.open("r", encoding="utf-8-sig", newline="")


def run_json(command: list[str]) -> dict:
    proc = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(proc.stdout)


def ffprobe_video(path: Path, ffprobe: str) -> dict | None:
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
        )
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None


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
    data = ffprobe_video(path, ffprobe)
    if not data:
        return StreamSummary(path, "video", None, None, None, None, None, None, None, 1, "ffprobe failed")
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    fps = ratio_to_float(stream.get("avg_frame_rate")) or ratio_to_float(stream.get("r_frame_rate"))
    duration = float(stream.get("duration") or fmt.get("duration") or 0.0) or None
    frame_count = int(stream["nb_frames"]) if str(stream.get("nb_frames", "")).isdigit() else None
    if frame_count is None and duration and fps:
        frame_count = int(round(duration * fps))
    median_dt = 1000.0 / fps if fps else None
    end_s = duration if duration is not None else (
        (frame_count - 1) / fps if frame_count and fps and frame_count > 0 else None
    )
    return StreamSummary(path, "video", frame_count, fps, 0.0, end_s, median_dt, median_dt, median_dt, 0, "")


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
        return StreamSummary(path, kind, len(times_s), None, times_s[0] if times_s else None, times_s[-1] if times_s else None, None, None, None, 0, "")
    diffs_ms = [(b - a) * 1000.0 for a, b in zip(times_s, times_s[1:]) if b >= a]
    if not diffs_ms:
        return StreamSummary(path, kind, len(times_s), None, times_s[0], times_s[-1], None, None, None, 1, "non-monotonic timestamps")
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
    header_idx = next((i for i, row in enumerate(rows) if len(row) >= 2 and row[0] == "Frame#" and row[1] == "Time"), None)
    if header_idx is None:
        return StreamSummary(path, "trc", None, None, None, None, None, None, None, 1, "missing TRC Frame#/Time header")
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
        return StreamSummary(path, "bvh", frames, None, None, None, None, None, None, 1, "missing BVH Frames/Frame Time")
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
            dialect = csv.excel_tab if path.suffix.lower() == ".tsv" or "\t" in first_line else csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        if not reader.fieldnames:
            return StreamSummary(path, "csv", None, None, None, None, None, None, None, 1, "missing header")
        lower_map = {name.lower(): name for name in reader.fieldnames}
        time_col = next(
            (lower_map[key] for key in ("capture_time", "capture_time_ns", "timestamp", "time", "log_time") if key in lower_map),
            None,
        )
        if time_col is None:
            return StreamSummary(path, "csv", None, None, None, None, None, None, None, 1, "no known time column")
        raw = [row.get(time_col, "") for row in reader]
    vals = numeric(raw)
    if time_col.lower().endswith("_ns"):
        times = [v / 1e9 for v in vals]
    elif vals and max(abs(v) for v in vals) > 1e12:
        times = [v / 1e9 for v in vals]
    elif vals and max(abs(v) for v in vals) > 1e9:
        times = [v / 1e6 for v in vals]
    else:
        times = vals
    return summarize_times(path, "csv", times)


def discover_files(session_dir: Path) -> list[Path]:
    return sorted(
        p for p in session_dir.rglob("*") if p.is_file() and (p.suffix.lower() in VIDEO_SUFFIXES or p.suffix.lower() in TEXT_SUFFIXES)
    )


def summarize_file(path: Path, ffprobe: str) -> StreamSummary:
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return video_summary(path, ffprobe)
    if suffix == ".trc":
        return trc_summary(path)
    if suffix == ".bvh":
        return bvh_summary(path)
    if suffix in {".csv", ".tsv"}:
        return csv_summary(path)
    return StreamSummary(path, suffix.lstrip("."), None, None, None, None, None, None, None, 0, "")


def fmt(value: float | int | None, digits: int = 6) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def write_inspection(session_dir: Path, segment: str | None, summaries: list[StreamSummary], out_dir: Path) -> None:
    rows = [
        [
            "path",
            "kind",
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
        "| file | kind | frames | fps | start_s | end_s | median_dt_ms | abnormal |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        rel = item.path.relative_to(session_dir) if item.path.is_relative_to(session_dir) else item.path
        duration = item.end_s - item.start_s if item.start_s is not None and item.end_s is not None else None
        rows.append(
            [
                str(rel),
                item.kind,
                fmt(item.frame_count),
                fmt(item.fps, 6),
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
            f"| `{rel}` | {item.kind} | {fmt(item.frame_count)} | {fmt(item.fps, 6)} | "
            f"{fmt(item.start_s, 3)} | {fmt(item.end_s, 3)} | {fmt(item.median_dt_ms, 3)} | "
            f"{item.abnormal_count} |"
        )
    abnormal = [s for s in summaries if s.abnormal_count or s.abnormal_reason]
    md.extend(["", "## Abnormal intervals", ""])
    if abnormal:
        for item in abnormal:
            rel = item.path.relative_to(session_dir) if item.path.is_relative_to(session_dir) else item.path
            md.append(f"- `{rel}`: {item.abnormal_reason or 'summary unavailable'}")
    else:
        md.append("- No abnormal text-stream intervals detected by the median-delta rule.")
    write_text(out_dir / "frame_rate_report.md", "\n".join(md) + "\n")
    with (out_dir / "frame_rate_report.tsv").open("w", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter="\t").writerows(rows)


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
    header_idx = next(i for i, row in enumerate(rows) if len(row) >= 2 and row[0] == "Frame#" and row[1] == "Time")
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


def resolve_auto_ratio(video: StreamSummary, nokov: StreamSummary) -> float:
    if video.fps and nokov.fps and video.fps > 0:
        return nokov.fps / video.fps
    return 8.0


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
        raise FileNotFoundError("Need at least one video and one TRC/BVH file for offset inspection.")
    video = video_summary(video_path, ffprobe)
    nokov = bvh_summary(nokov_path) if nokov_path.suffix.lower() == ".bvh" else trc_summary(nokov_path)
    ratio = resolve_auto_ratio(video, nokov) if ratio_arg == "auto" else float(ratio_arg)
    frame_count = video.frame_count or 0
    nokov_count = nokov.frame_count or 0
    rows = [[
        "video_frame",
        "expected_nokov_frame_float",
        "expected_nokov_frame",
        "offset_nokov_frame",
        "expected_delta_frames",
        "expected_minus_8x",
    ]]
    max_rows = min(frame_count, max(0, math.ceil((nokov_count - offset) / ratio))) if ratio > 0 else 0
    for video_frame in range(max_rows):
        expected_float = video_frame * ratio
        expected = int(round(expected_float))
        offset_frame = expected + offset
        rows.append(
            [
                str(video_frame),
                f"{expected_float:.9f}",
                str(expected),
                str(offset_frame),
                str(offset_frame - expected),
                f"{offset_frame - video_frame * 8:.9f}",
            ]
        )
    with (out_dir / "video_to_nokov_frame_alignment.tsv").open("w", encoding="utf-8", newline="") as f:
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
        f"- comparable_video_frames: {max_rows}",
        "",
        "The mapping is: `video frame N -> NOKOV frame round(N * main_ratio) + offset`.",
        "`expected_minus_8x` is kept because a fixed 8x mapping is the historical baseline.",
    ]
    write_text(out_dir / "offset_inspection.md", "\n".join(md) + "\n")


def write_offset_sweep(
    session_dir: Path,
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
    nokov = bvh_summary(nokov_path) if nokov_path.suffix.lower() == ".bvh" else trc_summary(nokov_path)
    ratio = resolve_auto_ratio(video, nokov) if ratio_arg == "auto" else float(ratio_arg)
    rows = [["offset", "main_ratio", "video_fps", "nokov_fps", "last_video_frame", "last_nokov_frame", "tail_margin_frames"]]
    frame_count = video.frame_count or 0
    nokov_count = nokov.frame_count or 0
    for offset in range(offset_min, offset_max + 1):
        last_video = max(0, frame_count - 1)
        last_nokov = int(round(last_video * ratio)) + offset
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
                f"- ratio: {ratio:.9f}",
                f"- offsets: {offset_min}..{offset_max}",
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
    parser.add_argument("--ratio", default="auto", help="Frame mode NOKOV/video ratio. Use auto, 8, or a float.")
    parser.add_argument("--offset", type=int, default=0, help="Frame mode NOKOV frame offset.")
    parser.add_argument("--reference-video", default="left")
    parser.add_argument("--use-proxy", action="store_true")
    parser.add_argument("--proxy-height", type=int, default=540)
    parser.add_argument("--display", action="store_true", help="Use display blueprint preset.")
    parser.add_argument("--mano-model-dir", type=Path, default=Path("Z:/MODELS/hand_models/mano"))
    parser.add_argument("--no-mano-mesh", action="store_true")
    parser.add_argument("--gt-coordinate-scale", type=float, default=1.0)
    parser.add_argument("--bvh-coordinate-scale", type=float, default=0.01)
    parser.add_argument("--spawn", action="store_true")
    parser.add_argument("--inspect", action="store_true")


def command_export(args: argparse.Namespace) -> int:
    from robocap_rerun_tools import exporter

    argv = [
        "--session-dir",
        str(args.session_dir),
        "--gt-alignment-mode",
        args.mode,
        "--gt-frame-reference-video",
        args.reference_video,
        "--gt-frame-offset",
        str(args.offset),
        "--mano-model-dir",
        str(args.mano_model_dir),
        "--gt-coordinate-scale",
        str(args.gt_coordinate_scale),
        "--bvh-coordinate-scale",
        str(args.bvh_coordinate_scale),
        "--proxy-height",
        str(args.proxy_height),
    ]
    if args.segment:
        argv.extend(["--segment", args.segment])
    if args.save:
        argv.extend(["--save", str(args.save)])
    if args.mode == "frame" and args.ratio != "auto":
        argv.extend(["--gt-frame-ratio", args.ratio])
    if args.use_proxy:
        argv.append("--use-proxy")
    if args.display:
        argv.extend(["--blueprint-preset", "display"])
    if args.no_mano_mesh:
        argv.append("--no-mano-mesh")
    if args.spawn:
        argv.append("--spawn")
    if args.inspect:
        argv.append("--inspect")
    old_argv = sys.argv
    try:
        sys.argv = [old_argv[0], *argv]
        exporter.main()
    finally:
        sys.argv = old_argv
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    files = discover_files(args.session_dir)
    if args.segment:
        files = [p for p in files if args.segment in p.name or p.suffix.lower() not in VIDEO_SUFFIXES]
    summaries = [summarize_file(path, args.ffprobe) for path in files]
    out_dir = args.output or args.session_dir / "_artifacts" / (args.segment or "all") / "inspection"
    write_inspection(args.session_dir, args.segment, summaries, out_dir)
    print(f"Wrote inspection reports to {out_dir}")
    return 0


def command_inspect_offset(args: argparse.Namespace) -> int:
    out_dir = args.output or args.session_dir / "_artifacts" / (args.segment or "segment") / f"offset{args.offset}_inspection"
    write_offset_report(args.session_dir, args.segment, args.ratio, args.offset, args.nokov_source, out_dir, args.ffprobe)
    print(f"Wrote offset inspection to {out_dir}")
    return 0


def command_sweep_offset(args: argparse.Namespace) -> int:
    out_dir = args.output or args.session_dir / "_artifacts" / (args.segment or "segment") / "offset_sweep"
    write_offset_sweep(args.session_dir, args.ratio, args.offset_min, args.offset_max, args.nokov_source, out_dir, args.ffprobe)
    print(f"Wrote offset sweep to {out_dir}")
    return 0


def command_package_data(args: argparse.Namespace) -> int:
    from robocap_rerun_tools.data_packager import package_session

    package_session(args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robocap-rerun")
    sub = parser.add_subparsers(dest="command", required=True)

    export_parser = sub.add_parser("export", help="Generate a time-aligned or frame-aligned RRD.")
    add_common_export_args(export_parser)
    export_parser.set_defaults(func=command_export)

    inspect_parser = sub.add_parser("inspect", help="Write frame/FPS and abnormal interval reports.")
    inspect_parser.add_argument("session_dir", type=Path)
    inspect_parser.add_argument("--segment", default=None)
    inspect_parser.add_argument("--output", type=Path, default=None)
    inspect_parser.add_argument("--ffprobe", default="ffprobe")
    inspect_parser.set_defaults(func=command_inspect)

    offset_parser = sub.add_parser("inspect-offset", help="Write one video-to-NOKOV frame mapping table.")
    offset_parser.add_argument("session_dir", type=Path)
    offset_parser.add_argument("--segment", default=None)
    offset_parser.add_argument("--ratio", default="auto")
    offset_parser.add_argument("--offset", type=int, required=True)
    offset_parser.add_argument("--nokov-source", type=Path, default=None)
    offset_parser.add_argument("--output", type=Path, default=None)
    offset_parser.add_argument("--ffprobe", default="ffprobe")
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
    sweep_parser.set_defaults(func=command_sweep_offset)

    package_parser = sub.add_parser("package-data", help="Package one session, using compressed proxy videos by default.")
    package_parser.add_argument("session_dir", type=Path)
    package_parser.add_argument("--output", type=Path, default=None)
    package_parser.add_argument("--segment", default=None)
    package_parser.add_argument("--raw-video", action="store_true", help="Copy original videos instead of compressed proxy MP4.")
    package_parser.add_argument("--proxy-height", type=int, default=540)
    package_parser.add_argument("--proxy-crf", type=int, default=28)
    package_parser.add_argument("--proxy-bitrate", default="1400k")
    package_parser.add_argument("--ffmpeg", default="ffmpeg")
    package_parser.add_argument("--include-artifacts", action="store_true")
    package_parser.add_argument("--include-rrd", action="store_true")
    package_parser.add_argument("--dry-run", action="store_true")
    package_parser.set_defaults(func=command_package_data)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
