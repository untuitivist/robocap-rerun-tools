"""Inventory named object Mocap exports without changing or uploading source data."""

from __future__ import annotations

import argparse
import codecs
import csv
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from .mocap_metadata import parse_mocap_capture_directory
from .session_layout import is_mocap_directory_name

EXTENSIONS = {".csv", ".trc", ".bvh", ".xrs", ".c3d"}
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "_artifacts",
    "_analysis",
    "_modelscope_dataset",
    "raw_calibration",
    "_raw_calibration_cache",
    "$recycle.bin",
    "system volume information",
    "windows",
    "node_modules",
}
EXPORT_NAME = re.compile(
    r"^(?P<capture>mocap-[a-z]+\d+-S\d+-.+-\d+p\d*)[-_](?P<label>.+)$", re.IGNORECASE
)
SESSION_NAME = re.compile(r"^\d{8}_\d{6}_session\d+$", re.IGNORECASE)
BODY_NAME = re.compile(r"^body\d*(?:[-_].*)?$", re.IGNORECASE)
TRACKER_NAME = re.compile(r"^tracker(?:\d+)?(?:[-_].*)?$", re.IGNORECASE)
UNNAMED_NAME = re.compile(r"^(?:unnamed|unlabeled|unlabelled)\d*(?:[-_ ].*)?$", re.IGNORECASE)
GENERIC_EXPORT_SUFFIX = re.compile(
    r"(?:^|[-_])(?P<label>Body\d*(?:[-_](?:Left|Right))?|"
    r"Tracker\d*(?:[-_](?:start|end))?|(?:Unnamed|Unlabeled|Unlabelled)\d*)$",
    re.IGNORECASE,
)
HUMAN_SEGMENTS = {
    "hips",
    "spine",
    "spine1",
    "spine2",
    "spine3",
    "head",
    "neck",
    "leftarm",
    "rightarm",
    "leftforearm",
    "rightforearm",
    "lefthand",
    "righthand",
    "leftupleg",
    "rightupleg",
    "leftleg",
    "rightleg",
    "leftfoot",
    "rightfoot",
}
FILE_FIELDS = [
    "root",
    "session_id",
    "action_id",
    "participant",
    "collection_session_index",
    "repetition_count",
    "source_directory",
    "capture_key",
    "capture_path",
    "object_name",
    "category",
    "extension",
    "size_bytes",
    "header_status",
    "content_type",
    "encoding",
    "declared_frames",
    "declared_rate_hz",
    "header_names",
    "issues",
    "file_path",
]
OBJECT_FIELDS = [
    "object_name",
    "name_variants",
    "recording_count",
    "source_copy_count",
    "file_count",
    "header_ok_files",
    "empty_files",
    "lfs_pointer_files",
    "other_header_files",
    "formats",
    "total_bytes",
    "participant_count",
    "participants",
    "action_ids",
    "example_file",
]
SESSION_FIELDS = [
    "capture_key",
    "session_id",
    "action_id",
    "participant",
    "source_directory",
    "source_copy_count",
    "file_count",
    "object_count",
    "object_names",
    "object_files",
    "body_files",
    "tracker_files",
    "unnamed_files",
    "header_issue_files",
    "finding",
    "source_paths",
]


def filename_parts(path: Path) -> tuple[str, object | None]:
    """Keep the complete object suffix, including spaces, hyphens and instance numbers."""
    # Older collections used names such as mocap-P01-St-wangyang1. Their exact
    # directory prefix is still reliable even though capture metadata cannot be parsed.
    anchor = next((p for p in path.parents if is_mocap_directory_name(p.name)), None)
    if anchor is not None and anchor.name.casefold() != "mocap":
        for separator in ("-", "_"):
            prefix = anchor.name + separator
            if path.stem.casefold().startswith(prefix.casefold()) and len(path.stem) > len(prefix):
                return path.stem[len(prefix) :], parse_mocap_capture_directory(anchor.name)
    match = EXPORT_NAME.fullmatch(path.stem)
    if match:
        capture = parse_mocap_capture_directory(match["capture"])
        if capture is not None:
            return match["label"], capture
    generic = GENERIC_EXPORT_SUFFIX.search(path.stem)
    if generic:
        return generic["label"], None
    return path.stem, None


def name_category(label: str) -> str:
    if BODY_NAME.fullmatch(label):
        return "body"
    if TRACKER_NAME.fullmatch(label):
        return "tracker"
    if UNNAMED_NAME.fullmatch(label):
        return "unnamed"
    return "object_candidate"


def _cells(line: str, suffix: str) -> list[str]:
    if suffix == ".csv":
        return [value.strip() for value in next(csv.reader([line]))]
    if "\t" in line:
        return [value.strip() for value in line.split("\t")]
    return line.split()


def _number(value: str):
    try:
        number = float(value)
        return number if number >= 0 and number < float("inf") else None
    except ValueError:
        return None


def inspect_header(path: Path, limit: int = 65536) -> dict:
    result = {
        "header_status": "unrecognized_header",
        "content_type": "unknown",
        "encoding": "",
        "declared_frames": None,
        "declared_rate_hz": None,
        "header_names": [],
        "issues": [],
    }
    try:
        before = path.stat()
        result["size_bytes"] = before.st_size
        with path.open("rb") as stream:
            prefix = stream.read(limit + 1)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            result["issues"].append("changed_during_read")
        if not prefix:
            result["header_status"] = "empty"
            return result
        if prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
            result["header_status"] = "git_lfs_pointer"
            return result
        if path.suffix.lower() == ".c3d":
            result.update(header_status="binary_not_inspected", content_type="c3d")
            return result
        # Keep the extra bytes while decoding so an arbitrary byte boundary cannot split a
        # multibyte character. An incomplete final line is never used as a complete header.
        encodings = (
            ["utf-16"]
            if prefix.startswith((b"\xff\xfe", b"\xfe\xff"))
            else [
                "utf-8-sig",
                "gb18030",
            ]
        )
        text = None
        for encoding in encodings:
            try:
                text = codecs.getincrementaldecoder(encoding)(errors="strict").decode(
                    prefix, final=len(prefix) <= limit
                )
                result["encoding"] = encoding
                break
            except UnicodeError:
                continue
        if text is None:
            result["header_status"] = "decode_error"
            return result
        lines = text.splitlines()
        if len(prefix) > limit:
            lines = lines[:-1]
        suffix = path.suffix.lower()
        names = []
        if suffix in {".csv", ".xrs"}:
            in_hierarchy = False
            has_data = False
            columns = None
            for line in lines:
                stripped = line.strip()
                if stripped == "[SegmentNames&Hierarchy]":
                    in_hierarchy = True
                    continue
                if stripped.startswith("["):
                    in_hierarchy = False
                    has_data |= stripped == "[SegmentData]"
                cells = _cells(line, suffix)
                if "NumFrames" in cells:
                    columns = cells
                elif columns and cells and any(cells):
                    values = dict(zip(columns, cells))
                    result["declared_frames"] = _number(values.get("NumFrames", ""))
                    result["declared_rate_hz"] = _number(values.get("DataFrameRate", ""))
                    columns = None
                if in_hierarchy and cells and cells[0] and cells[0].casefold() != "segment":
                    names.append(cells[0])
            if names and has_data:
                result.update(header_status="header_ok", content_type="segment_pose")
        elif suffix == ".trc":
            columns = None
            for line in lines:
                cells = _cells(line, suffix)
                if "NumFrames" in cells:
                    columns = cells
                elif columns and cells and any(cells):
                    values = dict(zip(columns, cells))
                    result["declared_frames"] = _number(values.get("NumFrames", ""))
                    result["declared_rate_hz"] = _number(values.get("DataRate", ""))
                    columns = None
                if cells and cells[0].casefold() == "frame#":
                    offset = 3 if len(cells) > 2 and "time" in cells[2].lower() else 2
                    names = [value for value in cells[offset:] if value]
                    if names:
                        result.update(header_status="header_ok", content_type="marker_trajectory")
                    break
        elif suffix == ".bvh":
            motion = False
            for line in lines:
                stripped = line.strip()
                match = re.match(r"(?:ROOT|JOINT)\s+(.+)", stripped)
                if match:
                    names.append(match[1].strip())
                motion |= stripped == "MOTION"
                if stripped.startswith("Frames:"):
                    result["declared_frames"] = _number(stripped.split(":", 1)[1].strip())
                if stripped.startswith("Frame Time:"):
                    period = _number(stripped.split(":", 1)[1].strip())
                    result["declared_rate_hz"] = 1 / period if period else None
            if names and motion:
                result.update(header_status="header_ok", content_type="bvh_hierarchy")
        normalized = {name.casefold() for name in names}
        if (
            len(normalized & HUMAN_SEGMENTS) >= 3
            or sum(name.startswith(("lefthand", "righthand")) for name in normalized) >= 2
        ):
            result["content_type"] = "human_skeleton"
        result["header_names"] = names
        if result["header_status"] == "unrecognized_header" and len(prefix) > limit:
            result["header_status"] = "header_limit_reached"
    except (OSError, csv.Error) as exc:
        result.update(header_status="read_error")
        result["issues"].append(str(exc))
    return result


def describe_file(path: Path, root: Path, header_bytes: int) -> dict:
    label, file_capture = filename_parts(path)
    anchor = next((p for p in path.parents if is_mocap_directory_name(p.name)), path.parent)
    directory_capture = parse_mocap_capture_directory(anchor.name)
    capture = file_capture or directory_capture
    session = next((p for p in path.parents if SESSION_NAME.fullmatch(p.name)), None)
    header = inspect_header(path, header_bytes)
    category = name_category(label)
    if header["content_type"] == "human_skeleton":
        if category != "body":
            header["issues"].append("name_contains_human_skeleton")
        category = "body"
    if file_capture and directory_capture:
        keys = ("action_id", "collection_session_index", "participant", "repetition_count")
        if any(getattr(file_capture, key) != getattr(directory_capture, key) for key in keys):
            header["issues"].append("filename_directory_identity_conflict")
    source_name = capture.source_directory if capture else anchor.name
    if capture is None:
        header["issues"].append("unparsed_capture_identity")
    logical_key = (
        f"{session.name if session else 'standalone'}/{source_name}" if capture else str(anchor)
    )
    return {
        "root": str(root),
        "session_id": session.name if session else "",
        "action_id": capture.action_id if capture else "",
        "participant": capture.participant if capture else "",
        "collection_session_index": capture.collection_session_index if capture else None,
        "repetition_count": capture.repetition_count if capture else None,
        "source_directory": source_name,
        "capture_key": logical_key,
        "capture_path": str(anchor),
        "object_name": label,
        "category": category,
        "extension": path.suffix.lower(),
        "file_path": str(path),
        **header,
    }


def _linked(path: Path) -> bool:
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(path)


def scan(roots: list[Path], output: Path, *, header_bytes=65536, log=print):
    rows, errors = [], []
    seen = set()
    directories = 0

    def error(path, message):
        errors.append({"path": str(path), "error": message})
        log(f"SCAN ERROR: {path}: {message}")

    for specified in roots:
        root = specified.expanduser().resolve()
        if not root.is_dir():
            error(root, "root_unavailable")
            continue
        if root == output or root.is_relative_to(output):
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
            directories += 1
            dirs[:] = sorted(
                d
                for d in dirs
                if d.casefold() not in SKIP_DIRS
                and not _linked(current / d)
                and not (current / d).resolve().is_relative_to(output)
            )
            inside_mocap = any(is_mocap_directory_name(p.name) for p in [current, *current.parents])
            for name in sorted(files):
                path = current / name
                if path.suffix.lower() not in EXTENSIONS or _linked(path):
                    continue
                if not inside_mocap and filename_parts(path)[1] is None:
                    continue
                row = describe_file(path, root, header_bytes)
                rows.append(row)
                if row["header_status"] == "read_error" or "changed_during_read" in row["issues"]:
                    error(path, "; ".join(row["issues"]))
                if row["category"] == "object_candidate":
                    log(f"OBJECT {row['object_name']}: {path} [{row['header_status']}]")
            if directories % 500 == 0:
                log(f"Scanned {directories} directories; inspected {len(rows)} motion files")
    return rows, errors


def summarize(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    by_object, by_capture = defaultdict(list), defaultdict(list)
    for row in rows:
        by_capture[row["capture_key"]].append(row)
        if row["category"] == "object_candidate":
            by_object[row["object_name"].casefold()].append(row)
    objects = []
    for _, group in sorted(by_object.items()):
        statuses = Counter(row["header_status"] for row in group)
        formats = Counter(row["extension"] for row in group)
        names = sorted({row["object_name"] for row in group})
        people = sorted({row["participant"] for row in group if row["participant"]})
        objects.append(
            {
                "object_name": names[0],
                "name_variants": names,
                "recording_count": len({row["capture_key"] for row in group}),
                "source_copy_count": len({(r["capture_path"], r["capture_key"]) for r in group}),
                "file_count": len(group),
                "header_ok_files": statuses["header_ok"],
                "empty_files": statuses["empty"],
                "lfs_pointer_files": statuses["git_lfs_pointer"],
                "other_header_files": len(group)
                - sum(statuses[k] for k in ("header_ok", "empty", "git_lfs_pointer")),
                "formats": [f"{key}: {count}" for key, count in sorted(formats.items())],
                "total_bytes": sum(row.get("size_bytes", 0) for row in group),
                "participant_count": len(people),
                "participants": people,
                "action_ids": sorted({row["action_id"] for row in group if row["action_id"]}),
                "example_file": group[0]["file_path"],
            }
        )
    sessions = []
    for key, group in sorted(by_capture.items()):
        names = sorted(
            {
                row["object_name"].casefold()
                for row in group
                if row["category"] == "object_candidate"
            }
        )
        counts = Counter(row["category"] for row in group)
        first = group[0]
        paths = sorted({row["capture_path"] for row in group})
        sessions.append(
            {
                "capture_key": key,
                **{
                    field: first[field]
                    for field in ("session_id", "action_id", "participant", "source_directory")
                },
                "source_copy_count": len(paths),
                "file_count": len(group),
                "object_count": len(names),
                "object_names": names,
                "object_files": counts["object_candidate"],
                "body_files": counts["body"],
                "tracker_files": counts["tracker"],
                "unnamed_files": counts["unnamed"],
                "header_issue_files": sum(r["header_status"] != "header_ok" for r in group),
                "finding": "named_object_candidates" if names else "no_named_object_exports",
                "source_paths": paths,
            }
        )
    return objects, sessions


def _cell(value):
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return "" if value is None else str(value)


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _cell(row.get(key)) for key in fields})


def _table(rows: list[dict], columns: dict[str, str]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for label in columns.values())
    body = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(_cell(row.get(key)))}</td>" for key in columns)
        + "</tr>"
        for row in rows
    )
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def write_reports(output: Path, roots: list[Path], rows: list[dict], errors: list[dict]):
    objects, sessions = summarize(rows)
    categories = Counter(row["category"] for row in rows)
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "roots": [str(path.resolve()) for path in roots],
        "scan_complete": not errors,
        "scan_errors": len(errors),
        "files": len(rows),
        "recordings": len(sessions),
        "object_names": len(objects),
        "object_files": categories["object_candidate"],
        "recordings_with_objects": sum(row["object_count"] > 0 for row in sessions),
        "categories": dict(categories),
        "header_statuses": dict(Counter(row["header_status"] for row in rows)),
        "scope": "Local directories; no archive extraction, remote requests or uploads.",
        "classification": "Non-Body/Tracker/Unnamed suffixes are candidates, not proven objects. "
        "Anatomical skeleton headers override suffix classification. Header checks do not "
        "validate full trajectories. Formats count as separate files but one recording.",
        "duplicate_policy": "Overlapping roots are visited once. Separate copies retain file "
        "counts, but equal Session name + compact capture name share one recording key. "
        "Copies are not verified as byte-identical.",
    }
    write_csv(output / "files.csv", rows, FILE_FIELDS)
    write_csv(output / "objects.csv", objects, OBJECT_FIELDS)
    write_csv(output / "sessions.csv", sessions, SESSION_FIELDS)
    write_csv(output / "scan_errors.csv", errors, ["path", "error"])
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    root_text = html.escape("; ".join(summary["roots"]))
    completeness = "扫描完成" if summary["scan_complete"] else "扫描不完整，请查看错误表"
    category_rows = [
        {
            "category": category,
            "files": count,
            "names": sorted({r["object_name"] for r in rows if r["category"] == category}),
        }
        for category, count in sorted(categories.items())
    ]
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>物品动捕文件统计</title><style>
body{font:15px/1.6 system-ui,sans-serif;margin:32px;color:#182434;background:#f5f7fa}
h1{font-size:25px}h2{font-size:20px;margin-top:30px}p{max-width:1100px}
.scroll{overflow:auto;background:white;border:1px solid #dce2ea;border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:9px 12px;
border-bottom:1px solid #e5e9ef;text-align:left;vertical-align:top;overflow-wrap:anywhere}
th{background:#edf2f8;white-space:nowrap}input{padding:8px;width:360px;max-width:80%}
</style><h1>物品动捕文件统计</h1>"""
    document += (
        f"<p>{completeness} · {html.escape(summary['generated_at'])}<br>目录：{root_text}</p>"
    )
    document += (
        f"<p>动捕文件 <b>{len(rows)}</b> 个；录制标识 <b>{len(sessions)}</b> 个；"
        f"候选物品名称 <b>{len(objects)}</b> 个；候选物品文件 <b>{summary['object_files']}</b> 个；"
        f"涉及录制 <b>{summary['recordings_with_objects']}</b> 个。</p>"
        "<p>候选物品按文件名后缀识别，需人工确认；Body、Tracker、Unnamed 单列。"
        "同一物品的 CSV/TRC/BVH 分别计文件，但录制数不按格式重复累加。"
        "文件头可读不代表完整轨迹有效；没有具名导出不等于没有采集物品。"
        "压缩包未展开，Git LFS 占位文件单独标明。</p>"
        "<p>同名 Session＋完整动捕目录名按一个录制标识统计；不同根目录中的副本"
        "仍分别计入文件数，未核验副本内容一致性。</p>"
        "<p><a href='objects.csv'>物品汇总 CSV</a> · <a href='sessions.csv'>录制汇总 CSV</a> · "
        "<a href='files.csv'>完整文件明细 CSV</a> · <a href='scan_errors.csv'>扫描错误 CSV</a></p>"
        "<label>筛选表格 <input id='query' placeholder='物品、动作、参与者或 Session'></label>"
        "<h2>文件分类</h2>"
    )
    document += _table(category_rows, {"category": "分类", "files": "文件数", "names": "名称"})
    document += "<h2>候选物品汇总</h2>"
    document += _table(
        objects,
        {
            "object_name": "物品后缀",
            "recording_count": "录制数",
            "source_copy_count": "源副本数",
            "file_count": "文件数",
            "formats": "格式",
            "participants": "参与者",
            "action_ids": "动作",
            "header_ok_files": "表头可读",
            "empty_files": "空文件",
            "lfs_pointer_files": "LFS占位",
            "other_header_files": "其他待检查",
        },
    )
    document += "<h2>涉及候选物品的录制</h2>"
    document += _table(
        [s for s in sessions if s["object_count"]],
        {
            "session_id": "Session",
            "source_directory": "动捕目录",
            "object_names": "物品",
            "object_files": "物品文件数",
            "source_copy_count": "源副本数",
            "header_issue_files": "表头待检查文件数",
            "source_paths": "本地位置",
        },
    )
    if errors:
        document += "<h2>扫描错误</h2>" + _table(errors, {"path": "路径", "error": "错误"})
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
        help="Local, mapped or UNC source root; repeat for multiple roots",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_artifacts/obj_repair_upload")
        / datetime.now(UTC).astimezone().strftime("%Y%m%d_%H%M%S_%f"),
        help="New report directory (must not already exist)",
    )
    parser.add_argument(
        "--header-bytes",
        type=int,
        default=65536,
        help="Maximum file-header bytes to inspect (default: 65536)",
    )
    args = parser.parse_args(argv)
    if args.header_bytes < 1024:
        parser.error("--header-bytes must be at least 1024")
    output = args.output.expanduser().resolve()
    if any(root.expanduser().resolve().is_relative_to(output) for root in args.root):
        parser.error("Output must not contain a source root")
    if output.exists():
        parser.error("Output already exists; choose a new directory to preserve earlier reports")
    output.mkdir(parents=True)
    with (output / "run.log").open("w", encoding="utf-8") as logfile:

        def log(message):
            print(message, flush=True)
            logfile.write(str(message) + "\n")
            logfile.flush()

        log("Object Mocap inventory: filenames and bounded headers; source data stays unchanged")
        log("Reports: " + str(output))
        rows, errors = scan(args.root, output, header_bytes=args.header_bytes, log=log)
        summary = write_reports(output, args.root, rows, errors)
        log(json.dumps(summary, ensure_ascii=False))
        log("FINISHED: " + str(output / "report.html"))
    return int(not summary["scan_complete"])


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
