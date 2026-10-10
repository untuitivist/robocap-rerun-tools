"""Classify capture defects separately from checks that could not be completed."""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

from . import batch_completeness
from .dataset_statistics import (
    classify_frame_count_anomalies,
    discover_segment_references,
    load_report_payload,
)

QUALITY_FIELDS = (
    "quality_status", "error_types", "quality_details", "local_integrity", "inspection_issues",
)


def integrity_error_types(report):
    return {
        "local_" + row["state"]
        for row in report.get("checks", [])
        if row.get("severity") == "error" or row.get("state") == "partial_data"
    }


def unstable_source(report):
    return any(
        row.get("state") in {"active_database", "changed"}
        for row in report.get("checks", [])
    )


def portable_integrity(report, source):
    def portable(value):
        if isinstance(value, str):
            return value.replace(str(source), ".").replace(source.as_posix(), ".")
        if isinstance(value, list):
            return [portable(item) for item in value]
        if isinstance(value, dict):
            return {key: portable(item) for key, item in value.items()}
        return value

    compact = {
        key: value for key, value in report.items()
        if key in {"version", "status", "errors", "warnings", "segments", "mocap_rigid_names", "fileCount"}
    }
    compact["checks"] = [row for row in report.get("checks", []) if row.get("severity") != "pass"]
    return portable(compact)


def assess_quality(source: Path, integrity, *, segment=None, ratio=None):
    """A confirmed defect admits a Session to fault; unknown quality never means clean."""
    errors = integrity_error_types(integrity)
    details, inspection_issues = {}, []
    references = {
        ref.segment: ref for ref in discover_segment_references(source)
        if segment is None or ref.segment == segment
    }
    segments = {segment} if segment else set(integrity.get("segments", [])) | set(references)
    if not segments:
        inspection_issues.append({"segment": "all", "reason": "No recording Segment detected"})
    for name in sorted(segments):
        reference = references.get(name)
        try:
            if reference is None:
                raise ValueError("No Robocap reference video")
            payload = load_report_payload(reference.report_path)
            selected_ratio = ratio if ratio is not None else payload.get("ratio")
            if not batch_completeness.inspection_is_current(reference, selected_ratio):
                raise ValueError("Inspection is stale, missing or uses a different ratio")
            anomalies = classify_frame_count_anomalies(payload)
            if anomalies is None:
                raise ValueError("Inspection frame counts are incomplete or invalid")
            n, r = int(payload["referenceFrames"]), int(payload["ratio"])
            mocap, third = int(payload["mocapFrames"]), int(payload["thirdFrames"])
            details[name] = {
                "ratio": r,
                "reference_frames": n,
                "mocap_frames": mocap,
                "expected_mocap_frames": r * (n + 1),
                "mocap_frame_difference": mocap - r * (n + 1),
                "third_person_frames": third,
                "expected_third_person_frames": n + 1,
                "third_person_frame_difference": third - (n + 1),
                "error_types": list(anomalies),
                "report_sha256": hashlib.sha256(reference.report_path.read_bytes()).hexdigest(),
            }
            errors.update(anomalies)
        except (OSError, ValueError, TypeError) as exc:
            reason = str(exc).replace(str(source), ".").replace(source.as_posix(), ".")
            inspection_issues.append({"segment": name, "reason": reason})
    critical_unchecked = any(
        row.get("state") in {"tool_unavailable", "active_database", "changed"}
        or (row.get("state") == "not_checked" and row.get("path", "").lower().endswith(".wav"))
        for row in integrity.get("checks", [])
    )
    status = "error" if errors else "unchecked" if inspection_issues or critical_unchecked else "passed"
    return {
        "quality_status": status,
        "error_types": sorted(errors),
        "quality_details": details,
        "local_integrity": portable_integrity(integrity, source),
        "inspection_issues": inspection_issues,
    }


def fault_report_document(session_id, quality):
    """A real completeness report when frame analysis cannot run; never invent counts."""
    payload = {"session": session_id, "sessionPath": session_id, **quality}
    encoded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (
        '<!doctype html><meta charset="utf-8"><title>Session 完整性与错误记录</title>'
        '<style>body{font:15px sans-serif;margin:24px}pre{white-space:pre-wrap}</style>'
        f"<h1>{html.escape(session_id)}</h1>"
        "<p>原始文件按实际内容归档。缺失文件和无法完成的帧数检查已列出；没有推测或补齐数据。</p>"
        f"<pre>{html.escape(json.dumps(payload, ensure_ascii=False, indent=2))}</pre>"
        f"<script>const report={encoded}; const eventTypes=[];</script>"
    )
