from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key

from .data_packager import PackagedFile, copy_or_compress_file, discover_package_files, is_video

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_ENDPOINT = "https://modelscope.cn"
TOKEN_KEY = "MODELSCOPE_API_TOKEN"
ENDPOINT_KEY = "MODELSCOPE_ENDPOINT"
REPORT_NAME = "timestamp_anomaly_detail_table.html"
METADATA_NAME = "metadata.jsonl"
DATASET_README_NAME = "README.md"
PRIMITIVE_ID_PATTERN = re.compile(r"P\d{2}\Z")
REPO_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
DEVICE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
ROBOWRIST_DIR_PATTERN = re.compile(
    r"robowrist_(?P<device_id>.+)_(?P<side>left|right)\Z", re.IGNORECASE
)


class ModelScopePublisherError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelScopeSettings:
    token: str | None
    endpoint: str
    env_path: Path
    token_source: str


@dataclass(frozen=True)
class StageResult:
    dataset_root: Path
    session_dir: Path
    primitive_id: str
    session_id: str
    manifest_path: Path
    metadata_path: Path
    readme_path: Path
    inspection_html: Path
    file_count: int
    total_bytes: int
    dry_run: bool = False
    main_device_id: str | None = None
    left_device_id: str | None = None
    right_device_id: str | None = None


@dataclass(frozen=True)
class StagedDataset:
    dataset_root: Path
    metadata_path: Path
    readme_path: Path
    session_paths: tuple[str, ...]


@dataclass(frozen=True)
class UploadResult:
    repo_id: str
    repo_url: str
    revision: str
    username: str
    uploaded_path: str
    session_count: int


def validate_primitive_id(value: str) -> str:
    primitive_id = value.strip().upper()
    if not PRIMITIVE_ID_PATTERN.fullmatch(primitive_id):
        raise ValueError("Primitive ID must use the PXX form, for example P01.")
    return primitive_id


def validate_repo_id(value: str) -> str:
    repo_id = value.strip()
    if not REPO_ID_PATTERN.fullmatch(repo_id):
        raise ValueError("ModelScope repo ID must use owner/name format.")
    return repo_id


def validate_session_id(value: str) -> str:
    session_id = value.strip()
    if not session_id or session_id in {".", ".."} or Path(session_id).name != session_id:
        raise ValueError("Session ID must be one non-empty directory name without path separators.")
    return session_id


def validate_endpoint(value: str | None) -> str:
    endpoint = (value or DEFAULT_ENDPOINT).strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "ModelScope endpoint must be an HTTP(S) origin without credentials or a path."
        )
    return endpoint


def _clean_secret(value: str | None) -> str | None:
    secret = (value or "").strip()
    if not secret:
        return None
    if "\n" in secret or "\r" in secret:
        raise ValueError("ModelScope token must be one line.")
    return secret


def ensure_env_file(env_path: Path = DEFAULT_ENV_PATH) -> Path:
    path = env_path.expanduser().resolve()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Local secrets. Do not commit this file.\n"
            f"{TOKEN_KEY}=\n"
            f"{ENDPOINT_KEY}={DEFAULT_ENDPOINT}\n",
            encoding="utf-8",
            newline="\n",
        )
    return path


def load_modelscope_settings(
    env_path: Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> ModelScopeSettings:
    path = env_path.expanduser().resolve()
    file_values = dotenv_values(path, encoding="utf-8", interpolate=False) if path.is_file() else {}
    process_values = os.environ if environ is None else environ
    process_token = _clean_secret(process_values.get(TOKEN_KEY))
    file_token = _clean_secret(file_values.get(TOKEN_KEY))
    token = process_token or file_token
    endpoint = validate_endpoint(
        process_values.get(ENDPOINT_KEY) or file_values.get(ENDPOINT_KEY) or DEFAULT_ENDPOINT
    )
    token_source = (
        ".env"
        if file_token and process_token == file_token
        else "process environment"
        if process_token
        else ".env"
        if file_token
        else "missing"
    )
    return ModelScopeSettings(token, endpoint, path, token_source)


def save_modelscope_settings(
    token: str | None,
    endpoint: str | None,
    env_path: Path = DEFAULT_ENV_PATH,
) -> ModelScopeSettings:
    path = ensure_env_file(env_path)
    clean_token = _clean_secret(token)
    clean_endpoint = validate_endpoint(endpoint)
    if clean_token is not None:
        set_key(str(path), TOKEN_KEY, clean_token, quote_mode="always", encoding="utf-8")
        os.environ[TOKEN_KEY] = clean_token
    set_key(str(path), ENDPOINT_KEY, clean_endpoint, quote_mode="always", encoding="utf-8")
    os.environ[ENDPOINT_KEY] = clean_endpoint
    return load_modelscope_settings(path)


def clear_modelscope_token(env_path: Path = DEFAULT_ENV_PATH) -> ModelScopeSettings:
    path = ensure_env_file(env_path)
    set_key(str(path), TOKEN_KEY, "", quote_mode="always", encoding="utf-8")
    os.environ.pop(TOKEN_KEY, None)
    return load_modelscope_settings(path)


def token_status(settings: ModelScopeSettings) -> str:
    state = "configured" if settings.token else "not configured"
    return (
        f"ModelScope token: {state}; source: {settings.token_source}; "
        f"endpoint: {settings.endpoint}; env: {settings.env_path}"
    )


def _clean_device_id(value: object) -> str | None:
    device_id = str(value or "").strip()
    if not device_id or device_id.upper() == "NONE":
        return None
    return device_id if DEVICE_ID_PATTERN.fullmatch(device_id) else None


def _ffprobe_format_tags(path: Path, ffprobe: str) -> dict[str, object]:
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format_tags",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return {}
    format_data = payload.get("format", {}) if isinstance(payload, dict) else {}
    tags = format_data.get("tags", {}) if isinstance(format_data, dict) else {}
    if not isinstance(tags, dict):
        return {}
    return {str(key).lower(): value for key, value in tags.items()}


def _local_device_id_index(
    session_dir: Path,
) -> tuple[str | None, str | None, str | None]:
    path = session_dir / "raw_calibration" / "device_ids.json"
    if not path.is_file():
        return None, None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelScopePublisherError(f"Invalid local device ID index: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ModelScopePublisherError(f"Invalid local device ID index: {path}")
    return (
        _clean_device_id(payload.get("main_device_id")),
        _clean_device_id(payload.get("left_device_id")),
        _clean_device_id(payload.get("right_device_id")),
    )


def _merge_device_id(current: str | None, candidate: str | None, role: str) -> str | None:
    if current and candidate and current != candidate:
        raise ModelScopePublisherError(
            f"Conflicting {role} device IDs: {current} != {candidate}"
        )
    return current or candidate


def discover_device_ids(session_dir: Path, ffprobe: str = "ffprobe") -> dict[str, object]:
    main_device_id: str | None = None
    left_device_id: str | None = None
    right_device_id: str | None = None

    main_videos = sorted(session_dir.glob("robocap_*_video_*.mp4"))
    for video in main_videos:
        tags = _ffprobe_format_tags(video, ffprobe)
        main_device_id = _clean_device_id(tags.get("deviceid"))
        if main_device_id:
            break

    indexed_main, indexed_left, indexed_right = _local_device_id_index(session_dir)
    main_device_id = _merge_device_id(main_device_id, indexed_main, "main")
    left_device_id = _merge_device_id(left_device_id, indexed_left, "left")
    right_device_id = _merge_device_id(right_device_id, indexed_right, "right")

    for child in sorted(session_dir.iterdir()):
        match = ROBOWRIST_DIR_PATTERN.fullmatch(child.name) if child.is_dir() else None
        if match and (device_id := _clean_device_id(match.group("device_id"))):
            side = match.group("side").lower()
            if side == "left":
                left_device_id = _merge_device_id(left_device_id, device_id, "left")
            else:
                right_device_id = _merge_device_id(right_device_id, device_id, "right")

    for video in sorted(session_dir.rglob("robowrist_*video*.mp4")):
        tags = _ffprobe_format_tags(video, ffprobe)
        device_id = _clean_device_id(tags.get("deviceid"))
        position = str(tags.get("position") or "").strip().lower()
        main_device_id = _merge_device_id(
            main_device_id, _clean_device_id(tags.get("host")), "main"
        )
        if position == "left":
            left_device_id = _merge_device_id(left_device_id, device_id, "left")
        elif position == "right":
            right_device_id = _merge_device_id(right_device_id, device_id, "right")

    return {
        "main": main_device_id,
        "left": left_device_id,
        "right": right_device_id,
    }


def default_dataset_root(session_dir: Path) -> Path:
    return session_dir.resolve().parent / "_modelscope_dataset"


def find_inspection_report(session_dir: Path, segment: str | None) -> Path | None:
    if segment:
        exact = session_dir / "_artifacts" / segment / "inspection" / REPORT_NAME
        if exact.is_file():
            return exact
    candidates = [path for path in session_dir.rglob(REPORT_NAME) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def find_rerun_files(session_dir: Path, segment: str | None) -> list[Path]:
    artifacts = session_dir / "_artifacts"
    search_root = artifacts / segment if segment else artifacts
    if not search_root.is_dir():
        return []
    return sorted(path for path in search_root.rglob("*.rrd") if path.is_file())


def copy_rerun_file(source: Path, session_dir: Path, target_dir: Path) -> PackagedFile:
    artifacts = session_dir / "_artifacts"
    relative_source = source.relative_to(session_dir)
    relative_target = Path("rerun") / source.relative_to(artifacts)
    target = target_dir / relative_target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    size = source.stat().st_size
    return PackagedFile(
        source=relative_source.as_posix(),
        packaged_as=relative_target.as_posix(),
        kind="rerun",
        original_bytes=size,
        packaged_bytes=size,
        compressed_video=False,
    )


def _portable_inspection_document(document: str, session_id: str) -> str:
    prefix = "const report="
    suffix = "; const eventTypes="
    start = document.find(prefix)
    if start < 0:
        raise ModelScopePublisherError(
            "Inspection HTML does not contain the embedded report payload."
        )
    payload_start = start + len(prefix)
    payload_end = document.find(suffix, payload_start)
    if payload_end < 0:
        raise ModelScopePublisherError("Inspection HTML report payload has an unknown format.")
    try:
        payload = json.loads(document[payload_start:payload_end])
    except json.JSONDecodeError as exc:
        raise ModelScopePublisherError(f"Inspection HTML report payload is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise ModelScopePublisherError("Inspection HTML report payload must be a JSON object.")
    payload["sessionPath"] = session_id
    portable_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    return document[:payload_start] + portable_payload + document[payload_end:]


def copy_portable_inspection_report(source: Path, target: Path, session_id: str) -> None:
    document = source.read_text(encoding="utf-8")
    portable = _portable_inspection_document(document, session_id)
    target.write_text(portable, encoding="utf-8", newline="")


def _dataset_readme() -> str:
    return """---
configs:
- config_name: default
  data_files:
  - split: train
    path: metadata.jsonl
---

# EgoMocap Dataset

Each row in `metadata.jsonl` describes one recording. Session files use the stable
`PXX/<session_id>/` hierarchy, where `PXX` is an action primitive ID.

Every session directory includes a self-contained `timestamp_anomaly_detail_table.html`
inspection report. Paths inside manifests and reports are dataset-relative.

## Session contents

- Required capture streams: six `robocap_<segment>_video_*.mp4` first-person cameras, Robocap
  IMU/MAG databases, third-person videos, and Robowrist left/right video and sensor streams.
- Required motion-capture content: all available NOKOV body and rigid-body data under `nokov/`.
  The concrete BVH, CSV, TRC, XRS, C3D, or other export formats are optional choices, but at
  least one motion-capture format must be present.
- Optional artifact: RRD files, only when explicitly selected.
- Generated: `manifest.json` and `timestamp_anomaly_detail_table.html`.

Calibration files live outside the session dataset. `manifest.json` and `metadata.jsonl` contain
only explicit `main`, `left`, and `right` device IDs needed to resolve those records.
"""


def _write_dataset_readme(dataset_root: Path) -> Path:
    readme_path = dataset_root / DATASET_README_NAME
    if not readme_path.exists():
        readme_path.write_text(_dataset_readme(), encoding="utf-8", newline="\n")
    return readme_path


def _read_metadata(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    entries: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ModelScopePublisherError(
                f"Invalid {METADATA_NAME} line {line_number}: {exc}"
            ) from exc
        if not isinstance(entry, dict):
            raise ModelScopePublisherError(
                f"Invalid {METADATA_NAME} line {line_number}: expected a JSON object."
            )
        entries.append(entry)
    return entries


def _update_metadata(dataset_root: Path, entry: dict[str, object]) -> Path:
    metadata_path = dataset_root / METADATA_NAME
    entries = _read_metadata(metadata_path)
    key = (entry["primitive_id"], entry["session_id"])
    by_key: dict[tuple[object, object], dict[str, object]] = {}
    for item in entries:
        item_key = (item.get("primitive_id"), item.get("session_id"))
        if not all(item_key):
            raise ModelScopePublisherError(
                f"Existing {METADATA_NAME} rows must include primitive_id and session_id."
            )
        by_key[item_key] = item
    by_key[key] = entry
    ordered = sorted(
        by_key.values(),
        key=lambda item: (str(item.get("primitive_id", "")), str(item.get("session_id", ""))),
    )
    text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in ordered)
    temporary = metadata_path.with_name(f".{metadata_path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(metadata_path)
    return metadata_path


def _validate_stage_locations(session_dir: Path, dataset_root: Path, target_dir: Path) -> None:
    if dataset_root.is_relative_to(session_dir):
        raise ValueError("Dataset root cannot be inside the source session directory.")
    if target_dir == session_dir:
        raise ValueError("Dataset target cannot be the source session directory.")


def stage_session(
    session_dir: Path,
    primitive_id: str,
    *,
    dataset_root: Path | None = None,
    session_id: str | None = None,
    segment: str | None = None,
    raw_video: bool = False,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    proxy_height: int = 540,
    proxy_crf: int = 28,
    proxy_bitrate: str = "1400k",
    include_rrd: bool = False,
    inspection_report: Path | None = None,
    dry_run: bool = False,
    progress: Callable[[str], None] | None = print,
) -> StageResult:
    source = session_dir.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    primitive = validate_primitive_id(primitive_id)
    resolved_session_id = validate_session_id(session_id or source.name)
    root = (dataset_root or default_dataset_root(source)).expanduser().resolve()
    target = root / primitive / resolved_session_id
    _validate_stage_locations(source, root, target)
    device_ids = discover_device_ids(source, ffprobe)

    report_source = inspection_report or find_inspection_report(source, segment)
    if report_source is None or not report_source.is_file():
        raise FileNotFoundError(
            f"Required inspection report not found: run inspect for {source} before staging."
        )
    files = discover_package_files(source, segment, include_artifacts=False, include_rrd=False)
    rerun_files = find_rerun_files(source, segment) if include_rrd else []
    if not files:
        raise ModelScopePublisherError(f"No session files were discovered in {source}.")

    manifest_path = target / "manifest.json"
    metadata_path = root / METADATA_NAME
    readme_path = root / DATASET_README_NAME
    inspection_target = target / REPORT_NAME
    if dry_run:
        return StageResult(
            dataset_root=root,
            session_dir=target,
            primitive_id=primitive,
            session_id=resolved_session_id,
            manifest_path=manifest_path,
            metadata_path=metadata_path,
            readme_path=readme_path,
            inspection_html=inspection_target,
            file_count=len(files) + len(rerun_files) + 1,
            total_bytes=sum(path.stat().st_size for path in files)
            + sum(path.stat().st_size for path in rerun_files)
            + report_source.stat().st_size,
            dry_run=True,
            main_device_id=device_ids["main"],
            left_device_id=device_ids["left"],
            right_device_id=device_ids["right"],
        )

    target.mkdir(parents=True, exist_ok=True)
    packaged = []
    total_source_files = len(files) + len(rerun_files)
    for index, path in enumerate(files, start=1):
        if progress is not None:
            operation = "compress" if is_video(path) and not raw_video else "copy"
            progress(f"[{index}/{total_source_files}] {operation} {path.relative_to(source)}")
        packaged.append(
            copy_or_compress_file(
                path,
                source,
                target,
                raw_video,
                ffmpeg,
                proxy_height,
                proxy_crf,
                proxy_bitrate,
            )
        )
    for index, path in enumerate(rerun_files, start=len(files) + 1):
        if progress is not None:
            progress(f"[{index}/{total_source_files}] copy {path.relative_to(source)}")
        packaged.append(copy_rerun_file(path, source, target))

    copy_portable_inspection_report(report_source, inspection_target, resolved_session_id)
    report_record = {
        "source": report_source.relative_to(source).as_posix(),
        "packaged_as": REPORT_NAME,
        "kind": "inspection_html",
        "original_bytes": report_source.stat().st_size,
        "packaged_bytes": inspection_target.stat().st_size,
        "compressed_video": False,
    }
    file_records = [asdict(item) for item in packaged] + [report_record]
    total_bytes = sum(int(item["packaged_bytes"]) for item in file_records)
    manifest = {
        "schema_version": 1,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "primitive_id": primitive,
        "session_id": resolved_session_id,
        "segment": segment or "auto/all",
        "device_ids": device_ids,
        "options": {
            "raw_video": raw_video,
            "proxy_height": proxy_height,
            "proxy_crf": proxy_crf,
            "proxy_bitrate": proxy_bitrate,
            "include_rrd": include_rrd,
        },
        "files": file_records,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    relative_session = f"{primitive}/{resolved_session_id}"
    metadata_path = _update_metadata(
        root,
        {
            "primitive_id": primitive,
            "session_id": resolved_session_id,
            "session_path": relative_session,
            "manifest": f"{relative_session}/manifest.json",
            "inspection_html": f"{relative_session}/{REPORT_NAME}",
            "segment": segment or "auto/all",
            "device_ids": device_ids,
            "file_count": len(file_records),
            "packaged_bytes": total_bytes,
        },
    )
    readme_path = _write_dataset_readme(root)
    return StageResult(
        dataset_root=root,
        session_dir=target,
        primitive_id=primitive,
        session_id=resolved_session_id,
        manifest_path=manifest_path,
        metadata_path=metadata_path,
        readme_path=readme_path,
        inspection_html=inspection_target,
        file_count=len(file_records),
        total_bytes=total_bytes,
        main_device_id=device_ids["main"],
        left_device_id=device_ids["left"],
        right_device_id=device_ids["right"],
    )


def load_staged_session(dataset_root: Path, primitive_id: str, session_id: str) -> StageResult:
    root = dataset_root.expanduser().resolve()
    primitive = validate_primitive_id(primitive_id)
    resolved_session_id = validate_session_id(session_id)
    target = root / primitive / resolved_session_id
    manifest_path = target / "manifest.json"
    metadata_path = root / METADATA_NAME
    readme_path = root / DATASET_README_NAME
    inspection_html = target / REPORT_NAME
    required = [manifest_path, metadata_path, readme_path, inspection_html]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Staged dataset files are missing: " + ", ".join(missing))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("primitive_id") != primitive
        or manifest.get("session_id") != resolved_session_id
    ):
        raise ModelScopePublisherError(
            "Staged manifest identity does not match the requested session."
        )
    files = manifest.get("files") or []
    device_ids = manifest.get("device_ids") or {}
    if not isinstance(device_ids, dict):
        raise ModelScopePublisherError("Staged manifest device_ids must be a JSON object.")
    total_bytes = sum(
        int(item.get("packaged_bytes", 0)) for item in files if isinstance(item, dict)
    )
    return StageResult(
        dataset_root=root,
        session_dir=target,
        primitive_id=primitive,
        session_id=resolved_session_id,
        manifest_path=manifest_path,
        metadata_path=metadata_path,
        readme_path=readme_path,
        inspection_html=inspection_html,
        file_count=len(files),
        total_bytes=total_bytes,
        main_device_id=_clean_device_id(device_ids.get("main")),
        left_device_id=_clean_device_id(device_ids.get("left")),
        right_device_id=_clean_device_id(device_ids.get("right")),
    )


def load_staged_dataset(dataset_root: Path) -> StagedDataset:
    root = dataset_root.expanduser().resolve()
    metadata_path = root / METADATA_NAME
    readme_path = root / DATASET_README_NAME
    if not metadata_path.is_file() or not readme_path.is_file():
        raise FileNotFoundError(f"Staged dataset requires {metadata_path} and {readme_path}.")
    entries = _read_metadata(metadata_path)
    if not entries:
        raise ModelScopePublisherError(f"{metadata_path} has no staged sessions.")
    session_paths: list[str] = []
    missing: list[str] = []
    for entry in entries:
        primitive = validate_primitive_id(str(entry.get("primitive_id", "")))
        session_id = validate_session_id(str(entry.get("session_id", "")))
        expected_path = f"{primitive}/{session_id}"
        if entry.get("session_path") != expected_path:
            raise ModelScopePublisherError(
                f"Metadata path for {primitive}/{session_id} must be {expected_path}."
            )
        session_dir = root / primitive / session_id
        for required in (session_dir / "manifest.json", session_dir / REPORT_NAME):
            if not required.is_file():
                missing.append(str(required))
        session_paths.append(expected_path)
    if missing:
        raise FileNotFoundError("Staged dataset files are missing: " + ", ".join(missing))
    return StagedDataset(root, metadata_path, readme_path, tuple(sorted(session_paths)))


def _hub_api(settings: ModelScopeSettings):
    try:
        from modelscope_hub import HubApi
    except ImportError as exc:
        raise ModelScopePublisherError(
            "ModelScope publishing requires modelscope-hub. Run uv sync --extra web."
        ) from exc
    return HubApi(token=settings.token, endpoint=settings.endpoint)


def _username(user: object) -> str:
    for name in ("username", "name", "user_name"):
        value = getattr(user, name, None)
        if value:
            return str(value)
    if isinstance(user, Mapping):
        for name in ("username", "name", "user_name"):
            value = user.get(name)
            if value:
                return str(value)
    return "authenticated user"


def _redacted_error(exc: Exception, token: str | None) -> str:
    message = str(exc)
    return message.replace(token, "<redacted>") if token else message


def verify_modelscope_auth(settings: ModelScopeSettings | None = None) -> str:
    resolved = settings or load_modelscope_settings()
    if not resolved.token:
        raise ModelScopePublisherError(f"{TOKEN_KEY} is not configured in {resolved.env_path}.")
    try:
        return _username(_hub_api(resolved).whoami())
    except Exception as exc:
        raise ModelScopePublisherError(
            f"ModelScope authentication failed: {_redacted_error(exc, resolved.token)}"
        ) from exc


def upload_staged_dataset(
    staged: StagedDataset,
    repo_id: str,
    *,
    revision: str = "master",
    create_if_missing: bool = False,
    visibility: str = "private",
    license_name: str | None = None,
    commit_message: str | None = None,
    max_workers: int | None = None,
    use_cache: bool = True,
    settings: ModelScopeSettings | None = None,
) -> UploadResult:
    resolved = settings or load_modelscope_settings()
    if not resolved.token:
        raise ModelScopePublisherError(f"{TOKEN_KEY} is not configured in {resolved.env_path}.")
    repository = validate_repo_id(repo_id)
    target_revision = revision.strip() or "master"
    if visibility not in {"private", "internal", "public"}:
        raise ValueError("Visibility must be private, internal, or public.")
    if max_workers is not None and max_workers < 1:
        raise ValueError("max_workers must be positive.")
    try:
        api = _hub_api(resolved)
        username = _username(api.whoami())
        exists = api.repo_exists(repository, "dataset")
        if not exists:
            if not create_if_missing:
                raise ModelScopePublisherError(
                    f"Dataset repository does not exist: {repository}. "
                    "Create it first or enable create-if-missing."
                )
            api.create_repo(
                repository,
                "dataset",
                visibility=visibility,
                license=license_name or None,
            )
        allow_patterns = [f"{path}/**" for path in staged.session_paths]
        allow_patterns.extend([METADATA_NAME, DATASET_README_NAME])
        api.upload_folder(
            repository,
            "dataset",
            staged.dataset_root,
            path_in_repo="",
            revision=target_revision,
            commit_message=commit_message
            or f"Upload {len(staged.session_paths)} prepared session(s)",
            allow_patterns=allow_patterns,
            max_workers=max_workers,
            use_cache=use_cache,
            disable_tqdm=True,
        )
    except ModelScopePublisherError:
        raise
    except Exception as exc:
        raise ModelScopePublisherError(
            f"ModelScope upload failed: {_redacted_error(exc, resolved.token)}"
        ) from exc
    repo_url = f"{resolved.endpoint.rstrip('/')}/datasets/{repository}"
    return UploadResult(
        repository,
        repo_url,
        target_revision,
        username,
        str(staged.dataset_root),
        len(staged.session_paths),
    )


def upload_staged_session(
    staged: StageResult,
    repo_id: str,
    **kwargs: Any,
) -> UploadResult:
    dataset = load_staged_dataset(staged.dataset_root)
    selected = f"{staged.primitive_id}/{staged.session_id}"
    if selected not in dataset.session_paths:
        raise ModelScopePublisherError(
            f"Selected staged session is missing from metadata: {selected}"
        )
    return upload_staged_dataset(dataset, repo_id, **kwargs)
