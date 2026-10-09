"""Inspect local right-wrist IMU databases without modifying or uploading recordings."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import webbrowser
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from .file_integrity import readonly_database
from .inventory_report import render_table, write_csv
from .mocap_metadata import parse_mocap_capture_directory

SESSION_NAME = re.compile(r"^\d{8}_\d{6}_session\d+$", re.IGNORECASE)
WRIST_DIR = re.compile(r"^robowrist_(?P<device>.+)_right$", re.IGNORECASE)
SEGMENT_FILE = re.compile(r"^(?:robocap|robowrist)_segment(\d+)_.+", re.IGNORECASE)
WRIST_FILE = re.compile(r"^robowrist_segment(\d+)_.+_right(?:_down)?\.[^.]+$", re.IGNORECASE)
IMU_FILE = re.compile(r"^robowrist_segment(\d+)_imu_right\.db$", re.IGNORECASE)
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "_artifacts",
    "_analysis",
    "_reports",
    "_modelscope_dataset",
    "raw_calibration",
    "_raw_calibration_cache",
    "$recycle.bin",
    "system volume information",
    "windows",
    "node_modules",
}
STATES = {
    "has_data": "ACC、GYRO 均有记录",
    "empty": "ACC、GYRO 均为空",
    "partial": "只有一路有记录",
    "missing_file": "缺少右腕 IMU 文件",
    "zero_byte": "零字节文件",
    "schema_error": "缺少数据表或必要字段",
    "sqlite_error": "数据库损坏或格式无效",
    "lfs_pointer": "仅有 Git LFS 占位文件",
    "active_database": "有未合并日志，停止录制后重查",
    "changed": "检查时文件发生变化",
    "read_error": "无法读取",
    "no_segment": "未识别录制片段",
}
INCOMPLETE_STATES = {"read_error", "active_database", "changed"}
FILE_FIELDS = [
    "session_id",
    "date",
    "participant",
    "action_id",
    "source_directory",
    "device_id",
    "device_source",
    "segment",
    "state",
    "status",
    "acc_rows",
    "gyro_rows",
    "acc_span_s",
    "gyro_span_s",
    "size_bytes",
    "detail",
    "file_path",
    "session_path",
    "root",
]
COUNT_FIELDS = ["checks", "session_directories", *STATES, "participants", "dates"]


def _linked(path: Path) -> bool:
    # Python 3.11 has no Path.is_junction. Reject Windows reparse points as well as symlinks.
    return path.is_symlink() or bool(
        getattr(path.lstat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _signature(path: Path):
    value = path.stat()
    return value.st_size, value.st_mtime_ns


def _pending_log(path: Path) -> str:
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        try:
            if sidecar.stat().st_size:
                return sidecar.name
        except FileNotFoundError:
            pass
    return ""


def _database_contents(path: Path) -> dict:
    with readonly_database(path) as connection:
        check = connection.execute("PRAGMA quick_check").fetchall()
        if check != [("ok",)]:
            return {"state": "sqlite_error", "detail": "; ".join(str(r[0]) for r in check)}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        result, issues = {}, []
        for label, table in (("acc", "acc_data"), ("gyro", "gyro_data")):
            if table not in tables:
                issues.append(f"missing table: {table}")
                continue
            columns = {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
            absent = {"timestamp", "x", "y", "z"} - columns
            if absent:
                issues.append(f"{table}: missing columns {', '.join(sorted(absent))}")
                continue
            count, first, last = connection.execute(
                f'SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM "{table}"'
            ).fetchone()
            result[label + "_rows"] = count
            if isinstance(first, (int, float)) and isinstance(last, (int, float)):
                result[label + "_span_s"] = (last - first) / 1e9
            elif count:
                result.setdefault("detail", "")
                result["detail"] += f"{table}: timestamp range is not numeric; "
        if issues:
            result.update(state="schema_error", detail="; ".join(issues))
        else:
            nonempty = sum(result[label + "_rows"] > 0 for label in ("acc", "gyro"))
            result["state"] = ("empty", "partial", "has_data")[nonempty]
        return result


def inspect_database(path: Path) -> dict:
    result = {
        "state": "read_error",
        "acc_rows": None,
        "gyro_rows": None,
        "acc_span_s": None,
        "gyro_span_s": None,
        "size_bytes": None,
        "detail": "",
    }
    try:
        before = _signature(path)
        result["size_bytes"] = before[0]
        if pending := _pending_log(path):
            result.update(state="active_database", detail=pending)
        else:
            with path.open("rb") as stream:
                prefix = stream.read(128)
            if not prefix:
                result["state"] = "zero_byte"
            elif prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
                result["state"] = "lfs_pointer"
            else:
                try:
                    result.update(_database_contents(path))
                except sqlite3.Error as exc:
                    code = getattr(exc, "sqlite_errorname", "")
                    unreadable = code.startswith(("SQLITE_IOERR", "SQLITE_CANTOPEN", "SQLITE_BUSY"))
                    result.update(
                        state="read_error" if unreadable else "sqlite_error", detail=str(exc)
                    )
        pending = _pending_log(path)
        if before != _signature(path):
            result.update(state="changed", detail="File size or modification time changed")
        elif pending:
            result.update(state="active_database", detail=pending)
    except OSError as exc:
        result.update(state="read_error", detail=str(exc))
    if result["state"] in INCOMPLETE_STATES:
        for key in ("acc_rows", "gyro_rows", "acc_span_s", "gyro_span_s"):
            result[key] = None
    result["status"] = STATES[result["state"]]
    return result


def _date(session_name: str) -> str:
    if SESSION_NAME.fullmatch(session_name):
        try:
            instant = datetime.strptime(session_name[:15], "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
            return instant.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return "unknown"


def _metadata(session: Path, dirs: list[str], files: list[str], error) -> tuple[dict, str]:
    capture, device = {}, ""
    manifest = next((session / name for name in files if name.lower() == "manifest.json"), None)
    if manifest is not None:
        try:
            if manifest.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("manifest exceeds 2 MiB; identity not read")
            value = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise TypeError("manifest must be a JSON object")
            record, ids = value.get("mocap_capture"), value.get("device_ids")
            capture = record if isinstance(record, dict) else {}
            right = ids.get("right") if isinstance(ids, dict) else None
            device = right if isinstance(right, str) and re.fullmatch(r"[\w-]+", right) else ""
        except (OSError, ValueError, TypeError) as exc:
            error(manifest, str(exc))
    parsed = []
    for name in dirs:
        try:
            if item := parse_mocap_capture_directory(name):
                parsed.append(item)
        except ValueError as exc:
            error(session / name, str(exc))
    if len(parsed) == 1:
        local = parsed[0].as_record()
        if capture and any(capture.get(k) != local[k] for k in ("action_id", "participant")):
            error(session, "manifest and local Mocap identity disagree; using local directory")
        capture = local
    elif len(parsed) > 1:
        error(session, "multiple Mocap identities; participant/action left unknown")
        capture = {}
    return {
        key: capture.get(key) if isinstance(capture.get(key), str) else ""
        for key in ("participant", "action_id", "source_directory")
    }, device


def inspect_session(session: Path, root: Path, dirs: list[str], files: list[str], error, log):
    metadata, declared_device = _metadata(session, dirs, files, error)
    base = {
        "root": str(root),
        "session_path": str(session),
        "session_id": session.name,
        "date": _date(session.name),
        **metadata,
    }
    segments = {int(m[1]) for name in files if (m := SEGMENT_FILE.fullmatch(name))}
    # Each container supplies evidence only for its own segments. Do not invent a
    # device x segment cross-product when devices were changed during a Session.
    containers = [(session, "unknown", "unknown", files)]
    unreadable = []
    for name in dirs:
        if match := WRIST_DIR.fullmatch(name):
            directory = session / name
            try:
                names = [p.name for p in directory.iterdir() if p.is_file() and not _linked(p)]
                containers.append((directory, match["device"], "directory", names))
            except OSError as exc:
                error(directory, str(exc))
                unreadable.append((directory, match["device"], str(exc)))
    evidence = defaultdict(list)
    for directory, device, source, names in containers:
        local_segments = {int(m[1]) for name in names if (m := WRIST_FILE.fullmatch(name))}
        segments.update(local_segments)
        for segment in local_segments:
            databases = [
                directory / name
                for name in names
                if (match := IMU_FILE.fullmatch(name)) and int(match[1]) == segment
            ]
            evidence[segment].append((directory, device, source, databases))
    rows = []

    def append(segment, device, source, path, state=None, detail=""):
        if state is None:
            log(f"CHECK {device} segment{segment}: {path}")
            result = inspect_database(path)
            if result["state"] in INCOMPLETE_STATES:
                error(path, result["detail"])
        else:
            result = {"state": state, "status": STATES[state], "detail": detail}
        row = {
            **base,
            "device_id": device,
            "device_source": source,
            "segment": segment,
            "file_path": str(path) if path else "",
            **result,
        }
        rows.append(row)
        log(f"  {session.name} / {device} / segment{segment}: {row['status']}")

    for directory, device, detail in unreadable:
        append(None, device, "directory", directory, "read_error", detail)
    for segment in sorted(segments):
        candidates = evidence[segment]
        if not candidates:
            # No per-segment evidence: a unique wrist directory or an explicit
            # manifest may attribute a missing file; otherwise keep it unknown.
            wrist_dirs = [c for c in containers if c[2] == "directory"]
            if len(wrist_dirs) == 1:
                directory, device, _, _ = wrist_dirs[0]
                source = "unique_session_directory"
            elif declared_device:
                directory = session / f"robowrist_{declared_device}_right"
                device, source = declared_device, "manifest"
            else:
                directory, device, source = session, "unknown", "unknown"
            if unreadable:
                # Inaccessible directories are already reported, not counted as missing.
                continue
            candidates = [(directory, device, source, [])]
        for directory, device, source, databases in candidates:
            if databases:
                for path in databases:
                    append(segment, device, source, path)
            else:
                append(
                    segment,
                    device,
                    source,
                    directory / f"robowrist_segment{segment}_imu_right.db",
                    "missing_file",
                )
    if not segments and not unreadable:
        append(
            None,
            declared_device or "unknown",
            "manifest" if declared_device else "unknown",
            None,
            "no_segment",
        )
    return rows


def scan(roots: list[Path], output: Path, *, log=print):
    rows, errors, seen, sessions = [], [], set(), set()

    def error(path, message):
        errors.append({"path": str(path), "error": message})
        log(f"SCAN ERROR: {path}: {message}")

    for specified in roots:
        root = specified.expanduser().resolve()
        if not root.is_dir():
            error(root, "root_unavailable")
            continue
        if root.is_relative_to(output):
            error(root, "scan_root_is_inside_output")
            continue
        log(f"SCAN ROOT: {root}")
        for directory, dirs, files in os.walk(
            root, followlinks=False, onerror=lambda exc: error(exc.filename, str(exc))
        ):
            current = Path(directory)
            identity = os.path.normcase(str(current.resolve()))
            if identity in seen:
                dirs[:] = []
                continue
            seen.add(identity)
            try:
                dirs[:] = sorted(
                    name
                    for name in dirs
                    if name.lower() not in SKIP_DIRS
                    and not _linked(current / name)
                    and not (current / name).resolve().is_relative_to(output)
                )
                files = sorted(name for name in files if not _linked(current / name))
            except OSError as exc:
                error(current, str(exc))
                dirs[:] = []
                continue
            inside_session = any(os.path.normcase(str(p)) in sessions for p in current.parents)
            is_session = not WRIST_DIR.fullmatch(current.name) and (
                SESSION_NAME.fullmatch(current.name)
                or any(SEGMENT_FILE.fullmatch(name) for name in files)
                or any(WRIST_DIR.fullmatch(name) for name in dirs)
            )
            if is_session and not inside_session:
                sessions.add(identity)
                rows.extend(inspect_session(current, root, dirs, files, error, log))
            if len(seen) % 500 == 0:
                log(f"Scanned {len(seen)} directories; inspected {len(rows)} checks")
    return rows, errors


def summarize(rows: list[dict], key: str) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get(key) or "unknown"].append(row)
    results = []
    for value, group in sorted(grouped.items()):
        counts = Counter(row["state"] for row in group)
        results.append(
            {
                key: value,
                "checks": len(group),
                "session_directories": len({row["session_path"] for row in group}),
                **{state: counts[state] for state in STATES},
                "participants": sorted({row["participant"] or "unknown" for row in group}),
                "dates": sorted({row["date"] for row in group}),
            }
        )
    return results


def write_reports(output: Path, roots: list[Path], rows: list[dict], errors: list[dict]):
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "roots": [str(path.resolve()) for path in roots],
        "scan_complete": not errors,
        "checks": len(rows),
        "session_directories": len({r["session_path"] for r in rows}),
        "states": dict(Counter(r["state"] for r in rows)),
        "scan_errors": len(errors),
    }
    write_csv(output / "files.csv", rows, FILE_FIELDS)
    write_csv(output / "scan_errors.csv", errors, ["path", "error"])
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>右手 IMU 检查统计</title><style>
body{font:15px system-ui,sans-serif;margin:28px;color:#183047;background:#f5f8fa}
h1,h2{color:#143e54}.scroll{overflow:auto;margin:16px 0 28px;background:white}
table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{padding:9px 12px;
border:1px solid #d8e2e8;text-align:left}th{background:#e5eff4}input{padding:8px;width:320px}
a{color:#00648a}.note{line-height:1.8}td:last-child{white-space:normal;min-width:140px}
</style><h1>右手 IMU 检查统计</h1>"""
    document += f"<p>检查项：{len(rows)}；录制目录：{summary['session_directories']}；"
    document += f"扫描错误：{len(errors)}。</p>"
    if errors:
        document += "<p><strong>扫描未完整完成，请先查看底部扫描错误，处理后重跑。</strong></p>"
    if not rows:
        document += "<p><strong>没有找到录制目录，请确认选对了原始数据目录。</strong></p>"
    document += """<p class="note">只检查右腕 Robowrist，不包含头戴 Robocap 的右侧 IMU。
每个录制目录、Segment、设备分别计一个检查项；不同位置的副本分别计数，重叠路径不重复。
日期按 Session 名的 UTC 时间换算为北京时间。未知信息保留为空或 unknown，不猜测设备或人员。<br>
“均有记录”只说明两张数据表非空，不代表数值、采样率、同步质量合格。
空库不能靠重新上传同一个文件恢复数据。本工具不修补、不上传、不修改源数据。</p>
<p><a href="files.csv">文件明细</a> · <a href="devices.csv">设备汇总</a> ·
<a href="dates.csv">日期汇总</a> · <a href="participants.csv">参与者汇总</a> ·
<a href="run.log">运行日志</a></p>
<label>搜索 <input id="query" placeholder="设备、日期、参与者、动作或 Session"></label>"""
    for filename, key, label in (
        ("devices.csv", "device_id", "设备"),
        ("dates.csv", "date", "日期"),
        ("participants.csv", "participant", "参与者"),
    ):
        groups = summarize(rows, key)
        # The grouping key must appear only once in a CSV header.
        fields = list(dict.fromkeys([key, *COUNT_FIELDS]))
        write_csv(output / filename, groups, fields)
        active_states = {state: STATES[state] for state in STATES if any(g[state] for g in groups)}
        document += f"<h2>按{label}汇总</h2>" + render_table(
            groups,
            {
                key: label,
                "checks": "检查项",
                "session_directories": "录制目录",
                **active_states,
            },
        )
    document += "<h2>所有检查项</h2>" + render_table(
        rows,
        {
            "date": "日期",
            "participant": "参与者",
            "action_id": "动作",
            "session_id": "Session",
            "device_id": "设备",
            "device_source": "设备归属依据",
            "segment": "Segment",
            "status": "检查结果",
            "acc_rows": "ACC 行数",
            "gyro_rows": "GYRO 行数",
            "acc_span_s": "ACC 跨度/秒",
            "gyro_span_s": "GYRO 跨度/秒",
            "file_path": "路径",
            "detail": "详情",
        },
    )
    if errors:
        document += "<h2>扫描错误</h2>" + render_table(errors, {"path": "路径", "error": "错误"})
    document += """<script>
document.getElementById('query').addEventListener('input',e=>{
 const q=e.target.value.toLocaleLowerCase();
 document.querySelectorAll('tbody tr').forEach(r=>{
  r.hidden=!r.textContent.toLocaleLowerCase().includes(q);
 });
});</script></html>"""
    (output / "report.html").write_text(document, encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        action="append",
        required=True,
        help="Local, mapped or UNC root; repeat for multiple roots",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_artifacts/imu_repair_upload")
        / datetime.now(UTC).astimezone().strftime("%Y%m%d_%H%M%S_%f"),
        help="New report directory; existing directories are never overwritten",
    )
    parser.add_argument("--open-report", action="store_true", help="Open HTML when finished")
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    if any(root.expanduser().resolve().is_relative_to(output) for root in args.root):
        parser.error("Output must not contain a source root")
    if output.exists():
        parser.error("Output already exists; choose a new directory")
    output.mkdir(parents=True)
    with (output / "run.log").open("w", encoding="utf-8") as logfile:

        def log(message):
            print(message, flush=True)
            logfile.write(str(message) + "\n")
            logfile.flush()

        log("Right-wrist IMU inventory: source files stay unchanged")
        log("Reports: " + str(output))
        rows, errors = scan(args.root, output, log=log)
        summary = write_reports(output, args.root, rows, errors)
        log(json.dumps(summary, ensure_ascii=False))
        log("FINISHED: " + str(output / "report.html"))
        if args.open_report:
            try:
                webbrowser.open((output / "report.html").as_uri())
            except (OSError, webbrowser.Error) as exc:
                log("Open report.html manually: " + str(exc))
    return int(not summary["scan_complete"])


if __name__ == "__main__":
    raise SystemExit(main())
