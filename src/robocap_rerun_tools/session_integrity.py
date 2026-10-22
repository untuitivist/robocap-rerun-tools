"""Expected Session layout plus actual file-content checks shared by CLI and BAT."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from pathlib import Path, PurePosixPath

from .file_integrity import (
    VIDEO_SUFFIXES,
    WARNING_STATES,
    inspect_file,
    result,
)
from .mocap_integrity import DEFAULT_REQUIREMENTS, check_mocap_exports
from .mocap_inventory import mocap_names
from .session_layout import is_mocap_directory_name

CAMERAS = ("left", "left_eye", "left_front", "right", "right_eye", "right_front")
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
    "rerun",
    "$recycle.bin",
    "system volume information",
    "windows",
    "node_modules",
}
SESSION_NAME = re.compile(r"^\d{8}_\d{6}_session\d+$", re.IGNORECASE)
SEGMENT_NAME = re.compile(r"^segment\d+$", re.IGNORECASE)
STREAM_SEGMENT = re.compile(r"^(?:robocap_|robowrist_|audio_)(segment\d+)(?:_|\.wav$)", re.IGNORECASE)
WRIST_DIRECTORY = re.compile(r"^robowrist_(.+)_(left|right)$", re.IGNORECASE)
INTEGRITY_VERSION = 4


def is_link(path: Path):
    return path.is_symlink() or bool(
        getattr(path.lstat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def matches_segment(path: Path, segment: str | None):
    match = STREAM_SEGMENT.match(path.name)
    return not segment or not match or match[1].lower() == segment.lower()


def source_files(session: Path):
    """Return source paths and unreadable entries; never follow directory links."""
    files, errors = [], []
    if not session.is_dir():
        return [], [(session, "Session directory does not exist or is inaccessible")]
    for directory, dirs, names in os.walk(
        session,
        followlinks=False,
        onerror=lambda exc: errors.append((Path(exc.filename), str(exc))),
    ):
        current = Path(directory)
        kept = []
        for name in sorted(dirs):
            path = current / name
            if name.casefold() in SKIP_DIRS:
                continue
            try:
                if is_link(path):
                    errors.append((path, "Linked directory not followed"))
                else:
                    kept.append(name)
            except OSError as exc:
                errors.append((path, str(exc)))
        dirs[:] = kept
        for name in sorted(names):
            path = current / name
            if name.lower().endswith(("-wal", "-shm", "-journal")):
                continue
            if name.lower() in {"timestamp_anomaly_detail_table.html", "manifest.tsv"}:
                continue  # generated report, not a capture payload
            try:
                if is_link(path):
                    errors.append((path, "Linked file not followed"))
                else:
                    files.append(path)
            except OSError as exc:
                errors.append((path, str(exc)))
    return files, errors


def discover_sessions(roots: list[Path], output: Path, log=print):
    sessions, errors, seen = [], [], set()
    for specified in roots:
        root = specified.expanduser().resolve()
        if not root.is_dir():
            errors.append({"path": str(root), "error": "root_unavailable"})
            continue
        log(f"SCAN ROOT: {root}")
        for directory, dirs, names in os.walk(
            root,
            followlinks=False,
            onerror=lambda exc: errors.append({"path": str(exc.filename), "error": str(exc)}),
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
                    and not is_link(current / name)
                    and not (current / name).resolve().is_relative_to(output)
                )
            except OSError as exc:
                errors.append({"path": str(current), "error": str(exc)})
                dirs[:] = []
                continue
            if not WRIST_DIRECTORY.fullmatch(current.name) and (
                SESSION_NAME.fullmatch(current.name)
                or any(STREAM_SEGMENT.match(name) for name in names)
                or any(WRIST_DIRECTORY.fullmatch(name) for name in dirs)
            ):
                sessions.append(current)
                dirs[:] = []
            if len(seen) % 500 == 0:
                log(f"Scanned {len(seen)} directories; found {len(sessions)} Sessions")
    return sorted(set(sessions)), errors


def audit_session(
    session: Path,
    *,
    segment=None,
    layout="raw",
    ffmpeg=None,
    ffprobe=None,
    video_log=None,
    progress=print,
    requirements_path=None,
):
    if layout not in {"raw", "published"}:
        raise ValueError("layout must be raw or published")
    if segment is not None and not SEGMENT_NAME.fullmatch(segment):
        raise ValueError("segment must be segment followed by digits")
    session = session.resolve()
    signature = source_signature(session, segment=segment, requirements_path=requirements_path)
    paths, errors = source_files(session)
    paths = [path for path in paths if matches_segment(path, segment)]
    rows = []

    def add(path, category, checked, requirement=""):
        text = str(path)
        if isinstance(path, Path) and path.is_relative_to(session):
            text = path.relative_to(session).as_posix() or "."
        detail = str(checked.get("detail", "")).replace(str(session), ".")
        detail = detail.replace(session.as_posix(), ".")
        rows.append(
            {
                **checked,
                "detail": detail,
                "path": text,
                "category": category,
                "requirement": requirement,
                "severity": "pass"
                if checked["state"] == "ok"
                else ("warning" if checked["state"] in WARNING_STATES else "error"),
            }
        )

    for path, detail in errors:
        add(path, "structure", result("read_error", detail))
    relative = {path.relative_to(session).as_posix(): path for path in paths}
    segments = {m[1].lower() for path in paths if (m := STREAM_SEGMENT.match(path.name))}
    if segment:
        segments.add(segment.lower())
    if not segments:
        add("robocap_segment<N>_*", "structure", result("missing", "No recording Segment detected"))
    for name in sorted(segments, key=lambda value: int(value[7:])):
        required = [(f"robocap_{name}_video_{camera}.mp4", "头戴视频") for camera in CAMERAS]
        required += [(f"robocap_{name}_imu_{side}.db", "头戴 IMU") for side in ("left", "right")]
        required += [(f"robocap_{name}_mag_middle.db", "头戴 MAG")]
        required += [(f"audio_{name}.wav", "WAV 音频")]
        for filename, role in required:
            matches = [p for rel, p in relative.items() if rel.lower() == filename]
            add(filename, "structure", result("ok" if matches else "missing"), role)
        for side in ("left", "right"):
            for suffix, role in (
                (f"video_{side}_down.mp4", "腕部视频"),
                (f"imu_{side}.db", "腕部 IMU"),
                (f"mag_{side}.db", "腕部 MAG"),
            ):
                filename = f"robowrist_{name}_{suffix}"
                matches = [
                    p
                    for rel, p in relative.items()
                    if len(PurePosixPath(rel).parts) == 2
                    and re.fullmatch(rf"robowrist_.+_{side}", p.parent.name, re.IGNORECASE)
                    and p.name.lower() == filename
                ]
                state = "missing" if not matches else "ambiguous" if len(matches) > 1 else "ok"
                add(
                    f"robowrist_*_{side}/{filename}",
                    "structure",
                    result(state, "; ".join(p.relative_to(session).as_posix() for p in matches)),
                    role,
                )
            devices = {
                p.parent.name.lower()
                for p in paths
                if p.parent.parent == session
                and WRIST_DIRECTORY.fullmatch(p.parent.name)
                and p.parent.name.lower().endswith("_" + side)
                and (m := STREAM_SEGMENT.match(p.name))
                and m[1].lower() == name
            }
            if len(devices) > 1:
                add(
                    f"{name}/{side}",
                    "structure",
                    result(
                        "ambiguous",
                        "Multiple wrist devices in this Segment: " + "; ".join(sorted(devices)),
                    ),
                )
    try:
        mocap_dirs = [
            p
            for p in session.iterdir()
            if p.is_dir() and is_mocap_directory_name(p.name) and not is_link(p)
        ]
    except OSError:
        mocap_dirs = []
    add(
        "mocap*/",
        "structure",
        result(
            "ok" if len(mocap_dirs) == 1 else "missing" if not mocap_dirs else "ambiguous",
            "; ".join(p.name for p in mocap_dirs),
        ),
        "动捕目录",
    )
    mocap_files = [p for p in paths if any(p.is_relative_to(d) for d in mocap_dirs)]
    check_mocap_exports(mocap_dirs, mocap_files, add, requirements_path=requirements_path)
    count = sum(p.suffix.lower() in VIDEO_SUFFIXES for p in mocap_files)
    add(
        "mocap*/*.{mp4,mov,avi,mkv}", "structure",
        result("ok" if count else "missing", f"{count} files"), "第三人称视频",
    )
    for index, path in enumerate(paths, 1):
        progress(f"  [{index}/{len(paths)}] content: {path.relative_to(session)}")
        checked = inspect_file(path, ffmpeg=ffmpeg, ffprobe=ffprobe, video_log=video_log)
        add(path, "content", checked)
        progress(f"    {checked['status']}: {checked.get('detail', '')}")
    manifest = next(
        (p for p in paths if p.parent == session and p.name.lower() == "manifest.json"), None
    )
    if layout == "published":
        for name, label in (
            ("manifest.json", "发布清单"),
            ("timestamp_anomaly_detail_table.html", "已发布检查报告"),
        ):
            add(name, "structure", result("ok" if (session / name).is_file() else "missing"), label)
        report_path = session / "timestamp_anomaly_detail_table.html"
        if report_path.is_file():
            try:
                if not report_path.stat().st_size:
                    checked = result("zero_byte")
                else:
                    document = report_path.read_text(encoding="utf-8-sig")
                    raw = document.split("const report=", 1)[1].split("; const eventTypes=", 1)[0]
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or payload.get("session") != session.name:
                        checked = result("manifest_mismatch", "Report Session identity mismatch")
                    else:
                        checked = result()
                add(report_path, "content", checked, "已发布检查报告")
            except (OSError, ValueError, IndexError) as exc:
                add(report_path, "content", result("invalid_format", str(exc)), "已发布检查报告")
    if manifest is not None:
        _check_manifest(session, manifest, add, segment)
    if signature != source_signature(session, segment=segment, requirements_path=requirements_path):
        add(".", "structure", result("changed", "Source files changed during inspection; rerun"))
    counts = Counter(row["severity"] for row in rows)
    status = "failed" if counts["error"] else "incomplete" if counts["warning"] else "passed"
    return {
        "version": INTEGRITY_VERSION,
        "layout": layout,
        "status": status,
        "fileCount": len(paths),
        "errors": counts["error"],
        "warnings": counts["warning"],
        "checks": rows,
        "segments": sorted(segments),
        "source_signature": signature,
        "mocap_rigid_names": mocap_names(mocap_files),
    }


def source_signature(session: Path, *, segment=None, requirements_path=None):
    """Invalidate old verdicts when files or the action requirements change."""
    session = session.resolve()
    paths, errors = source_files(session)
    inventory = []
    for path in paths:
        if not matches_segment(path, segment):
            continue
        try:
            info = path.stat()
            inventory.append((path.relative_to(session).as_posix(), info.st_size, info.st_mtime_ns))
        except OSError as exc:
            errors.append((path, str(exc)))
    rules = Path(requirements_path or DEFAULT_REQUIREMENTS)
    try:
        rules_hash = hashlib.sha256(rules.read_bytes()).hexdigest()
    except OSError as exc:
        rules_hash = type(exc).__name__
    document = json.dumps(
        {"files": sorted(inventory), "errors": [(str(p), e) for p, e in errors],
         "requirements": rules_hash}, ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def save_integrity(session: Path, integrity: dict, segment=None):
    if segment is not None and not SEGMENT_NAME.fullmatch(segment):
        raise ValueError("Invalid integrity segment")
    path = session / "_artifacts" / f"integrity-{segment or 'all'}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(integrity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def saved_integrity(session: Path, segment=None, *, requirements_path=None):
    """Read an integrity verdict without treating stale reports as current passes."""
    path = session / "_artifacts" / f"integrity-{segment or 'all'}.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(report, dict) or report.get("version") != INTEGRITY_VERSION
                or not isinstance(report.get("checks"), list)
                or report.get("status") not in {"passed", "failed", "incomplete"}
                or any(not isinstance(row, dict) or not {"path", "state", "severity"} <= row.keys()
                       for row in report["checks"])):
            return None
        if report.get("source_signature") != source_signature(
            session, segment=segment, requirements_path=requirements_path,
        ):
            return {**report, "status": "stale"}
        return report
    except (OSError, ValueError):
        return None


def integrity_summary(session: Path):
    report = saved_integrity(session)
    if report is None:
        reports = [
            saved_integrity(session, path.stem.removeprefix("integrity-"))
            for path in (session / "_artifacts").glob("integrity-segment*.json")
        ]
        reports = [item for item in reports if item is not None]
        if reports:
            # Partial Segment coverage must not become a whole-Session pass.
            state = ("stale" if any(p["status"] == "stale" for p in reports) else
                     "failed" if any(p["status"] == "failed" for p in reports) else "partial")
            report = {"status": state, "checks": [r for p in reports for r in p["checks"]]}
    if report is None:
        return "unchecked", ()
    issues = tuple(dict.fromkeys(
        f"{row['path']}: {row.get('status', row['state'])} {row.get('detail', '')}".strip()
        for row in report["checks"] if row.get("severity") != "pass"
    ))
    return report["status"], issues


def _check_manifest(session, manifest, add, segment):
    try:
        if manifest.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Manifest exceeds 4 MiB")
        value = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict) or not isinstance(value.get("files"), list):
            raise TypeError("Manifest must contain a files list")
        if not value["files"]:
            raise ValueError("Manifest files list is empty")
        if value.get("session_id") != session.name:
            add(
                manifest,
                "manifest",
                result("manifest_mismatch", "session_id does not match directory"),
            )
        seen = set()
        for record in value["files"]:
            name = record.get("packaged_as") if isinstance(record, dict) else None
            if not isinstance(name, str):
                raise TypeError("Invalid manifest packaged_as")
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                raise ValueError("Unsafe manifest path")
            if name in seen:
                add(name, "manifest", result("manifest_mismatch", "Duplicate manifest path"))
                continue
            seen.add(name)
            path = session.joinpath(*relative.parts)
            if not path.resolve().is_relative_to(session):
                raise ValueError("Manifest path leaves Session directory")
            if not matches_segment(path, segment):
                continue
            expected = record.get("packaged_bytes")
            if not path.is_file():
                add(name, "manifest", result("missing", "Listed in manifest"))
            elif isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                add(name, "manifest", result("manifest_mismatch", "Missing/invalid packaged_bytes"))
            elif path.stat().st_size != expected:
                add(
                    name,
                    "manifest",
                    result(
                        "manifest_mismatch",
                        f"bytes: expected {expected}, actual {path.stat().st_size}",
                    ),
                )
    except (OSError, ValueError, TypeError) as exc:
        add(manifest, "manifest", result("invalid_format", str(exc)))
