"""Full local validation and durable batch results, before any remote skip decision."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .inventory_report import render_table, write_csv
from .session_integrity import INTEGRITY_VERSION, audit_session, save_integrity, source_signature


def blocks_normal_upload(report):
    return bool(report["errors"]) or any(
        row.get("state") in {"active_database", "changed", "tool_unavailable", "partial_data"}
        or (row.get("state") == "not_checked" and row.get("path", "").lower().endswith(".wav"))
        for row in report["checks"]
    )


def inspection_is_current(reference, ratio):
    from .dataset_statistics import load_report_payload

    try:
        payload = load_report_payload(reference.report_path)
        integrity = payload.get("integrity", {})
        signature = integrity.get("source_signature")
        return (
            payload.get("ratio") == ratio
            and integrity.get("version") == INTEGRITY_VERSION
            and signature is not None
            and signature
            in {
                source_signature(reference.session_dir),
                source_signature(reference.session_dir, segment=reference.segment),
            }
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def fault_source_files(session: Path, segment=None):
    """Include existing capture files even when Mocap is absent or ambiguous."""
    from .data_packager import is_excluded
    from .session_integrity import matches_segment, source_files

    files, errors = source_files(session)
    if errors:
        raise ValueError("Cannot inventory all source files: " + "; ".join(str(p) for p, _ in errors))
    return [p for p in files if p.name.lower() != "manifest.json"
            and not is_excluded(p, session, False, False) and matches_segment(p, segment)]


def fault_relative_path(path: Path, session: Path):
    from .session_layout import canonical_mocap_relative_path, discover_mocap_directories

    if len(discover_mocap_directories(session)) > 1:
        return path.relative_to(session)  # Keep ambiguous captures separate, never overwrite one.
    return canonical_mocap_relative_path(path, session)


def local_upload_inventory(session: Path, mocap_files, progress=None, *, quality_target="normal"):
    from .data_packager import discover_package_files
    from .modelscope_publisher import resolve_mocap_files
    from .session_layout import canonical_mocap_relative_path, is_path_under_mocap
    from .upload_verification import file_digest

    files = fault_source_files(session) if quality_target == "fault" else [
        p
        for p in discover_package_files(session, None, include_artifacts=False, include_rrd=False)
        if not is_path_under_mocap(p, session)
    ] + resolve_mocap_files(session, mocap_files)
    inventory = {}
    for path in files:
        relative = (fault_relative_path(path, session) if quality_target == "fault"
                    else canonical_mocap_relative_path(path, session)).as_posix()
        if relative in inventory:
            raise ValueError(f"Duplicate packaged path: {relative}")
        if progress:
            progress(f"[local hash] {session.name}/{relative}")
        before = path.stat()
        digest = file_digest(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"Source changed while hashing: {path}")
        inventory[relative] = {"size": before.st_size, "sha256": digest}
    return inventory


def inspect_local_batch(sessions, output: Path, progress=print):
    from .media_tools import activate_media_tools

    media = activate_media_tools()
    reports = {}
    for index, session in enumerate(sessions, 1):
        progress(f"[local integrity {index}/{len(sessions)}] {session}")
        try:
            report = audit_session(
                session,
                ffmpeg=media.ffmpeg,
                ffprobe=media.ffprobe,
                progress=progress,
                video_log=output / "video_decode.log",
            )
            save_integrity(session, report)
        except (OSError, ValueError, RuntimeError) as exc:
            report = {
                "status": "failed",
                "errors": 1,
                "warnings": 0,
                "checks": [
                    {"path": ".", "state": "read_error", "severity": "error", "detail": str(exc)}
                ],
            }
        reports[session] = report
        # Save after each Session so interruption does not erase completed checks.
        (output / "local_integrity.json").write_text(
            json.dumps(
                {str(path): value for path, value in reports.items()}, ensure_ascii=False, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
    return reports


def write_batch_report(output: Path, reports, decisions, scan_errors=()):
    rows, issues = [], []
    for session, report in reports.items():
        rows.append(
            {
                "session": str(session),
                "local_status": report["status"],
                "errors": report["errors"],
                "warnings": report["warnings"],
                "mocap_rigid_names": report.get("mocap_rigid_names", []),
                **decisions.get(session, {"upload_status": "not_processed", "detail": ""}),
            }
        )
        issues.extend(
            {"session": str(session), **row}
            for row in report["checks"]
            if row.get("severity") != "pass"
        )
    issues.extend(
        {
            "session": row["path"],
            "path": ".",
            "state": "scan_error",
            "severity": "error",
            "detail": row["error"],
        }
        for row in scan_errors
    )
    columns = {
        "session": "Session",
        "local_status": "本地完整性",
        "errors": "错误数",
        "warnings": "待确认数",
        "mocap_rigid_names": "实际对象名称",
        "target": "目标数据集",
        "error_types": "错误类型",
        "upload_status": "上传结果",
        "detail": "原因",
    }
    issue_columns = {
        "session": "Session",
        "path": "文件/要求",
        "state": "问题类型",
        "severity": "级别",
        "detail": "具体问题",
    }
    write_csv(output / "sessions.csv", rows, list(columns))
    write_csv(output / "issues.csv", issues, list(issue_columns))
    (output / "report.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>批量完整性检查与上传</title>'
        "<style>body{font:14px sans-serif;margin:24px}table{border-collapse:collapse}"
        "td,th{border:1px solid #ccc;padding:6px;text-align:left}.scroll{overflow:auto}</style>"
        "<h1>批量完整性检查与上传</h1><p>本地错误与远端上传结果分别记录；"
        "动作物品清单未配置属于待确认。帧数口径仍单独保留。</p>"
        + f"<p>结果目录：{html.escape(str(output))}</p>"
        + render_table(rows, columns)
        + "<h2>本地问题明细</h2>"
        + render_table(issues, issue_columns),
        encoding="utf-8",
    )
