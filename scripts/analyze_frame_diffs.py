from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import rerun as rr

from robocap_rerun_tools.cli import choose_time_column, normalize_time_values

VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv"}
DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
MOTION_SUFFIXES = {".bvh", ".trc", ".csv", ".tsv", ".xrs"}
BINARY_MOTION_SUFFIXES = {".trb", ".xrb"}


@dataclass
class StreamRecord:
    session: str
    relative_path: str
    kind: str
    stream: str
    frame_count: int | None
    timestamp_basis: str = ""
    timestamp_count: int = 0
    missing_timestamp_count: int = 0
    diff_count: int = 0
    skipped_diff_count: int = 0
    positive_diff_count: int = 0
    zero_diff_count: int = 0
    negative_diff_count: int = 0
    unique_diff_us_count: int = 0
    mode_diff_ms: float | None = None
    mode_diff_count: int = 0
    median_diff_ms: float | None = None
    min_diff_ms: float | None = None
    max_diff_ms: float | None = None
    outside_tolerance_count: int = 0
    large_gap_count: int = 0
    top_diffs: str = ""
    note: str = ""
    histogram: Counter[int] = field(default_factory=Counter, repr=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Count frames and timestamp-diff distributions for every session under a root."
    )
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--extra-root",
        action="append",
        type=Path,
        default=[],
        help="Additional root containing session directories; may be repeated.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--session",
        action="append",
        default=[],
        help="Only inspect this session directory name; may be repeated.",
    )
    return parser.parse_args()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def quantized_histogram(diffs_ms: Iterable[float]) -> Counter[int]:
    return Counter(round(value * 1000.0) for value in diffs_ms if math.isfinite(value))


def normalize_optional_time_values(
    values: list[float | None], column_name: str
) -> list[float | None]:
    finite_values = [value for value in values if value is not None and math.isfinite(value)]
    normalized = iter(normalize_time_values(finite_values, column_name))
    return [
        next(normalized) if value is not None and math.isfinite(value) else None for value in values
    ]


def add_timestamp_stats(record: StreamRecord, times_s: list[float | None]) -> StreamRecord:
    times = [value if value is not None and math.isfinite(value) else None for value in times_s]
    finite_times = [value for value in times if value is not None]
    record.timestamp_count = len(finite_times)
    record.missing_timestamp_count = len(times) - len(finite_times)

    diffs_ms = [
        (second - first) * 1000.0
        for first, second in pairwise(times)
        if first is not None and second is not None
    ]
    record.diff_count = len(diffs_ms)
    record.skipped_diff_count = max(len(times) - 1, 0) - record.diff_count
    record.positive_diff_count = sum(value > 0 for value in diffs_ms)
    record.zero_diff_count = sum(value == 0 for value in diffs_ms)
    record.negative_diff_count = sum(value < 0 for value in diffs_ms)
    record.histogram = quantized_histogram(diffs_ms)
    record.unique_diff_us_count = len(record.histogram)
    if record.histogram:
        mode_us, record.mode_diff_count = record.histogram.most_common(1)[0]
        record.mode_diff_ms = mode_us / 1000.0
        record.top_diffs = "; ".join(
            f"{diff_us / 1000.0:.3f}ms x {count}"
            for diff_us, count in record.histogram.most_common(5)
        )

    positive = [value for value in diffs_ms if value > 0]
    if positive:
        median = statistics.median(positive)
        tolerance = max(2.0, median * 0.25)
        record.median_diff_ms = median
        record.min_diff_ms = min(positive)
        record.max_diff_ms = max(positive)
        record.outside_tolerance_count = sum(abs(value - median) > tolerance for value in diffs_ms)
        record.large_gap_count = sum(value > median * 1.5 for value in diffs_ms)
    return record


def classify_video(path: Path) -> str:
    name = path.name.lower()
    parents = {part.lower() for part in path.parts}
    if name.startswith("robocap_"):
        return "robocap_video"
    if name.startswith("robowrist_"):
        return "robowrist_video"
    if "mocap" in parents or not name.startswith(("robocap_", "robowrist_")):
        return "third_person_video"
    return "video"


def video_record(session_dir: Path, path: Path) -> StreamRecord:
    relative = path.relative_to(session_dir).as_posix()
    kind = classify_video(path)
    record = StreamRecord(session_dir.name, relative, kind, path.stem, None, "MP4 frame PTS")
    try:
        timestamps_ns = list(rr.AssetVideo(path=path).read_frame_timestamps_nanos())
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - codec backend dependent
        record.note = f"video timestamp probe failed: {exc}"
        return record
    record.frame_count = len(timestamps_ns)
    return add_timestamp_stats(record, [value / 1e9 for value in timestamps_ns])


def bvh_record(session_dir: Path, path: Path) -> StreamRecord:
    frames: int | None = None
    frame_time: float | None = None
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            lowered = line.strip().lower()
            if lowered.startswith("frames:"):
                try:
                    frames = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif lowered.startswith("frame time:"):
                frame_time = finite_float(line.split(":", 1)[1].strip())
                break
    record = StreamRecord(
        session_dir.name,
        path.relative_to(session_dir).as_posix(),
        "bvh",
        path.stem,
        frames,
        "BVH Frame Time (synthetic per-frame time)",
    )
    if frames is None or frame_time is None:
        record.note = "missing Frames or Frame Time header"
        return record
    return add_timestamp_stats(record, [index * frame_time for index in range(frames)])


def trc_record(session_dir: Path, path: Path) -> StreamRecord:
    frame_count = 0
    times: list[float | None] = []
    found_header = False
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for row in reader:
            if not found_header:
                if len(row) >= 2 and row[0].strip() == "Frame#" and row[1].strip() == "Time":
                    found_header = True
                continue
            try:
                float(row[0])
            except (IndexError, ValueError):
                continue
            frame_count += 1
            value = finite_float(row[1] if len(row) > 1 else None)
            times.append(value)
    record = StreamRecord(
        session_dir.name,
        path.relative_to(session_dir).as_posix(),
        "trc",
        path.stem,
        frame_count if found_header else None,
        "TRC Time column" if found_header else "",
    )
    if not found_header:
        record.note = "missing Frame#/Time header"
        return record
    return add_timestamp_stats(record, times)


def csv_record(session_dir: Path, path: Path) -> StreamRecord:
    num_frames: int | None = None
    data_rate: float | None = None
    timestamp_index: int | None = None
    frame_rows = 0
    times: list[float | None] = []
    expect_head_values = False

    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
        for row in reader:
            cleaned = [cell.strip() for cell in row]
            lowered = [cell.lower() for cell in cleaned]
            if expect_head_values:
                if cleaned and cleaned[0]:
                    try:
                        num_frames = int(float(cleaned[0]))
                    except ValueError:
                        pass
                    if len(cleaned) > 2:
                        data_rate = finite_float(cleaned[2])
                    expect_head_values = False
                continue
            if cleaned and cleaned[0].lower() == "numframes":
                expect_head_values = True
                continue
            if timestamp_index is None and "timestamp" in lowered:
                timestamp_index = lowered.index("timestamp")
                continue
            if timestamp_index is None:
                continue
            if not cleaned or not cleaned[0]:
                continue
            try:
                float(cleaned[0])
            except ValueError:
                continue
            frame_rows += 1
            value = finite_float(
                cleaned[timestamp_index] if timestamp_index < len(cleaned) else None
            )
            if value is None or value == 0:
                times.append(None)
            else:
                times.append(value)

    frame_count = num_frames if num_frames is not None else (frame_rows or None)
    basis = "CSV Timestamp column" if timestamp_index is not None else ""
    record = StreamRecord(
        session_dir.name,
        path.relative_to(session_dir).as_posix(),
        path.suffix.lower().lstrip("."),
        path.stem,
        frame_count,
        basis,
    )
    notes: list[str] = []
    if timestamp_index is None:
        notes.append("no Timestamp column")
    if num_frames is not None and frame_rows and num_frames != frame_rows:
        notes.append(f"NumFrames={num_frames}, parsed data rows={frame_rows}")
    if data_rate is not None:
        notes.append(f"declared DataFrameRate={data_rate:g} Hz")
    record.note = "; ".join(notes)
    if timestamp_index is None:
        return record
    return add_timestamp_stats(record, normalize_optional_time_values(times, "timestamp"))


def xrs_record(session_dir: Path, path: Path) -> StreamRecord:
    record = StreamRecord(
        session_dir.name,
        path.relative_to(session_dir).as_posix(),
        "xrs",
        path.stem,
        None,
    )
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))
    header_index: int | None = None
    timestamp_index: int | None = None
    for index, row in enumerate(rows):
        lowered = [cell.strip().lower() for cell in row]
        if "timestamp" in lowered:
            header_index = index
            timestamp_index = lowered.index("timestamp")
            break
    if header_index is None or timestamp_index is None:
        record.note = "missing XRS Timestamp header"
        return record

    frame_column = 0
    if header_index > 0:
        previous = [cell.strip().lower() for cell in rows[header_index - 1]]
        if "frame#" in previous:
            frame_column = previous.index("frame#")
    num_frames: int | None = None
    for index, row in enumerate(rows[:header_index]):
        if row and row[0].strip().lower() == "numframes":
            for candidate in rows[index + 1 : header_index + 1]:
                value = finite_float(candidate[0] if candidate else None)
                if value is not None:
                    num_frames = int(value)
                    break
            break
    times: list[float | None] = []
    frame_ids: list[int] = []
    for row in rows[header_index + 1 :]:
        if not row or all(not cell.strip() for cell in row):
            continue
        frame_value = finite_float(row[frame_column] if frame_column < len(row) else None)
        if frame_value is None:
            continue
        frame_ids.append(int(frame_value))
        value = finite_float(row[timestamp_index] if timestamp_index < len(row) else None)
        times.append(value if value not in {None, 0} else None)

    record.frame_count = num_frames if num_frames is not None else (len(frame_ids) or None)
    record.timestamp_basis = "XRS Timestamp column"
    notes: list[str] = []
    if num_frames is not None and frame_ids and num_frames != len(frame_ids):
        notes.append(f"NumFrames={num_frames}, parsed data rows={len(frame_ids)}")
    if not any(value is not None for value in times):
        notes.append("no valid XRS timestamp rows")
    record.note = "; ".join(notes)
    return add_timestamp_stats(record, normalize_optional_time_values(times, "timestamp"))


def binary_motion_record(session_dir: Path, path: Path) -> StreamRecord:
    return StreamRecord(
        session_dir.name,
        path.relative_to(session_dir).as_posix(),
        "binary_motion",
        path.stem,
        None,
        note=(
            f"proprietary NOKOV {path.suffix.upper()} binary; frame count is not decoded; "
            "use the companion text export when present"
        ),
    )


def database_records(session_dir: Path, path: Path) -> list[StreamRecord]:
    records: list[StreamRecord] = []
    try:
        connection = sqlite3.connect(path)
    except sqlite3.Error as exc:
        return [
            StreamRecord(
                session_dir.name,
                path.relative_to(session_dir).as_posix(),
                "database",
                "database",
                None,
                note=f"failed to open SQLite database: {exc}",
            )
        ]
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if not str(row[0]).lower().startswith("sqlite_")
        ]
        for table in tables:
            quoted_table = quote_identifier(table)
            columns_info = list(connection.execute(f"PRAGMA table_info({quoted_table})"))
            columns = [str(row[1]) for row in columns_info]
            time_column = choose_time_column(columns)
            primary_keys = [(int(row[5]), str(row[1])) for row in columns_info if int(row[5] or 0)]
            order_by = (
                ", ".join(quote_identifier(name) for _, name in sorted(primary_keys))
                if primary_keys
                else "rowid"
            )
            frame_count = int(
                connection.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()[0]
            )
            record = StreamRecord(
                session_dir.name,
                path.relative_to(session_dir).as_posix(),
                "sqlite",
                table,
                frame_count,
                f"SQLite {time_column} column" if time_column else "",
            )
            if time_column is None:
                record.note = "no known timestamp column"
                records.append(record)
                continue
            values: list[float | None] = []
            query = (
                f"SELECT {quote_identifier(time_column)} FROM {quoted_table} ORDER BY {order_by}"
            )
            for (raw_value,) in connection.execute(query):
                value = finite_float(raw_value)
                values.append(value)
            records.append(
                add_timestamp_stats(
                    record,
                    normalize_optional_time_values(values, time_column),
                )
            )
    except sqlite3.Error as exc:
        records.append(
            StreamRecord(
                session_dir.name,
                path.relative_to(session_dir).as_posix(),
                "database",
                "database",
                None,
                note=f"SQLite inspection failed: {exc}",
            )
        )
    finally:
        connection.close()
    return records


def image_sequence_records(session_dir: Path) -> list[StreamRecord]:
    groups: dict[Path, list[Path]] = defaultdict(list)
    for path in session_dir.rglob("*.png"):
        if "_artifacts" not in path.relative_to(session_dir).parts:
            groups[path.parent].append(path)
    return [
        StreamRecord(
            session_dir.name,
            directory.relative_to(session_dir).as_posix(),
            "image_sequence",
            directory.name,
            len(paths),
            note="PNG files have no embedded sequence timestamps",
        )
        for directory, paths in sorted(groups.items())
    ]


def inspect_session(session_dir: Path) -> list[StreamRecord]:
    records: list[StreamRecord] = []
    files = sorted(
        path
        for path in session_dir.rglob("*")
        if path.is_file() and "_artifacts" not in path.relative_to(session_dir).parts
    )
    for path in files:
        suffix = path.suffix.lower()
        if suffix in VIDEO_SUFFIXES:
            records.append(video_record(session_dir, path))
        elif suffix == ".bvh":
            records.append(bvh_record(session_dir, path))
        elif suffix == ".trc":
            records.append(trc_record(session_dir, path))
        elif suffix in {".csv", ".tsv"}:
            records.append(csv_record(session_dir, path))
        elif suffix == ".xrs":
            records.append(xrs_record(session_dir, path))
        elif suffix in BINARY_MOTION_SUFFIXES:
            records.append(binary_motion_record(session_dir, path))
        elif suffix in DATABASE_SUFFIXES:
            records.extend(database_records(session_dir, path))
        elif suffix != ".png":
            records.append(
                StreamRecord(
                    session_dir.name,
                    path.relative_to(session_dir).as_posix(),
                    "metadata_or_unsupported",
                    path.stem,
                    None,
                    note=f"no frame parser for {suffix or 'extensionless'} file",
                )
            )
    records.extend(image_sequence_records(session_dir))
    return records


def fmt_number(value: float | None, digits: int = 3) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def tsv_value(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def write_tsv(path: Path, records: list[StreamRecord]) -> None:
    columns = [
        "session",
        "relative_path",
        "kind",
        "stream",
        "frame_count",
        "timestamp_basis",
        "timestamp_count",
        "missing_timestamp_count",
        "diff_count",
        "skipped_diff_count",
        "positive_diff_count",
        "zero_diff_count",
        "negative_diff_count",
        "unique_diff_us_count",
        "mode_diff_ms",
        "mode_diff_count",
        "median_diff_ms",
        "min_diff_ms",
        "max_diff_ms",
        "outside_tolerance_count",
        "large_gap_count",
        "top_diffs",
        "note",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(columns)
        for record in records:
            writer.writerow(
                [
                    tsv_value(record.session),
                    tsv_value(record.relative_path),
                    record.kind,
                    record.stream,
                    tsv_value(record.frame_count),
                    record.timestamp_basis,
                    record.timestamp_count,
                    record.missing_timestamp_count,
                    record.diff_count,
                    record.skipped_diff_count,
                    record.positive_diff_count,
                    record.zero_diff_count,
                    record.negative_diff_count,
                    record.unique_diff_us_count,
                    fmt_number(record.mode_diff_ms, 6),
                    record.mode_diff_count,
                    fmt_number(record.median_diff_ms, 6),
                    fmt_number(record.min_diff_ms, 6),
                    fmt_number(record.max_diff_ms, 6),
                    record.outside_tolerance_count,
                    record.large_gap_count,
                    record.top_diffs,
                    record.note,
                ]
            )


def source_files(sessions: list[Path]) -> list[tuple[Path, Path]]:
    return sorted(
        (
            (session, path)
            for session in sessions
            for path in session.rglob("*")
            if path.is_file() and "_artifacts" not in path.relative_to(session).parts
        ),
        key=lambda item: (item[0].name.lower(), item[1].as_posix().lower()),
    )


def write_source_file_inventory(
    path: Path,
    sessions: list[Path],
    records: list[StreamRecord],
) -> None:
    by_file: dict[tuple[str, str], list[StreamRecord]] = defaultdict(list)
    image_sequences: dict[tuple[str, str], StreamRecord] = {}
    for record in records:
        key = (record.session, record.relative_path)
        if record.kind == "image_sequence":
            image_sequences[key] = record
        else:
            by_file[key].append(record)

    columns = [
        "session",
        "relative_path",
        "extension",
        "bytes",
        "parsed_stream_count",
        "kinds",
        "frame_counts",
        "explicit_timestamp_streams",
        "diff_count",
        "notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(columns)
        for session, source_path in source_files(sessions):
            relative = source_path.relative_to(session).as_posix()
            file_records = by_file.get((session.name, relative), [])
            if source_path.suffix.lower() == ".png":
                parent = source_path.parent.relative_to(session).as_posix()
                sequence = image_sequences.get((session.name, parent))
                frame_counts = (
                    f"1 sequence frame; sequence total={sequence.frame_count}"
                    if sequence is not None
                    else "1 image"
                )
                kinds = "image_sequence_member"
                parsed_stream_count = 1
                explicit_timestamp_streams = 0
                diff_count = 0
                notes = "PNG has no embedded sequence timestamp"
            else:
                frame_counts = "; ".join(
                    f"{record.stream}={record.frame_count if record.frame_count is not None else 'unknown'}"
                    for record in file_records
                )
                kinds = ",".join(sorted({record.kind for record in file_records}))
                parsed_stream_count = len(file_records)
                explicit_timestamp_streams = sum(
                    bool(record.timestamp_basis) and record.kind != "bvh" for record in file_records
                )
                diff_count = sum(record.diff_count for record in file_records)
                notes = "; ".join(
                    dict.fromkeys(record.note for record in file_records if record.note)
                )
            writer.writerow(
                [
                    session.name,
                    relative,
                    source_path.suffix.lower(),
                    source_path.stat().st_size,
                    parsed_stream_count,
                    kinds,
                    frame_counts,
                    explicit_timestamp_streams,
                    diff_count,
                    notes,
                ]
            )


def write_histogram(path: Path, records: list[StreamRecord]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(
            ["session", "relative_path", "stream", "diff_us", "diff_ms", "count", "percent"]
        )
        for record in records:
            if not record.histogram:
                continue
            for diff_us, count in sorted(record.histogram.items()):
                percent = count * 100.0 / record.diff_count if record.diff_count else 0.0
                writer.writerow(
                    [
                        record.session,
                        record.relative_path,
                        record.stream,
                        diff_us,
                        f"{diff_us / 1000.0:.6f}",
                        count,
                        f"{percent:.6f}",
                    ]
                )


def session_relationship_rows(records: list[StreamRecord]) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    by_session: dict[str, list[StreamRecord]] = defaultdict(list)
    for record in records:
        by_session[record.session].append(record)
    for session, session_records in sorted(by_session.items()):
        own = [
            record.frame_count
            for record in session_records
            if record.kind == "robocap_video" and record.frame_count is not None
        ]
        third = [
            record.frame_count
            for record in session_records
            if record.kind == "third_person_video" and record.frame_count is not None
        ]
        mocap = [
            record.frame_count
            for record in session_records
            if record.kind in {"bvh", "trc", "csv", "xrs"} and record.frame_count is not None
        ]
        n = Counter(own).most_common(1)[0][0] if own else None
        third_text = ",".join(str(value) for value in sorted(set(third))) if third else "missing"
        mocap_text = ",".join(str(value) for value in sorted(set(mocap))) if mocap else "missing"
        expected_third = n + 1 if n is not None else None
        expected_mocap = 8 * (n + 1) if n is not None else None
        rows.append(
            (
                session,
                n,
                third_text,
                expected_third,
                mocap_text,
                expected_mocap,
                "yes" if third and len(set(third)) == 1 and third[0] == expected_third else "no",
                "yes" if mocap and len(set(mocap)) == 1 and mocap[0] == expected_mocap else "no",
            )
        )
    return rows


def compact_counts(records: Iterable[StreamRecord]) -> str:
    counter = Counter(record.frame_count for record in records if record.frame_count is not None)
    return (
        ", ".join(
            f"{frame_count} x{count}" if count > 1 else str(frame_count)
            for frame_count, count in sorted(counter.items())
        )
        or "missing"
    )


def session_summary_rows(records: list[StreamRecord]) -> list[dict[str, object]]:
    by_session: dict[str, list[StreamRecord]] = defaultdict(list)
    for record in records:
        by_session[record.session].append(record)
    rows: list[dict[str, object]] = []
    for session, session_records in sorted(by_session.items()):
        explicit_timestamp_streams = [
            record for record in session_records if record.timestamp_basis and record.kind != "bvh"
        ]
        rows.append(
            {
                "session": session,
                "robocap_video_frames": compact_counts(
                    record for record in session_records if record.kind == "robocap_video"
                ),
                "robowrist_video_frames": compact_counts(
                    record for record in session_records if record.kind == "robowrist_video"
                ),
                "third_person_video_frames": compact_counts(
                    record for record in session_records if record.kind == "third_person_video"
                ),
                "bvh_frames": compact_counts(
                    record for record in session_records if record.kind == "bvh"
                ),
                "trc_frames": compact_counts(
                    record for record in session_records if record.kind == "trc"
                ),
                "csv_frames": compact_counts(
                    record for record in session_records if record.kind == "csv"
                ),
                "xrs_frames": compact_counts(
                    record for record in session_records if record.kind == "xrs"
                ),
                "timestamp_streams": len(explicit_timestamp_streams),
                "diff_count": sum(record.diff_count for record in explicit_timestamp_streams),
                "skipped_diff_count": sum(
                    record.skipped_diff_count for record in explicit_timestamp_streams
                ),
                "missing_timestamp_count": sum(
                    record.missing_timestamp_count for record in explicit_timestamp_streams
                ),
                "zero_diff_count": sum(
                    record.zero_diff_count for record in explicit_timestamp_streams
                ),
                "negative_diff_count": sum(
                    record.negative_diff_count for record in explicit_timestamp_streams
                ),
                "outside_tolerance_count": sum(
                    record.outside_tolerance_count for record in explicit_timestamp_streams
                ),
                "large_gap_count": sum(
                    record.large_gap_count for record in explicit_timestamp_streams
                ),
            }
        )
    return rows


def write_session_summary(path: Path, records: list[StreamRecord]) -> None:
    rows = session_summary_rows(records)
    columns = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(
    path: Path,
    roots: list[Path],
    sessions: list[Path],
    records: list[StreamRecord],
    archives: list[Path],
) -> None:
    files = source_files(sessions)
    extensions = Counter(source_path.suffix.lower() or "[none]" for _, source_path in files)
    lines = [
        "# NOKOV all-data frame and timestamp-diff report",
        "",
        f"- roots: {', '.join(f'`{root}`' for root in roots)}",
        f"- sessions: {len({record.session for record in records})}",
        f"- physical source files: {len(files)}",
        f"- streams: {len(records)}",
        f"- streams with explicit timestamp fields: {sum(bool(record.timestamp_basis) and record.kind != 'bvh' for record in records)}",
        f"- streams with at least one valid explicit timestamp: {sum(record.timestamp_count > 0 and record.kind != 'bvh' for record in records)}",
        f"- BVH streams with synthetic per-frame time: {sum(record.kind == 'bvh' for record in records)}",
        f"- total frames/rows across streams: {sum(record.frame_count or 0 for record in records)}",
        f"- total timestamp diffs: {sum(record.diff_count for record in records)}",
        f"- source archives inventoried without double-counting: {len(archives)}",
        "",
        (
            "`diff_count` only includes adjacent source rows where both timestamps are valid. "
            "Intervals touching an invalid/zero timestamp are counted in `skipped_diff_count`; "
            "timestamps on opposite sides of a missing row are never subtracted from each other. "
            "The complete per-stream values are in `all_data_frame_diff_summary.tsv`; every 1-us "
            "diff bin is in `all_data_timestamp_diff_histogram.tsv`. For SQLite streams, "
            "`frame_count` means table rows; SQLite `metadata` tables are listed but are not sensor "
            "frames. BVH diff values are synthesized from the declared `Frame Time`."
        ),
        "",
        "## Physical file inventory",
        "",
        "| extension | files |",
        "|---|---:|",
    ]
    for extension, count in sorted(extensions.items()):
        lines.append(f"| {extension} | {count} |")
    lines.extend(
        [
            "",
            (
                "Every physical file is listed in `all_data_source_file_inventory.tsv`. SQLite "
                "files expand to one entry per table in the per-stream report; PNG files are "
                "counted as members of their containing image sequence."
            ),
            "",
            "## Frame relationships",
            "",
            "| session | own video n | third-person actual | expected n+1 | mocap actual | expected 8*(n+1) | third match | mocap match |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in session_relationship_rows(records):
        lines.append("| " + " | ".join(tsv_value(value) for value in row) + " |")

    lines.extend(
        [
            "",
            "## Per-session frame and explicit timestamp-diff totals",
            "",
            "| session | own video | wrist video | third-person | BVH | TRC | CSV | XRS | timestamp streams | diffs | skipped diffs | missing ts | zero | negative | outside tolerance | large gaps |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in session_summary_rows(records):
        lines.append("| " + " | ".join(tsv_value(value) for value in row.values()) + " |")

    aggregate: dict[str, list[StreamRecord]] = defaultdict(list)
    for record in records:
        aggregate[record.kind].append(record)
    lines.extend(
        [
            "",
            "## Type totals",
            "",
            "| kind | streams | frames/rows | timestamped | diffs | skipped diffs | zero/negative diffs | outside tolerance | large gaps |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for kind, kind_records in sorted(aggregate.items()):
        lines.append(
            "| "
            + " | ".join(
                [
                    kind,
                    str(len(kind_records)),
                    str(sum(record.frame_count or 0 for record in kind_records)),
                    str(sum(record.timestamp_count > 0 for record in kind_records)),
                    str(sum(record.diff_count for record in kind_records)),
                    str(sum(record.skipped_diff_count for record in kind_records)),
                    str(
                        sum(
                            record.zero_diff_count + record.negative_diff_count
                            for record in kind_records
                        )
                    ),
                    str(sum(record.outside_tolerance_count for record in kind_records)),
                    str(sum(record.large_gap_count for record in kind_records)),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Timestamp problems",
            "",
            "| session | file | stream | frames | valid ts | missing ts | diffs | skipped diffs | unique diff-us | zero | negative | outside tolerance | large gaps | top diffs | note |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    problems = [
        record
        for record in records
        if (
            record.missing_timestamp_count
            or record.zero_diff_count
            or record.negative_diff_count
            or record.outside_tolerance_count
            or record.large_gap_count
            or record.note
        )
        and not (record.kind == "sqlite" and not record.timestamp_basis)
        and record.kind != "image_sequence"
    ]
    for record in problems:
        values = [
            record.session,
            f"`{record.relative_path}`",
            record.stream,
            record.frame_count,
            record.timestamp_count,
            record.missing_timestamp_count,
            record.diff_count,
            record.skipped_diff_count,
            record.unique_diff_us_count,
            record.zero_diff_count,
            record.negative_diff_count,
            record.outside_tolerance_count,
            record.large_gap_count,
            record.top_diffs,
            record.note,
        ]
        lines.append(
            "| " + " | ".join(tsv_value(value).replace("|", "\\|") for value in values) + " |"
        )

    lines.extend(["", "## Source archives inventoried without double-counting", ""])
    for archive in archives:
        lines.append(f"- `{archive.name}` ({archive.stat().st_size} bytes)")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")


def main() -> int:
    args = parse_args()
    roots: list[Path] = []
    for candidate in [args.root, *args.extra_root]:
        resolved = candidate.resolve()
        if resolved not in roots:
            roots.append(resolved)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    sessions = sorted(
        {
            path.resolve()
            for root in roots
            for path in root.iterdir()
            if path.is_dir() and "_session" in path.name.lower() and not path.name.startswith("_")
        },
        key=lambda path: (path.name.lower(), str(path).lower()),
    )
    if args.session:
        requested_sessions = set(args.session)
        available_sessions = {path.name for path in sessions}
        missing_sessions = sorted(requested_sessions - available_sessions)
        if missing_sessions:
            raise RuntimeError(
                f"requested session directories not found: {', '.join(missing_sessions)}"
            )
        sessions = [path for path in sessions if path.name in requested_sessions]
    records = [record for session in sessions for record in inspect_session(session)]
    records.sort(key=lambda record: (record.session, record.relative_path, record.stream))
    archives = sorted(
        {
            path.resolve()
            for root in roots
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in {".zip", ".rar", ".7z"}
        }
    )
    for record in records:
        adjacent_interval_count = max(
            record.timestamp_count + record.missing_timestamp_count - 1,
            0,
        )
        if record.diff_count + record.skipped_diff_count != adjacent_interval_count:
            raise RuntimeError(
                f"diff invariant failed for {record.session}/{record.relative_path}/{record.stream}: "
                f"{record.diff_count} + {record.skipped_diff_count} != "
                f"{adjacent_interval_count}"
            )
        if sum(record.histogram.values()) != record.diff_count:
            raise RuntimeError(
                f"histogram invariant failed for "
                f"{record.session}/{record.relative_path}/{record.stream}"
            )
    write_tsv(output / "all_data_frame_diff_summary.tsv", records)
    write_source_file_inventory(output / "all_data_source_file_inventory.tsv", sessions, records)
    write_session_summary(output / "all_data_session_summary.tsv", records)
    write_histogram(output / "all_data_timestamp_diff_histogram.tsv", records)
    write_markdown(output / "all_data_frame_diff_report.md", roots, sessions, records, archives)
    print(f"sessions={len(sessions)} streams={len(records)}")
    print(f"timestamped_streams={sum(record.timestamp_count > 0 for record in records)}")
    print(f"total_diffs={sum(record.diff_count for record in records)}")
    print(f"output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
