"""Fault-dataset admission, metadata and isolated repository configuration."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

from dotenv import dotenv_values, set_key

from .dataset_statistics import (
    classify_frame_count_anomalies,
    discover_segment_references,
    load_report_payload,
)

FAULT_REPO_KEY = "MODELSCOPE_ERROR_REPO_ID"
DEFAULT_FAULT_REPO = "untuitivist/EgoMotionActions-fault"
TARGET_MARKER = ".publishing_target.json"


def fault_settings(env_path: Path | None = None):
    from .modelscope_publisher import DEFAULT_ENV_PATH, load_modelscope_settings, validate_repo_id

    settings = load_modelscope_settings(env_path or DEFAULT_ENV_PATH)
    values = dotenv_values(settings.env_path, encoding="utf-8", interpolate=False)
    repository = validate_repo_id(
        os.environ.get(FAULT_REPO_KEY) or values.get(FAULT_REPO_KEY) or DEFAULT_FAULT_REPO
    )
    if repository == settings.repo_id:
        raise ValueError("Normal and fault datasets must use different repositories.")
    return replace(settings, repo_id=repository)


def save_fault_repository(repository: str, env_path: Path | None = None) -> str:
    from .modelscope_publisher import (
        DEFAULT_ENV_PATH,
        ensure_env_file,
        load_modelscope_settings,
        validate_repo_id,
    )

    repository = validate_repo_id(repository)
    path = ensure_env_file(env_path or DEFAULT_ENV_PATH)
    if repository == load_modelscope_settings(path).repo_id:
        raise ValueError("Normal and fault datasets must use different repositories.")
    set_key(str(path), FAULT_REPO_KEY, repository, quote_mode="always", encoding="utf-8")
    os.environ[FAULT_REPO_KEY] = repository
    return repository


def collect_fault_quality(source: Path, segment: str | None = None) -> dict:
    references = discover_segment_references(source)
    if segment:
        references = tuple(item for item in references if item.segment == segment)
    if not references:
        raise ValueError("Fault upload requires a Robocap reference video and valid inspection.")
    details = {}
    errors: set[str] = set()
    for reference in references:
        payload = load_report_payload(reference.report_path)
        anomalies = classify_frame_count_anomalies(payload)
        if anomalies is None:
            raise ValueError(f"Invalid inspection frame counts: {reference.segment}")
        errors.update(anomalies)
        n = int(payload["referenceFrames"])
        ratio = int(payload["ratio"])
        mocap = int(payload["mocapFrames"])
        third = int(payload["thirdFrames"])
        details[reference.segment] = {
            "ratio": ratio,
            "reference_frames": n,
            "mocap_frames": mocap,
            "expected_mocap_frames": ratio * (n + 1),
            "mocap_frame_difference": mocap - ratio * (n + 1),
            "third_person_frames": third,
            "expected_third_person_frames": n + 1,
            "third_person_frame_difference": third - (n + 1),
            "error_types": list(anomalies),
            "report_sha256": hashlib.sha256(reference.report_path.read_bytes()).hexdigest(),
        }
    if not errors:
        raise ValueError(
            "No frame-count difference: clean Sessions cannot enter the fault dataset."
        )
    return {"quality_status": "error", "error_types": sorted(errors), "quality_details": details}


def fault_readme(document: str, repository: str = DEFAULT_FAULT_REPO) -> str:
    document = document.replace("untuitivist/EgoMotionActions", repository)
    # Keep the original classification YAML and shared collection documentation.
    start = document.find("\n# ")
    start = max(start, 0)
    introduction = (
        "\n# EgoMotionActions-fault\n\n"
        "本数据集保存经过有效检查确认存在帧数差异的采集 Session，用于异常分析与对齐研究。\n"
        "视频保留原文件；不压缩、不裁切、不补帧。未检查、报告无效或缺失动捕的数据不收录。\n\n"
        "This companion dataset contains inspected recordings with frame-count mismatches. "
        "It is not a clean synchronized training split. Original videos are preserved byte-for-byte.\n\n"
        "路径：`EgoMotionActions/<YYYYMMDD>/<action_id>/<session_id>/`，日期按 UTC+08:00。\n"
        "`metadata.jsonl` 和 `manifest.json` 包含 `quality_status=error`、`error_types` 数组以及 "
        "`quality_details` 中每个 Segment 的实际/期望帧数、差值和检查报告 SHA-256。\n"
        "错误类型：`mocap_extra`、`mocap_missing`、`third_person_extra`、`third_person_missing`。"
        "同一 Session 可以有多种错误，但只保存一份。帧数规则为 `n:ratio*(n+1):n+1`，ratio 为 4 或 8。\n\n"
        "## Shared collection specification / 共用采集说明\n"
        "下文的动作与设备规范沿用正常数据集；这些规范不代表本库每条数据已通过同步检查。\n"
    )
    return document[:start] + introduction + document[start:]


def bind_staging_target(root: Path, target: str) -> None:
    if target not in {"normal", "fault"}:
        raise ValueError(f"Unknown publishing target: {target}")
    marker = root / TARGET_MARKER
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8"))["target"] != target:
            raise ValueError("Staging directory belongs to a different publishing target.")
    elif target == "fault" and (root / "metadata.jsonl").exists():
        raise ValueError("Use an empty, isolated staging directory for fault uploads.")
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"target": target}), encoding="utf-8")


def validate_fault_staging(root: Path) -> tuple[str, ...]:
    from .modelscope_publisher import load_staged_dataset

    staged = load_staged_dataset(root)
    entries = [
        json.loads(line)
        for line in staged.metadata_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for entry in entries:
        manifest = json.loads((root / entry["manifest"]).read_text(encoding="utf-8"))
        for record in (entry, manifest):
            if record.get("quality_status") != "error" or not record.get("error_types"):
                raise ValueError(f"Missing fault classification: {entry['session_id']}")
        for key in ("quality_status", "error_types", "quality_details"):
            if entry.get(key) != manifest.get(key):
                raise ValueError(f"Fault metadata differs from manifest: {entry['session_id']}")
        validate_quality_record(entry)
    return staged.pending_session_paths


def validate_quality_record(record: dict) -> None:
    details = record.get("quality_details")
    if not isinstance(details, dict) or not details:
        raise ValueError("Missing per-Segment fault details")
    errors = set()
    for detail in details.values():
        payload = {
            "ratio": detail.get("ratio"),
            "referenceFrames": detail.get("reference_frames"),
            "mocapFrames": detail.get("mocap_frames"),
            "thirdFrames": detail.get("third_person_frames"),
        }
        classified = classify_frame_count_anomalies(payload)
        if classified is None or list(classified) != detail.get("error_types"):
            raise ValueError("Fault classification does not match frame counts")
        n = payload["referenceFrames"]
        expected = {
            "expected_mocap_frames": payload["ratio"] * (n + 1),
            "expected_third_person_frames": n + 1,
            "mocap_frame_difference": payload["mocapFrames"] - payload["ratio"] * (n + 1),
            "third_person_frame_difference": payload["thirdFrames"] - (n + 1),
        }
        if any(detail.get(key) != value for key, value in expected.items()):
            raise ValueError("Fault expected counts or differences are inconsistent")
        errors.update(classified)
    if (
        not errors
        or sorted(errors) != record.get("error_types")
        or record.get("quality_status") != "error"
    ):
        raise ValueError("Session fault classification does not match Segment classifications")
