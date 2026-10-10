"""Read-only content checks; file existence and parseable headers are not sufficient."""

from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
import subprocess
from contextlib import closing, contextmanager
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv"}
MOTION_SUFFIXES = {".trc", ".bvh", ".csv", ".tsv", ".xrs", ".c3d"}
DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SENSOR_COLUMNS = {
    "acc_data": ("timestamp", "x", "y", "z"),
    "gyro_data": ("timestamp", "x", "y", "z"),
    "mag_data": ("timestamp", "mag_x", "mag_y", "mag_z"),
}
STATUS_LABELS = {
    "ok": "通过",
    "missing": "缺失",
    "zero_byte": "零字节文件",
    "lfs_pointer": "只有 LFS 占位",
    "empty_data": "没有实际数据",
    "schema_error": "缺表或缺字段",
    "invalid_format": "格式无效或内容损坏",
    "invalid_data": "存在无效数据",
    "count_mismatch": "表头与实际数据行数不符",
    "column_mismatch": "表头与实际数据列数不符",
    "partial_data": "部分轨迹点缺失",
    "read_error": "无法读取",
    "active_database": "数据库尚有未合并日志",
    "changed": "检查期间文件发生变化",
    "tool_unavailable": "检查工具不可用",
    "not_checked": "格式尚未验证",
    "ambiguous": "存在多个候选",
    "manifest_mismatch": "与清单不一致",
    "requirements_unknown": "动作刚体清单待确认",
}
WARNING_STATES = {
    "partial_data", "not_checked", "tool_unavailable", "active_database", "changed",
    "requirements_unknown",
}


def result(state="ok", detail="", **metrics):
    return {"state": state, "status": STATUS_LABELS[state], "detail": detail, **metrics}


def file_signature(path: Path):
    value = path.stat()
    return value.st_size, value.st_mtime_ns


def pending_database_log(path: Path) -> str:
    for suffix in ("-wal", "-journal"):
        candidate = path.with_name(path.name + suffix)
        try:
            if candidate.stat().st_size:
                return candidate.name
        except FileNotFoundError:
            pass
    return ""


def open_readonly_connection(path: Path):
    if pending := pending_database_log(path):
        raise sqlite3.OperationalError(
            f"Unmerged database log; stop recording and recheck: {pending}"
        )
    uri = path.resolve().as_uri()
    if uri.startswith("file://") and not uri.startswith("file:///"):
        uri = "file:////" + uri[len("file://") :]
    # immutable prevents creating a journal/SHM in the source directory. WAL was
    # checked above because immutable deliberately ignores it.
    return sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)


@contextmanager
def readonly_database(path: Path):
    with closing(open_readonly_connection(path)) as connection:
        yield connection


def _finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (ValueError, TypeError, OverflowError):
        return False


def inspect_sensor_database(path: Path):
    tables_report, issues = [], []
    with readonly_database(path) as connection:
        integrity = connection.execute("PRAGMA quick_check").fetchall()
        if integrity != [("ok",)]:
            return result("invalid_format", "; ".join(str(r[0]) for r in integrity))
        available = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        lowered = path.stem.lower()
        expected = (
            ("acc_data", "gyro_data")
            if "_imu_" in lowered
            else (
                ("mag_data",)
                if "_mag_" in lowered
                else tuple(t for t in SENSOR_COLUMNS if t in available)
            )
        )
        if not expected:
            return result("schema_error", "No ACC/GYRO/MAG data tables")
        for table in expected:
            if table not in available:
                issues.append(f"missing table: {table}")
                tables_report.append({"table": table, "state": "schema_error"})
                continue
            columns = {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
            absent = set(SENSOR_COLUMNS[table]) - columns
            if absent:
                issues.append(f"{table}: missing columns {', '.join(sorted(absent))}")
                tables_report.append({"table": table, "state": "schema_error"})
                continue
            count, invalid = 0, 0
            names = ",".join(f'"{name}"' for name in SENSOR_COLUMNS[table])
            for row in connection.execute(f'SELECT {names} FROM "{table}"'):
                count += 1
                invalid += not all(_finite(value) for value in row)
            state = "empty_data" if not count else "invalid_data" if invalid else "ok"
            tables_report.append(
                {"table": table, "state": state, "rows": count, "invalid_rows": invalid}
            )
            issues.append(f"{table}: {count} rows, {invalid} invalid")
    states = {r["state"] for r in tables_report}
    state = next((s for s in ("schema_error", "empty_data", "invalid_data") if s in states), "ok")
    return result(
        state,
        "; ".join(issues),
        tables=tables_report,
        rows=sum(r.get("rows", 0) for r in tables_report),
    )


def inspect_video(path: Path, ffmpeg: str | None, ffprobe: str | None, log_path: Path | None):
    if not ffmpeg or not ffprobe:
        return result("tool_unavailable", "FFmpeg/FFprobe is unavailable; video not decoded")
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    if probe.returncode:
        return result("invalid_format", probe.stderr.decode("utf-8", errors="replace")[-4000:])
    streams = json.loads(probe.stdout).get("streams", [])
    if not streams or not streams[0].get("width") or not streams[0].get("height"):
        return result("empty_data", "No video stream with valid dimensions")
    process = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            # Only the throwaway decode sink gets sequential timestamps. Avoid
            # output-muxer DTS warnings and automatic duplicate/drop of VFR frames;
            # the original timestamps are still inspected separately, unchanged.
            "-vf",
            "setpts=N/(30*TB)",
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            "1:30",
            "-progress",
            "pipe:1",
            "-nostats",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    progress = process.stdout.decode("utf-8", errors="replace")
    diagnostic = process.stderr.decode("utf-8", errors="replace")
    if log_path is not None:
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"\nVIDEO: {path.name}\n{progress}\n{diagnostic}\n")
    frames = re.findall(r"(?m)^frame=\s*(\d+)\s*$", progress)
    count = int(frames[-1]) if frames else 0
    if process.returncode or diagnostic.strip():
        return result(
            "invalid_format", "Full video decode failed: " + diagnostic[-4000:], frames=count
        )
    if not count:
        return result("empty_data", "No frames were decoded", frames=0)
    return result(frames=count, detail=f"Full decode: {count} frames")


def _motion_result(rows, declared, valid, missing=0):
    detail = f"actual rows={rows}; declared rows={declared}; valid samples={valid}"
    if not rows or not valid:
        return result("empty_data", detail, rows=rows, declared_rows=declared)
    if declared is not None and rows != declared:
        return result("count_mismatch", detail, rows=rows, declared_rows=declared)
    if missing:
        return result(
            "partial_data",
            detail + f"; missing samples={missing}",
            rows=rows,
            declared_rows=declared,
            missing_samples=missing,
        )
    return result(detail=detail, rows=rows, declared_rows=declared)


def inspect_trc(stream):
    declared = None
    metadata = None
    width = offset = 0
    rows = valid = missing = 0
    mismatched_widths = set()
    for number, line in enumerate(stream, 1):
        if not line.strip():
            continue
        cells = [c.strip() for c in line.rstrip("\r\n").split("\t")]
        if not width:
            words = line.split()
            if "NumFrames" in words:
                metadata = words
            elif metadata:
                declared = int(words[metadata.index("NumFrames")])
                metadata = None
            if cells[0] == "Frame#":
                if len(cells) < 3:
                    raise ValueError("TRC has no marker columns")
                offset = 3 if "time" in cells[2].lower() or "stamp" in cells[2].lower() else 2
                markers = sum(bool(value) for value in cells[offset:])
                if not markers:
                    raise ValueError("TRC has no marker names")
                width = offset + 3 * markers
            continue
        if not cells[0].isdigit():
            if not rows and not cells[0] and any(v.startswith("X") for v in cells[offset:]):
                continue  # coordinate-label row immediately after Frame#/Time
            raise ValueError(f"TRC line {number}: invalid frame row")
        while (
            len(cells) > offset
            and not cells[-1]
            and (len(cells) > width or (len(cells) - offset) % 3)
        ):
            cells.pop()
        if len(cells) < offset or len(cells) > width or (len(cells) - offset) % 3:
            raise ValueError(f"TRC line {number}: expected {width} columns, found {len(cells)}")
        if offset < len(cells) < width:
            mismatched_widths.add(len(cells))
        # NOKOV writes only Frame#/Time/Timestamp for completely untracked rows.
        # They are missing marker samples, not a truncated/corrupt text record.
        cells.extend([""] * (width - len(cells)))
        if not _finite(cells[1]):
            raise ValueError(f"TRC line {number}: invalid Time")
        for index in range(offset, width, 3):
            xyz = cells[index : index + 3]
            # NaN is a missing marker, but arbitrary text is malformed.
            if any(
                value and value.lower() not in {"nan", "-nan"} and not _finite(value)
                for value in xyz
            ):
                raise ValueError(f"TRC line {number}: nonnumeric coordinate")
            if all(value and _finite(value) for value in xyz):
                valid += 1
            else:
                missing += 1
        rows += 1
    if not width:
        raise ValueError("TRC missing Frame#/Time header")
    checked = _motion_result(rows, declared, valid, missing)
    if mismatched_widths:
        return result(
            "column_mismatch",
            checked["detail"] + f"; header columns={width}; "
            f"observed columns={sorted(mismatched_widths)}",
            rows=rows,
            declared_rows=declared,
            missing_samples=missing,
        )
    return checked


def inspect_bvh(stream):
    channels, depth, roots = 0, 0, 0
    motion = False
    declared = None
    frame_time = None
    rows = 0
    for number, line in enumerate(stream, 1):
        words = line.split()
        if not words:
            continue
        if not motion:
            if words[0] == "MOTION":
                if not channels or roots != 1 or depth:
                    raise ValueError("BVH has invalid hierarchy/channel declarations")
                motion = True
                continue
            roots += words[0] == "ROOT"
            depth += words.count("{") - words.count("}")
            if depth < 0:
                raise ValueError("BVH has unbalanced hierarchy braces")
            if words[0] == "CHANNELS":
                count = int(words[1])
                if count <= 0 or count != len(words) - 2:
                    raise ValueError("BVH invalid CHANNELS declaration")
                channels += count
            continue
        if declared is None:
            if not line.strip().startswith("Frames:"):
                raise ValueError("BVH missing Frames header")
            declared = int(line.split(":", 1)[1])
        elif frame_time is None:
            if not line.strip().startswith("Frame Time:"):
                raise ValueError("BVH missing Frame Time header")
            frame_time = float(line.split(":", 1)[1])
            if not math.isfinite(frame_time) or frame_time <= 0:
                raise ValueError("BVH invalid Frame Time")
        else:
            if len(words) != channels or not all(_finite(value) for value in words):
                raise ValueError(f"BVH line {number}: expected {channels} finite channel values")
            rows += 1
    if not motion or frame_time is None:
        raise ValueError("BVH missing MOTION or frame headers")
    return _motion_result(rows, declared, rows)


def _pose_cells(line, suffix):
    if suffix == ".csv":
        return [v.strip() for v in next(csv.reader([line]))]
    return [v.strip() for v in line.rstrip("\r\n").split("\t")] if "\t" in line else line.split()


def inspect_pose(stream, suffix):
    declared, metadata, header, xyz_groups = None, None, None, []
    rows = valid = missing = 0
    for number, line in enumerate(stream, 1):
        if not line.strip():
            continue
        cells = _pose_cells(line, suffix)
        if header is None:
            if "NumFrames" in cells:
                metadata = cells
                continue
            if metadata:
                declared = int(cells[metadata.index("NumFrames")])
                metadata = None
                continue
            if "Timestamp" in cells and any(
                c.startswith(("XToGlobal", "XToParent")) for c in cells
            ):
                if cells[0] == "Timestamp":
                    cells.insert(0, "Frame#")
                header = cells
                for index, name in enumerate(header):
                    if name.startswith(("XToGlobal", "XToParent")):
                        try:
                            xyz_groups.append(
                                (index, header.index("Y" + name[1:]), header.index("Z" + name[1:]))
                            )
                        except ValueError as exc:
                            raise ValueError("Pose file has incomplete XYZ columns") from exc
            continue
        while len(cells) > len(header) and not cells[-1]:
            cells.pop()
        while len(header) > len(cells) and not header[-1]:
            header.pop()
        if len(cells) != len(header) or not cells[0].isdigit():
            raise ValueError(f"Pose line {number}: invalid row width or frame number")
        for group in xyz_groups:
            xyz = [cells[index] for index in group]
            if any(v and not _finite(v) and v.lower() not in {"nan", "-nan"} for v in xyz):
                raise ValueError(f"Pose line {number}: nonnumeric coordinate")
            if all(v and _finite(v) for v in xyz):
                valid += 1
            else:
                missing += 1
        rows += 1
    if header is None:
        raise ValueError("Pose file missing Timestamp/XYZ data header")
    return _motion_result(rows, declared, valid, missing)


def inspect_motion(path):
    if path.suffix.lower() == ".c3d":
        import ezc3d
        import numpy as np

        data = ezc3d.c3d(str(path))
        points = data["data"]["points"]
        rows = int(points.shape[2])
        # The fourth channel is residual; negative means that point was not tracked.
        valid = np.isfinite(points[:3]).all(axis=0) & (points[3] >= 0)
        return _motion_result(rows, None, int(valid.sum()), int((~valid).sum()))
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            with path.open(encoding=encoding, errors="strict", newline="") as stream:
                if path.suffix.lower() == ".trc":
                    checked = inspect_trc(stream)
                elif path.suffix.lower() == ".bvh":
                    checked = inspect_bvh(stream)
                else:
                    checked = inspect_pose(stream, path.suffix.lower())
            return {**checked, "encoding": encoding}
        except UnicodeDecodeError:
            if encoding == "gb18030":
                raise
    raise ValueError("Unsupported text encoding")


def inspect_file(path: Path, *, ffmpeg=None, ffprobe=None, video_log=None):
    before = None
    try:
        before = file_signature(path)
        with path.open("rb") as stream:
            prefix = stream.read(128)
        if not prefix:
            checked = result("zero_byte")
        elif prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
            checked = result("lfs_pointer")
        elif path.suffix.lower() in DATABASE_SUFFIXES:
            if pending := pending_database_log(path):
                checked = result("active_database", pending)
            else:
                checked = inspect_sensor_database(path)
        elif path.suffix.lower() in VIDEO_SUFFIXES:
            checked = inspect_video(path, ffmpeg, ffprobe, video_log)
        elif path.suffix.lower() in MOTION_SUFFIXES:
            checked = inspect_motion(path)
        elif path.suffix.lower() == ".json":
            with path.open(encoding="utf-8-sig") as stream:
                value = json.load(stream)
            checked = result() if value else result("empty_data", "Empty JSON value")
        else:
            with path.open("rb") as stream:
                while stream.read(1024 * 1024):
                    pass
            checked = result("not_checked", "Bytes readable; this format has no content validator")
        if before != file_signature(path):
            checked = result("changed")
        elif path.suffix.lower() in DATABASE_SUFFIXES and (pending := pending_database_log(path)):
            checked = result("active_database", pending)
    except FileNotFoundError as exc:
        tool_missing = (
            before is not None and path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
        )
        checked = result("tool_unavailable" if tool_missing else "read_error", str(exc))
    except OSError as exc:
        checked = result("read_error", str(exc))
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorname", "")
        unreadable = code.startswith(
            ("SQLITE_IOERR", "SQLITE_CANTOPEN", "SQLITE_BUSY", "SQLITE_LOCKED")
        )
        checked = result("read_error" if unreadable else "invalid_format", str(exc))
    except Exception as exc:  # noqa: BLE001 - one broken file must not hide the rest of the Session
        checked = result("invalid_format", f"{type(exc).__name__}: {exc}")
    return {**checked, "size_bytes": before[0] if before else None}
