from __future__ import annotations

import csv
import html
import json
import re
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from .dataset_statistics import (
    classify_frame_count_anomalies,
    format_duration,
    load_report_payload,
    session_frame_anomaly_labels,
    summarize_session,
)
from .mocap_metadata import parse_mocap_capture_directory
from .session_layout import discover_mocap_directories

REPORTS_DIRECTORY = "_reports"
SESSION_TIMESTAMP_PATTERN = re.compile(r"(?<!\d)(\d{8})_(\d{6})(?!\d)")
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class StatisticsReportResult:
    output_dir: Path
    html_path: Path
    zip_path: Path
    sessions_csv: Path
    segments_csv: Path
    inspections_csv: Path
    files_csv: Path
    session_count: int
    segment_count: int
    file_count: int


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _session_times(name: str) -> tuple[str, str]:
    match = SESSION_TIMESTAMP_PATTERN.search(name)
    if match is None:
        return "", ""
    try:
        utc_value = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return "", ""
    return utc_value.isoformat(), utc_value.astimezone(SHANGHAI_TIMEZONE).isoformat()


def _file_category(path: Path, session_dir: Path) -> str:
    relative = path.relative_to(session_dir)
    parts = tuple(part.casefold() for part in relative.parts)
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if any(part.startswith("mocap") for part in parts[:-1]):
        return "mocap"
    if "_artifacts" in parts:
        if suffix == ".rrd":
            return "rerun"
        if name == "timestamp_anomaly_detail_table.html":
            return "inspection_report"
        return "artifact"
    if suffix in {".mp4", ".avi", ".mov", ".mkv"}:
        return "robocap_video" if name.startswith("robocap_") else "video"
    if suffix in {".db", ".sqlite", ".sqlite3"}:
        return "sensor_database"
    if suffix in {".bvh", ".c3d", ".csv", ".trc", ".xrs"}:
        return "motion_data"
    return "other"


def _scalar_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): value
        for key, value in payload.items()
        if value is None or isinstance(value, (bool, int, float, str))
    }


def _inspection_row(
    root: Path,
    session_relative_path: str,
    segment: str,
    report_path: Path,
) -> dict[str, object]:
    base: dict[str, object] = {
        "session_relative_path": session_relative_path,
        "segment": segment,
        "report_relative_path": _relative(report_path, root),
        "parse_status": "ok",
        "parse_error": "",
        "ratio": "",
        "reference_frames": "",
        "mocap_frames": "",
        "third_person_frames": "",
        "expected_mocap_frames": "",
        "mocap_frame_difference": "",
        "expected_third_person_frames": "",
        "third_person_frame_difference": "",
        "frame_anomalies": "",
        "payload_scalar_json": "",
    }
    try:
        payload = load_report_payload(report_path)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        base["parse_status"] = "error"
        base["parse_error"] = str(exc)
        return base

    ratio = payload.get("ratio")
    reference_frames = payload.get("referenceFrames")
    mocap_frames = payload.get("mocapFrames")
    third_frames = payload.get("thirdFrames")
    base.update(
        {
            "ratio": ratio,
            "reference_frames": reference_frames,
            "mocap_frames": mocap_frames,
            "third_person_frames": third_frames,
            "payload_scalar_json": json.dumps(
                _scalar_payload(payload), ensure_ascii=False, sort_keys=True
            ),
        }
    )
    if all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (ratio, reference_frames)
    ):
        expected_mocap = ratio * (reference_frames + 1)
        expected_third = reference_frames + 1
        base["expected_mocap_frames"] = expected_mocap
        base["expected_third_person_frames"] = expected_third
        if isinstance(mocap_frames, int) and not isinstance(mocap_frames, bool):
            base["mocap_frame_difference"] = mocap_frames - expected_mocap
        if isinstance(third_frames, int) and not isinstance(third_frames, bool):
            base["third_person_frame_difference"] = third_frames - expected_third
    anomalies = classify_frame_count_anomalies(payload)
    base["frame_anomalies"] = "|".join(anomalies or ()) if anomalies is not None else "invalid"
    return base


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]], fields: Sequence[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _table(title: str, rows: Sequence[Mapping[str, object]], fields: Sequence[str]) -> str:
    heading = "".join(f"<th>{html.escape(field)}</th>" for field in fields)
    body = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(row.get(field, '')))}</td>" for field in fields)
        body.append(f"<tr>{cells}</tr>")
    return (
        f'<section><h2>{html.escape(title)}</h2><input class="filter" '
        f'placeholder="Filter {html.escape(title)}" aria-label="Filter {html.escape(title)}">'
        f'<div class="table-wrap"><table><thead><tr>{heading}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div></section>"
    )


def _render_html(
    root: Path,
    generated_at: datetime,
    sessions: Sequence[Mapping[str, object]],
    segments: Sequence[Mapping[str, object]],
    inspections: Sequence[Mapping[str, object]],
    files: Sequence[Mapping[str, object]],
    extension_counts: Mapping[str, int],
    status_counts: Mapping[str, int],
) -> str:
    total_duration = sum(float(row["duration_s"]) for row in sessions)
    metadata_count = sum(int(row["mocap_metadata_count"]) for row in sessions)
    file_bytes = sum(int(row["size_bytes"]) for row in files)
    session_fields = list(sessions[0]) if sessions else SESSION_FIELDS
    segment_fields = list(segments[0]) if segments else SEGMENT_FIELDS
    inspection_fields = list(inspections[0]) if inspections else INSPECTION_FIELDS
    file_fields = list(files[0]) if files else FILE_FIELDS
    summary_json = json.dumps(
        {
            "extensions": extension_counts,
            "inspection_statuses": status_counts,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Robocap Statistics Report</title>
<style>
:root{{--bg:#f6f7f9;--panel:#fff;--text:#17202a;--muted:#5d6875;--line:#d9dee5;--accent:#064e8a}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,"Microsoft YaHei",sans-serif}}
header,main{{max-width:1600px;margin:auto;padding:20px}}header{{background:#fff;border-bottom:1px solid var(--line);max-width:none}}
h1{{margin:0 0 6px;font-size:24px}}h2{{font-size:18px;margin:0 0 10px}}.meta{{color:var(--muted);overflow-wrap:anywhere}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:16px 0}}
.metric{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px}}.metric b{{display:block;font-size:20px;color:var(--accent)}}
section{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px;margin:14px 0}}
.filter{{width:min(520px,100%);padding:8px;border:1px solid #aab3bd;border-radius:4px;margin-bottom:10px}}
.table-wrap{{overflow:auto;max-height:620px}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}
th,td{{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top;max-width:560px;overflow:hidden;text-overflow:ellipsis}}
th{{position:sticky;top:0;background:#eaf1f7;color:#173b57;z-index:1}}tr:nth-child(even){{background:#fafbfd}}
code{{white-space:pre-wrap;overflow-wrap:anywhere}}
</style></head><body>
<header><h1>Robocap 完整统计报告 / Complete Statistics Report</h1>
<div class="meta">根目录 / Root: {html.escape(str(root))}<br>生成时间 / Generated: {generated_at.isoformat()}</div></header>
<main><div class="metrics">
<div class="metric">Sessions<b>{len(sessions)}</b></div><div class="metric">Segments<b>{len(segments)}</b></div>
<div class="metric">Duration<b>{format_duration(total_duration)}</b></div><div class="metric">Files<b>{len(files)}</b></div>
<div class="metric">Bytes<b>{file_bytes:,}</b></div><div class="metric">Parsed Mocap metadata<b>{metadata_count}</b></div>
</div>
<section><h2>汇总 / Summary</h2><code>{html.escape(summary_json)}</code></section>
{_table("Sessions", sessions, session_fields)}
{_table("Segments", segments, segment_fields)}
{_table("Inspections", inspections, inspection_fields)}
{_table("Files", files, file_fields)}
</main><script>
document.querySelectorAll('section').forEach(section=>{{const input=section.querySelector('.filter');if(!input)return;
const rows=[...section.querySelectorAll('tbody tr')];input.addEventListener('input',()=>{{const q=input.value.toLocaleLowerCase();
rows.forEach(row=>row.hidden=!row.textContent.toLocaleLowerCase().includes(q));}});}});
</script></body></html>"""


SESSION_FIELDS = [
    "session_relative_path",
    "session_name",
    "primitive_id",
    "duration_s",
    "duration_hms",
    "inspection_status",
    "inspection_anomalies",
    "inspection_errors",
    "session_time_utc",
    "session_time_utc_plus_8",
    "mocap_directories",
    "mocap_metadata_count",
    "action_ids",
    "collection_session_indexes",
    "participants",
    "repetition_counts",
    "mocap_metadata_json",
    "file_count",
    "total_size_bytes",
    "extension_counts_json",
]
SEGMENT_FIELDS = [
    "session_relative_path",
    "segment",
    "reference_video_relative_path",
    "duration_s",
    "duration_hms",
    "inspection_report_relative_path",
    "inspection_status",
    "frame_anomalies",
    "detail",
]
INSPECTION_FIELDS = [
    "session_relative_path",
    "segment",
    "report_relative_path",
    "parse_status",
    "parse_error",
    "ratio",
    "reference_frames",
    "mocap_frames",
    "third_person_frames",
    "expected_mocap_frames",
    "mocap_frame_difference",
    "expected_third_person_frames",
    "third_person_frame_difference",
    "frame_anomalies",
    "payload_scalar_json",
]
FILE_FIELDS = [
    "session_relative_path",
    "root_relative_path",
    "session_relative_file_path",
    "category",
    "extension",
    "size_bytes",
    "modified_time_utc_plus_8",
]


def generate_statistics_report(
    dataset_root: Path,
    session_directories: Iterable[Path],
    ffprobe: str,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    generated_at: datetime | None = None,
) -> StatisticsReportResult:
    root = dataset_root.expanduser().resolve()
    sessions = sorted(
        {path.expanduser().resolve() for path in session_directories},
        key=lambda path: _relative(path, root).casefold(),
    )
    now = (generated_at or datetime.now(SHANGHAI_TIMEZONE)).astimezone(SHANGHAI_TIMEZONE)
    reports_root = root / REPORTS_DIRECTORY
    reports_root.mkdir(parents=True, exist_ok=True)
    stem = f"statistics_report_{now:%Y%m%d_%H%M%S}"
    output_dir = reports_root / stem
    serial = 2
    while output_dir.exists() or (reports_root / f"{output_dir.name}.zip").exists():
        output_dir = reports_root / f"{stem}_{serial}"
        serial += 1
    output_dir.mkdir(parents=True)
    zip_path = reports_root / f"{output_dir.name}.zip"

    session_rows: list[dict[str, object]] = []
    segment_rows: list[dict[str, object]] = []
    inspection_rows: list[dict[str, object]] = []
    file_rows: list[dict[str, object]] = []
    extension_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()

    for index, session_dir in enumerate(sessions, start=1):
        session_relative = _relative(session_dir, root)
        if progress is not None:
            progress(index, len(sessions), session_relative)
        statistic = summarize_session(root, session_dir, ffprobe)
        mocap_dirs = discover_mocap_directories(session_dir)
        metadata = [
            parsed
            for mocap_dir in mocap_dirs
            if (parsed := parse_mocap_capture_directory(mocap_dir.name)) is not None
        ]
        utc_time, local_time = _session_times(session_dir.name)
        session_files: list[Path] = []
        try:
            candidates = session_dir.rglob("*")
            for path in candidates:
                if REPORTS_DIRECTORY in {part.casefold() for part in path.parts}:
                    continue
                if path.is_file():
                    session_files.append(path)
        except OSError:
            pass
        session_extensions: Counter[str] = Counter()
        session_bytes = 0
        session_file_count = 0
        for path in sorted(session_files, key=lambda item: str(item).casefold()):
            try:
                file_stat = path.stat()
            except OSError:
                continue
            suffix = path.suffix.casefold() or "[no extension]"
            session_extensions[suffix] += 1
            extension_counts[suffix] += 1
            session_bytes += file_stat.st_size
            session_file_count += 1
            file_rows.append(
                {
                    "session_relative_path": session_relative,
                    "root_relative_path": _relative(path, root),
                    "session_relative_file_path": path.relative_to(session_dir).as_posix(),
                    "category": _file_category(path, session_dir),
                    "extension": suffix,
                    "size_bytes": file_stat.st_size,
                    "modified_time_utc_plus_8": datetime.fromtimestamp(
                        file_stat.st_mtime, tz=SHANGHAI_TIMEZONE
                    ).isoformat(),
                }
            )

        labels = session_frame_anomaly_labels(statistic, language="English")
        if not statistic.segments or any(item.status == "unchecked" for item in statistic.segments):
            inspection_status = "unchecked"
        elif any(item.status == "frame_difference" for item in statistic.segments):
            inspection_status = "frame_difference"
        else:
            inspection_status = "clean"
        status_counts[inspection_status] += 1
        session_rows.append(
            {
                "session_relative_path": session_relative,
                "session_name": session_dir.name,
                "primitive_id": statistic.primitive_id,
                "duration_s": round(statistic.duration_s, 6),
                "duration_hms": format_duration(statistic.duration_s),
                "inspection_status": inspection_status,
                "inspection_anomalies": "|".join(labels),
                "inspection_errors": " | ".join(statistic.errors),
                "session_time_utc": utc_time,
                "session_time_utc_plus_8": local_time,
                "mocap_directories": "|".join(_relative(path, root) for path in mocap_dirs),
                "mocap_metadata_count": len(metadata),
                "action_ids": "|".join(item.action_id for item in metadata),
                "collection_session_indexes": "|".join(
                    str(item.collection_session_index) for item in metadata
                ),
                "participants": "|".join(item.participant for item in metadata),
                "repetition_counts": "|".join(str(item.repetition_count) for item in metadata),
                "mocap_metadata_json": json.dumps(
                    [asdict(item) for item in metadata], ensure_ascii=False, sort_keys=True
                ),
                "file_count": session_file_count,
                "total_size_bytes": session_bytes,
                "extension_counts_json": json.dumps(
                    dict(sorted(session_extensions.items())), ensure_ascii=False
                ),
            }
        )

        for segment in statistic.segments:
            report_relative = (
                _relative(segment.report_path, root) if segment.report_path.exists() else ""
            )
            segment_rows.append(
                {
                    "session_relative_path": session_relative,
                    "segment": segment.segment,
                    "reference_video_relative_path": _relative(segment.video_path, root),
                    "duration_s": ""
                    if segment.duration_s is None
                    else round(segment.duration_s, 6),
                    "duration_hms": ""
                    if segment.duration_s is None
                    else format_duration(segment.duration_s),
                    "inspection_report_relative_path": report_relative,
                    "inspection_status": segment.status,
                    "frame_anomalies": "|".join(segment.frame_anomalies),
                    "detail": segment.detail,
                }
            )
            if segment.report_path.is_file():
                inspection_rows.append(
                    _inspection_row(root, session_relative, segment.segment, segment.report_path)
                )

    sessions_csv = output_dir / "sessions.csv"
    segments_csv = output_dir / "segments.csv"
    inspections_csv = output_dir / "inspections.csv"
    files_csv = output_dir / "files.csv"
    html_path = output_dir / "statistics_report.html"
    _write_csv(sessions_csv, session_rows, SESSION_FIELDS)
    _write_csv(segments_csv, segment_rows, SEGMENT_FIELDS)
    _write_csv(inspections_csv, inspection_rows, INSPECTION_FIELDS)
    _write_csv(files_csv, file_rows, FILE_FIELDS)
    html_path.write_text(
        _render_html(
            root,
            now,
            session_rows,
            segment_rows,
            inspection_rows,
            file_rows,
            dict(sorted(extension_counts.items())),
            dict(sorted(status_counts.items())),
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.iterdir(), key=lambda item: item.name.casefold()):
            archive.write(path, arcname=f"{output_dir.name}/{path.name}")

    return StatisticsReportResult(
        output_dir=output_dir,
        html_path=html_path,
        zip_path=zip_path,
        sessions_csv=sessions_csv,
        segments_csv=segments_csv,
        inspections_csv=inspections_csv,
        files_csv=files_csv,
        session_count=len(session_rows),
        segment_count=len(segment_rows),
        file_count=len(file_rows),
    )
