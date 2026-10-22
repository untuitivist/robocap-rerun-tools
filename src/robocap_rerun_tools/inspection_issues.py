"""Shared, multi-label classification for inspection and dataset statistics."""

from __future__ import annotations

import math

CLASSIFICATION_VERSION = 1
FRAME_ANOMALY_ORDER = (
    "mocap_extra", "mocap_missing", "third_person_extra", "third_person_missing",
)
ISSUE_LABELS = {
    "mocap_extra": ("帧数", "Mocap 多帧"),
    "mocap_missing": ("帧数", "Mocap 少帧"),
    "third_person_extra": ("帧数", "第三人称多帧"),
    "third_person_missing": ("帧数", "第三人称少帧"),
    "timestamp_missing": ("时间戳", "时间戳缺失或无效"),
    "timestamp_order": ("时间戳", "时间戳重复或倒退"),
    "timestamp_interval": ("时间戳", "采样间隔异常（待确认）"),
    "frame_index": ("时间戳", "帧序号跳号、重复或倒退"),
    "trajectory_missing": ("轨迹", "轨迹点部分缺失"),
    "empty_data": ("文件内容", "空文件或无有效数据"),
    "file_missing": ("文件结构", "缺文件或仅有占位文件"),
    "format_error": ("文件结构", "格式损坏或行列数不符"),
    "invalid_data": ("文件内容", "无效数据"),
    "file_unreadable": ("检查状态", "文件无法读取"),
    "ambiguous": ("文件结构", "文件或目录匹配不唯一"),
    "manifest_mismatch": ("文件结构", "文件与清单不一致"),
    "inspection_incomplete": ("检查状态", "检查未完成"),
    "inspection_legacy": ("检查状态", "旧版报告待复检"),
    "inspection_stale": ("检查状态", "源文件已变化，报告待复检"),
    "format_note": ("格式说明", "已兼容的导出格式"),
}
STATE_CODES = {
    "missing": "file_missing", "lfs_pointer": "file_missing",
    "zero_byte": "empty_data", "empty_data": "empty_data",
    "invalid_format": "format_error", "schema_error": "format_error",
    "count_mismatch": "format_error", "column_mismatch": "format_error",
    "partial_data": "trajectory_missing", "invalid_data": "invalid_data",
    "read_error": "file_unreadable", "ambiguous": "ambiguous",
    "manifest_mismatch": "manifest_mismatch",
    "changed": "inspection_stale",
    "not_checked": "inspection_incomplete", "tool_unavailable": "inspection_incomplete",
    "active_database": "inspection_incomplete", "requirements_unknown": "inspection_incomplete",
}


def _nonnegative_integer(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) and number >= 0 and number.is_integer() else None


def classify_frame_count_anomalies(payload):
    reference = _nonnegative_integer(payload.get("referenceFrames"))
    mocap = _nonnegative_integer(payload.get("mocapFrames"))
    third = _nonnegative_integer(payload.get("thirdFrames"))
    ratio = _nonnegative_integer(payload.get("ratio"))
    if None in (reference, mocap, third) or ratio not in {4, 8}:
        return None
    anomalies = []
    for actual, expected, prefix in (
        (mocap, ratio * (reference + 1), "mocap"), (third, reference + 1, "third_person"),
    ):
        if actual != expected:
            anomalies.append(prefix + ("_extra" if actual > expected else "_missing"))
    return tuple(anomalies)


def issue_record(code, *, path="", detail="", count=None, severity="error"):
    group, label = ISSUE_LABELS[code]
    return {"code": code, "group": group, "label": label, "severity": severity,
            "path": path, "count": count, "detail": detail}


def quality_status(issues):
    severities = {row["severity"] for row in issues}
    return ("error" if "error" in severities else "unchecked" if "unchecked" in severities
            else "review" if "warning" in severities else "passed")


def classify_inspection(payload):
    try:
        return _classify_inspection(payload)
    except (TypeError, ValueError, AttributeError) as exc:
        issues = [issue_record("inspection_incomplete", severity="unchecked",
                               detail=f"检查记录格式无效：{type(exc).__name__}: {exc}")]
        return {"status": "unchecked", "issues": issues}


def _classify_inspection(payload):
    from .session_integrity import INTEGRITY_VERSION

    collected = {}

    def add(code, path="", detail="", count=None, severity="error"):
        key = code, path
        row = issue_record(code, path=path, detail=detail, count=count, severity=severity)
        if key in collected:
            previous = collected[key]
            row["count"] = max(previous["count"] or 0, count or 0) or None
            row["detail"] = "; ".join(dict.fromkeys(
                value for value in (previous["detail"], detail) if value
            ))
        collected[key] = row

    def records(value, label):
        if not isinstance(value, list):
            add("inspection_incomplete", detail=f"{label}列表无效", severity="unchecked")
            return []
        if any(not isinstance(row, dict) for row in value):
            add("inspection_incomplete", detail=f"{label}包含无效记录", severity="unchecked")
        return [row for row in value if isinstance(row, dict)]

    anomalies = classify_frame_count_anomalies(payload)
    if anomalies is None:
        add("inspection_incomplete", detail="缺少有效的帧数或比例", severity="unchecked")
    else:
        for code in anomalies:
            add(code, detail="实际帧数不满足 n:ratio*(n+1):n+1")
    integrity = payload.get("integrity")
    if not isinstance(integrity, dict) or not isinstance(integrity.get("checks"), list):
        add("inspection_incomplete", detail="未执行文件结构与内容检查", severity="unchecked")
        integrity = {}
    if not payload.get("files"):
        add("inspection_incomplete", detail="没有时间戳数据流检查记录", severity="unchecked")
    if (payload.get("classificationVersion") != CLASSIFICATION_VERSION
            or integrity.get("version") != INTEGRITY_VERSION):
        add("inspection_legacy", detail="需要重建报告以覆盖时间戳与内容检查", severity="unchecked")
    for row in records(integrity.get("checks", []), "文件检查"):
        state = row.get("state")
        path, detail = row.get("path", ""), row.get("detail", "")
        code = STATE_CODES.get(state)
        if code:
            severity = "unchecked" if code.startswith("inspection_") or code == "file_unreadable" else "error"
            add(code, path, detail, severity=severity)
        if row.get("missing_samples", 0) and state != "empty_data":
            add("trajectory_missing", path, detail, row["missing_samples"])
        timestamp = row.get("timestamp_detail", {})
        for key, code in (("missing_timestamps", "timestamp_missing"),
                          ("nonincreasing_timestamps", "timestamp_order")):
            if timestamp.get(key, 0):
                add(code, path, detail, timestamp[key])
        if state == "timestamp_error" and not timestamp:
            add("invalid_data", path, detail)
        for note in row.get("warnings", []):
            add("format_note", path, note, severity="info")
    for row in records(payload.get("files", []), "时间戳检查"):
        path = row.get("path", "")
        events = row.get("eventCounts") or {}
        if not isinstance(events, dict):
            add("inspection_incomplete", path, "逐点事件计数无效", severity="unchecked")
            continue
        if row.get("parseError") or "parse error:" in row.get("stream", ""):
            add("inspection_incomplete", path, row.get("parseError") or row["stream"],
                severity="unchecked")
        elif row.get("sampleCount") == 0:
            add("inspection_incomplete", path, "没有可检查的时间戳行", severity="unchecked")
        for count, code, severity in (
            (row.get("missing", 0), "timestamp_missing", "error"),
            (events.get("timestamp_duplicate", 0) + events.get("timestamp_reversed", 0),
             "timestamp_order", "error"),
            (events.get("timestamp_too_short", 0) + events.get("timestamp_too_long", 0),
             "timestamp_interval", "warning"),
            (row.get("frameIssues", 0), "frame_index", "error"),
        ):
            if count:
                add(code, path, "逐点证据见时间戳异常表", count, severity)
    issues = list(collected.values())
    return {"status": quality_status(issues), "issues": issues}
