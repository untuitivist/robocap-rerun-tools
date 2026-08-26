from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key

from .data_packager import PackagedFile, copy_or_compress_file, discover_package_files, is_video
from .dataset_intersection import (
    AlignedIntersectionPlan,
    DatasetIntersectionError,
    build_aligned_intersection_plan,
    stage_aligned_file,
)
from .session_layout import (
    CANONICAL_MOCAP_DIR_NAME,
    canonical_mocap_relative_path,
    discover_mocap_directories,
    is_mocap_directory_name,
    is_path_under_mocap,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_ENDPOINT = "https://modelscope.cn"
TOKEN_KEY = "MODELSCOPE_API_TOKEN"
ENDPOINT_KEY = "MODELSCOPE_ENDPOINT"
REPO_ID_KEY = "MODELSCOPE_REPO_ID"
REPORT_NAME = "timestamp_anomaly_detail_table.html"
METADATA_NAME = "metadata.jsonl"
DATASET_README_NAME = "README.md"
ACTIONS_DIR_NAME = "EgoMotionActions"
CALIBRATION_DIR_NAME = "raw_calibration"
MOCAP_DIR_NAME = CANONICAL_MOCAP_DIR_NAME
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
    repo_id: str | None = None


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
    alignment: dict[str, object] | None = None


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


def require_mocap_directory(session_dir: Path) -> Path:
    mocap_dirs = discover_mocap_directories(session_dir)
    if not mocap_dirs:
        raise ModelScopePublisherError(
            f"Session motion-capture directory name must start with {MOCAP_DIR_NAME}: {session_dir}"
        )
    if len(mocap_dirs) > 1:
        raise ModelScopePublisherError(
            "Session contains multiple mocap* directories; keep one source directory per Session: "
            f"{list(mocap_dirs)}"
        )
    mocap_dir = mocap_dirs[0]
    if not any(path.is_file() for path in mocap_dir.rglob("*")):
        raise ModelScopePublisherError(f"Session motion-capture directory is empty: {mocap_dir}")
    return mocap_dir


def find_mocap_files(session_dir: Path) -> list[Path]:
    source = session_dir.expanduser().resolve()
    mocap_dir = require_mocap_directory(source)
    mocap_root = mocap_dir.resolve()
    files = discover_package_files(
        source,
        segment=None,
        include_artifacts=False,
        include_rrd=False,
    )
    selectable = [
        path
        for path in files
        if path.is_relative_to(mocap_dir)
        and not path.is_symlink()
        and path.resolve().is_relative_to(mocap_root)
    ]
    if not selectable:
        raise ModelScopePublisherError(
            f"Session mocap* directory contains no packageable files: {mocap_dir}"
        )
    return selectable


def resolve_mocap_files(
    session_dir: Path,
    selected_files: Sequence[str | Path] | None,
) -> list[Path]:
    available = find_mocap_files(session_dir)
    if selected_files is None:
        return available
    if not selected_files:
        raise ValueError("Select at least one Mocap file for ModelScope staging.")

    available_by_path = {path.resolve(): path for path in available}
    resolved: list[Path] = []
    seen: set[Path] = set()
    for value in selected_files:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = session_dir / candidate
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        source = available_by_path.get(candidate)
        if source is None:
            raise ValueError(
                f"Selected Mocap file is not available under the Session mocap* directory: {value}"
            )
        seen.add(candidate)
        resolved.append(source)
    return resolved


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
            f"{ENDPOINT_KEY}={DEFAULT_ENDPOINT}\n"
            f"{REPO_ID_KEY}=\n",
            encoding="utf-8",
            newline="\n",
        )
    else:
        values = dotenv_values(path, encoding="utf-8", interpolate=False)
        defaults = {
            TOKEN_KEY: "",
            ENDPOINT_KEY: DEFAULT_ENDPOINT,
            REPO_ID_KEY: "",
        }
        for key, value in defaults.items():
            if key not in values:
                set_key(str(path), key, value, quote_mode="always", encoding="utf-8")
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
    repo_value = process_values.get(REPO_ID_KEY) or file_values.get(REPO_ID_KEY)
    repo_id = validate_repo_id(str(repo_value)) if repo_value else None
    token_source = (
        ".env"
        if file_token and process_token == file_token
        else "process environment"
        if process_token
        else ".env"
        if file_token
        else "missing"
    )
    return ModelScopeSettings(token, endpoint, path, token_source, repo_id)


def save_modelscope_settings(
    token: str | None,
    endpoint: str | None,
    env_path: Path = DEFAULT_ENV_PATH,
    *,
    repo_id: str | None = None,
) -> ModelScopeSettings:
    path = ensure_env_file(env_path)
    clean_token = _clean_secret(token)
    clean_endpoint = validate_endpoint(endpoint)
    clean_repo_id = validate_repo_id(repo_id) if (repo_id or "").strip() else None
    if clean_token is not None:
        set_key(str(path), TOKEN_KEY, clean_token, quote_mode="always", encoding="utf-8")
        os.environ[TOKEN_KEY] = clean_token
    set_key(str(path), ENDPOINT_KEY, clean_endpoint, quote_mode="always", encoding="utf-8")
    os.environ[ENDPOINT_KEY] = clean_endpoint
    if clean_repo_id is not None:
        set_key(str(path), REPO_ID_KEY, clean_repo_id, quote_mode="always", encoding="utf-8")
        os.environ[REPO_ID_KEY] = clean_repo_id
    return load_modelscope_settings(path)


def clear_modelscope_token(env_path: Path = DEFAULT_ENV_PATH) -> ModelScopeSettings:
    path = ensure_env_file(env_path)
    set_key(str(path), TOKEN_KEY, "", quote_mode="always", encoding="utf-8")
    os.environ.pop(TOKEN_KEY, None)
    return load_modelscope_settings(path)


def token_status(settings: ModelScopeSettings) -> str:
    state = "configured" if settings.token else "not configured"
    repository = settings.repo_id or "not configured"
    return (
        f"ModelScope token: {state}; source: {settings.token_source}; "
        f"endpoint: {settings.endpoint}; repository: {repository}; env: {settings.env_path}"
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


def resolve_rerun_files(
    session_dir: Path,
    segment: str | None,
    *,
    include_all: bool,
    selected_files: Sequence[str | Path] | None,
) -> list[Path]:
    if include_all and selected_files:
        raise ValueError("Use either include_rrd or selected RRD files, not both.")

    available = find_rerun_files(session_dir, segment)
    if include_all:
        return available
    if not selected_files:
        return []

    available_by_path = {path.resolve(): path for path in available}
    resolved: list[Path] = []
    seen: set[Path] = set()
    for value in selected_files:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = session_dir / candidate
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        source = available_by_path.get(candidate)
        if source is None:
            scope = f"segment {segment!r}" if segment else "the session artifacts"
            raise ValueError(f"Selected RRD file is not available under {scope}: {value}")
        seen.add(candidate)
        resolved.append(source)
    return resolved


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


def reset_staged_rerun_directory(target_dir: Path) -> None:
    rerun_dir = target_dir / "rerun"
    if not rerun_dir.exists():
        return
    if rerun_dir.is_symlink() or not rerun_dir.is_dir():
        raise ModelScopePublisherError(f"Staged RRD path is not a regular directory: {rerun_dir}")
    shutil.rmtree(rerun_dir)


def reset_staged_mocap_directories(
    target_dir: Path,
    progress: Callable[[str], None] | None,
) -> None:
    for path in sorted(target_dir.iterdir(), key=lambda item: item.name.casefold()):
        if not is_mocap_directory_name(path.name):
            continue
        if path.is_symlink() or not path.is_dir():
            raise ModelScopePublisherError(
                f"Staged Mocap path is not a regular directory: {path}"
            )
        if progress is not None:
            progress(f"Removing previously staged Mocap directory: {path}")
        shutil.rmtree(path)


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


_DATASET_DOWNLOAD = """## Download

Browse the ModelScope **Dataset Files** page for file metadata and individual data files.

:modelscope-code[]{type="sdk"}

:modelscope-code[]{type="git"}

### Update a local copy

If the dataset was cloned with Git, run the following command inside the cloned repository to
fast-forward it to the latest ModelScope revision:

```bat
git pull --ff-only
```

If Git reports that LFS objects are still missing after the pull, run `git lfs pull`. A directory
downloaded with the ModelScope SDK is not necessarily a Git working tree; update that copy by
running the SDK download command again with the same local directory.

After downloading, read the dataset-wide session index without any additional dependency:

```python
import json
from pathlib import Path

dataset_root = Path("./EgoMotionActions")
records = [
    json.loads(line)
    for line in (dataset_root / "metadata.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
for record in records:
    print(record["primitive_id"], record["session_id"], record["session_path"])
```

"""


_ACTION_TASK_CATALOG = """## Global collection rules

- **Participant behavior:** Move naturally; do not exaggerate gait or arm swing and do not imitate
  a robot.
- **Speed:** Participants should not measure speed. Use the verbal descriptions: slow, normal,
  fast. Keep movement comfortable and controlled.
- **Start:** Unless otherwise specified, begin standing naturally with feet approximately
  hip-width apart.
- **End:** Unless otherwise specified, finish in stable standing and remain still for ~2 seconds.
- **Variation:** For standardized trials, follow the listed geometry. For randomized trials, vary
  target positions, turn directions, path shape, and obstacle placement.

## Action task catalog (P01-P29)

Each recording is assigned to exactly one action primitive. `Episodes / participant` is not yet
specified in the collection definition and remains **TBD** for every primitive.

### Speed & stopping

| ID | Primitive | Participant instruction | Distance / geometry | Approx. duration | Room | Purpose |
|---|---|---|---:|---:|---|---|
| P01 | Normal straight walking | Start still; walk straight at normal comfortable speed; stop at endpoint. | 5 m | 5–7 s | Open | Baseline forward locomotion |
| P02 | Slow walking | Walk forward noticeably slower than normal; do not shuffle; stop. | 5 m | 7–10 s | Open | Slow-speed locomotion |
| P03 | Fast walking | Walk quickly but do not run; remain comfortable and controlled; stop. | 5 m | 4–6 s | Open | Fast walking |
| P04 | Accelerate | Start still; gradually go slow → normal → fast; briefly maintain fast speed; stop. | 5 m | 8–10 s | Open | Natural acceleration |
| P05 | Decelerate | Start at normal speed; gradually slow over several steps; stop gently. | 5 m | 7–10 s | Open | Natural deceleration |
| P06 | Sudden stop | Walk normally; at marked point stop as quickly as comfortably possible; remain still. | 5 m | 5–8 s | Open | Stopping dynamics |
| P07 | Start from rest | Stand still 2 s; begin walking naturally; walk forward; stop. | 5 m | 7–9 s | Open | Stand-to-walk transition |

### Turning & trajectories

| ID | Primitive | Participant instruction | Distance / geometry | Approx. duration | Room | Purpose |
|---|---|---|---:|---:|---|---|
| P08 | Gentle curves | Walk forward; gradually curve left/right; straighten out. | ~5 m path | 7–10 s | Open | Continuous curvature |
| P09 | 45° turn | Walk straight; at marker turn ~45° while continuing to walk; continue straight. | ~5 m | 6–9 s | Open | Moderate heading change |
| P10 | 90° turn | Walk straight; turn 90° left/right while continuing to walk; continue. | ~5 m | 6–9 s | Open | Major heading change |
| P11 | 180° turn | Walk straight; turn around and walk back; use wide or tight turn. | ~5 m | 7–10 s | Open | Reverse heading |
| P12 | Multiple turns | Walk through 3–5 turns in sequence; keep moving; stop at end. | ~8–12 m | 10–15 s | Open | Composed turning |
| P13 | Figure eight | Walk one full figure-eight continuously at comfortable speed; stop. | ~8–12 m | 10–15 s | Open | Alternating curvature |

### Non-forward motion

| ID | Primitive | Participant instruction | Distance / geometry | Approx. duration | Room | Purpose |
|---|---|---|---:|---:|---|---|
| P14 | Sideways walking | Move laterally left/right for ~3 m; natural controlled steps; stop. | 3 m | 5–8 s | Open | Lateral locomotion |
| P15 | Diagonal walking | Walk forward while moving diagonally left/right; finish ~45° from start direction. | ~4–5 m | 6–9 s | Open | Diagonal locomotion |
| P16 | Backward walking | Walk backward slowly; stop. | 3 m | 6–10 s | Open | Backward locomotion |

### Composed & long sequences

| ID | Primitive | Participant instruction | Distance / geometry | Approx. duration | Room | Purpose |
|---|---|---|---:|---:|---|---|
| P17 | Speed change + turn | Walk normally; turn left/right while slowing; after turn accelerate back to normal. | ~5–7 m | 8–12 s | Open | Coupled speed/heading |
| P18 | Stop → turn → walk | Walk; stop fully; turn 90° left/right while standing; walk new direction. | ~5 m | 8–11 s | Open | Discrete transition |
| P19 | Walk → turn → stop | Walk; turn 90° left/right; walk several steps; stop. | ~5 m | 7–10 s | Open | Composed primitive |
| P20 | S-shaped walking | Follow S-shaped path continuously; no stopping between curves. | ~8–10 m | 10–15 s | Open | Continuous heading changes |
| P21 | Variable-speed natural walk | Walk continuously ~15 s; naturally alternate slow/normal/fast several times; no stop. | Room | 15 s | Open | Natural speed trajectory |
| P22 | Random locomotion | Walk through randomly placed floor targets; choose natural path; include turns and speed changes. | Room | 15–25 s | Open | Goal-directed variation |
| P23 | Long continuous locomotion | Walk continuously around room; naturally change direction/speed; no manipulation. | Room | 30 s | Open | Long-horizon baseline |

### Full-body transitions

| ID | Primitive | Participant instruction | Distance / geometry | Approx. duration | Room | Purpose |
|---|---|---|---:|---:|---|---|
| P24 | Walk → crouch → walk | Walk; crouch comfortably at marker; remain briefly; stand and continue. | ~5 m | 10–15 s | Transition | Whole-body transition |
| P25 | Walk → sit → stand → walk | Walk to chair; sit ~2 s; stand; walk away. | ~5 m | 12–18 s | Transition | Sit/stand transition |

### Terrain & navigation

| ID | Primitive | Participant instruction | Distance / geometry | Approx. duration | Room | Purpose |
|---|---|---|---:|---:|---|---|
| P26 | Step over obstacle | Walk toward low obstacle; step over without stopping; continue. | ~5 m | 7–10 s | Terrain | Foot placement |
| P27 | Walk around obstacle | Walk toward obstacle; alter path to avoid it; continue toward original destination. | ~5 m | 7–12 s | Navigation | Obstacle avoidance |
| P28 | Narrow passage | Walk through passage without touching sides; continue normally after exit. | 0.7–1.2 m | 7–12 s | Navigation | Body-aware navigation |
| P29 | Target-directed navigation | Start at marker; visit targets in order; choose natural paths; avoid obstacles; stop at final target. | Room | 15–30 s | Navigation | High-value goal-directed locomotion |

"""


def _dataset_readme() -> str:
    return (
        """---
domain:
- multi-modal
- cv
license: Apache License 2.0
tasks:
- action-recognition
- body-3d-keypoints
tags:
- egocentric-video
- human-motion
- motion-capture
- synchronized-multimodal
- wearable-sensors
- imu
- magnetometer
configs:
- config_name: default
  data_files:
  - split: train
    path: metadata.jsonl
---

# EgoMocap Dataset

Each row in `metadata.jsonl` describes one recording. Session files use the stable
`EgoMotionActions/PXX/<session_id>/` hierarchy, where `PXX` is an action primitive ID.

Every session directory includes a self-contained `timestamp_anomaly_detail_table.html`
inspection report. Paths inside manifests and reports are dataset-relative.

"""
        + _DATASET_DOWNLOAD
        + _ACTION_TASK_CATALOG
        + """## Session contents

- Required capture streams: six `robocap_<segment>_video_*.mp4` first-person cameras, Robocap
  IMU/MAG databases, third-person videos, and Robowrist left/right video and sensor streams.
- Required motion-capture content: the explicitly selected NOKOV body, rigid-body, and third-person
  files under `mocap/`. The concrete BVH, CSV, TRC, XRS, C3D, or other export formats are optional
  choices, but at least one motion-capture format must be present. Files not selected during
  staging are not published.
- Optional artifact: RRD files, only when explicitly selected.
- Generated: `manifest.json` and `timestamp_anomaly_detail_table.html`.

Raw calibration lives outside each action session under the dataset-level
`raw_calibration/<device_id>/` tree. Session manifests and `metadata.jsonl` contain only explicit
`main`, `left`, and `right` device IDs needed to resolve those records.

## Alignment variants

Session staging normally preserves the complete source recording. A session may instead contain
only the frame-aligned intersection selected by its source Robocap-frame offset and Mocap/Robocap
ratio. For an intersection session, `manifest.json` records the source half-open frame interval of
every video and motion-capture file, the source offset, and the capture-time window applied to
SQLite sensors. After cropping, all staged streams have zero residual offset. Original TRC/CSV/XRS
frame and timestamp columns and the C3D first-frame identity remain unchanged for traceability.

## Complete dataset structure

Only the concrete NOKOV motion-capture export format selection and explicitly selected RRD files
are optional. All other listed capture streams and generated records are required.

```text
<dataset_root>/
  README.md                                      # required Dataset Card
  metadata.jsonl                                # required global session index
  raw_calibration/                              # required, maintained separately
    <device_id>/
  EgoMotionActions/                             # required action recordings
    PXX/
      <session_id>/
        robocap_<segment>_video_*.mp4            # required six first-person videos
        robocap_<segment>_imu_*.db               # required Robocap IMU
        robocap_<segment>_mag_*.db               # required Robocap MAG
        mocap/
          *.mp4                                  # required third-person video
          *.{bvh,trc,csv,xrs,c3d,...}            # one or more formats required
        robowrist_<device_id>_left/               # required left streams
        robowrist_<device_id>_right/              # required right streams
        rerun/<segment>/inspection/*.rrd          # optional
        manifest.json                            # generated, required
        timestamp_anomaly_detail_table.html      # generated, required
```

`README.md` is the ModelScope Dataset Card. `metadata.jsonl` is the dataset-wide session index.
`raw_calibration/` is a required dataset-level collection maintained by the calibration workflow;
session staging never copies session-local calibration files into it. `EgoMotionActions/` is
generated by session staging.
"""
    )


def _write_dataset_readme(dataset_root: Path) -> Path:
    readme_path = dataset_root / DATASET_README_NAME
    document = _dataset_readme()
    if not readme_path.is_file() or readme_path.read_text(encoding="utf-8") != document:
        readme_path.write_text(document, encoding="utf-8", newline="\n")
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
    mocap_files: Sequence[str | Path] | None = None,
    include_rrd: bool = False,
    rrd_files: Sequence[str | Path] | None = None,
    inspection_report: Path | None = None,
    aligned_intersection: bool = False,
    frame_ratio: float | None = None,
    video_frame_offset: int = 0,
    reference_video_label: str = "left",
    dry_run: bool = False,
    progress: Callable[[str], None] | None = print,
) -> StageResult:
    source = session_dir.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(source)
    primitive = validate_primitive_id(primitive_id)
    resolved_session_id = validate_session_id(session_id or source.name)
    root = (dataset_root or default_dataset_root(source)).expanduser().resolve()
    target = root / ACTIONS_DIR_NAME / primitive / resolved_session_id
    _validate_stage_locations(source, root, target)
    device_ids = discover_device_ids(source, ffprobe)

    report_source = inspection_report or find_inspection_report(source, segment)
    if report_source is None or not report_source.is_file():
        raise FileNotFoundError(
            f"Required inspection report not found: run inspect for {source} before staging."
        )
    discovered_files = discover_package_files(
        source,
        segment,
        include_artifacts=False,
        include_rrd=False,
    )
    resolved_mocap_files = resolve_mocap_files(source, mocap_files)
    files = sorted(
        [path for path in discovered_files if not is_path_under_mocap(path, source)]
        + resolved_mocap_files,
        key=lambda path: path.as_posix().casefold(),
    )
    rerun_files = resolve_rerun_files(
        source,
        segment,
        include_all=include_rrd,
        selected_files=rrd_files,
    )
    if not files:
        raise ModelScopePublisherError(f"No session files were discovered in {source}.")
    intersection_plan: AlignedIntersectionPlan | None = None
    if aligned_intersection:
        if frame_ratio is None:
            raise ValueError("Aligned-intersection staging requires a resolved frame ratio.")
        try:
            intersection_plan = build_aligned_intersection_plan(
                source,
                files,
                segment=segment,
                ratio=frame_ratio,
                video_frame_offset=video_frame_offset,
                ffprobe=ffprobe,
                reference_video_label=reference_video_label,
                progress=progress,
            )
        except DatasetIntersectionError as exc:
            raise ModelScopePublisherError(str(exc)) from exc

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
            alignment=(intersection_plan.as_manifest() if intersection_plan else None),
        )

    target.mkdir(parents=True, exist_ok=True)
    reset_staged_mocap_directories(target, progress)
    reset_staged_rerun_directory(target)
    packaged: list[PackagedFile] = []
    aligned_records: dict[str, dict[str, object]] = {}
    total_source_files = len(files) + len(rerun_files)
    for index, path in enumerate(files, start=1):
        package_relative = canonical_mocap_relative_path(path, source)
        if progress is not None:
            if intersection_plan is not None:
                operation = "crop"
            else:
                operation = "compress" if is_video(path) and not raw_video else "copy"
            progress(f"[{index}/{total_source_files}] {operation} {path.relative_to(source)}")
        if intersection_plan is not None:
            try:
                item, aligned_record = stage_aligned_file(
                    path,
                    source,
                    target,
                    intersection_plan,
                    raw_video=raw_video,
                    ffmpeg=ffmpeg,
                    proxy_height=proxy_height,
                    proxy_crf=proxy_crf,
                    proxy_bitrate=proxy_bitrate,
                    package_relative=package_relative,
                )
            except (DatasetIntersectionError, subprocess.CalledProcessError) as exc:
                relative = path.relative_to(source)
                raise ModelScopePublisherError(
                    f"Aligned-intersection staging failed for {relative}: {exc}"
                ) from exc
            packaged.append(item)
            aligned_records[item.source] = aligned_record
        else:
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
                    package_relative=package_relative,
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
    file_records = []
    for item in packaged:
        record = asdict(item)
        if item.source in aligned_records:
            record["aligned_selection"] = aligned_records[item.source]
        file_records.append(record)
    file_records.append(report_record)
    total_bytes = sum(int(item["packaged_bytes"]) for item in file_records)
    manifest = {
        "schema_version": 2 if intersection_plan is not None else 1,
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
            "mocap_selection": "all" if mocap_files is None else "explicit",
            "mocap_files": [
                path.relative_to(source).as_posix() for path in resolved_mocap_files
            ],
            "include_rrd": bool(rerun_files),
            "rrd_files": [path.relative_to(source).as_posix() for path in rerun_files],
            "aligned_intersection": intersection_plan is not None,
        },
        "files": file_records,
    }
    if intersection_plan is not None:
        manifest["alignment"] = intersection_plan.as_manifest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    relative_session = f"{ACTIONS_DIR_NAME}/{primitive}/{resolved_session_id}"
    metadata_record: dict[str, object] = {
        "primitive_id": primitive,
        "session_id": resolved_session_id,
        "session_path": relative_session,
        "manifest": f"{relative_session}/manifest.json",
        "inspection_html": f"{relative_session}/{REPORT_NAME}",
        "segment": segment or "auto/all",
        "device_ids": device_ids,
        "file_count": len(file_records),
        "packaged_bytes": total_bytes,
    }
    if intersection_plan is not None:
        metadata_record["alignment"] = intersection_plan.as_metadata()
    metadata_path = _update_metadata(root, metadata_record)
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
        alignment=(intersection_plan.as_manifest() if intersection_plan else None),
    )


def load_staged_session(dataset_root: Path, primitive_id: str, session_id: str) -> StageResult:
    root = dataset_root.expanduser().resolve()
    primitive = validate_primitive_id(primitive_id)
    resolved_session_id = validate_session_id(session_id)
    target = root / ACTIONS_DIR_NAME / primitive / resolved_session_id
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
    alignment = manifest.get("alignment")
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
        alignment=alignment if isinstance(alignment, dict) else None,
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
        expected_path = f"{ACTIONS_DIR_NAME}/{primitive}/{session_id}"
        if entry.get("session_path") != expected_path:
            raise ModelScopePublisherError(
                f"Metadata path for {primitive}/{session_id} must be {expected_path}."
            )
        session_dir = root / ACTIONS_DIR_NAME / primitive / session_id
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
    repo_id: str | None,
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
    repository_value = (repo_id or resolved.repo_id or "").strip()
    if not repository_value:
        raise ModelScopePublisherError(
            f"Repository is not configured. Pass --repo-id or set {REPO_ID_KEY} in {resolved.env_path}."
        )
    repository = validate_repo_id(repository_value)
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
        if (staged.dataset_root / CALIBRATION_DIR_NAME).is_dir():
            allow_patterns.append(f"{CALIBRATION_DIR_NAME}/**")
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
            disable_tqdm=False,
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
    repo_id: str | None,
    **kwargs: Any,
) -> UploadResult:
    dataset = load_staged_dataset(staged.dataset_root)
    selected = f"{ACTIONS_DIR_NAME}/{staged.primitive_id}/{staged.session_id}"
    if selected not in dataset.session_paths:
        raise ModelScopePublisherError(
            f"Selected staged session is missing from metadata: {selected}"
        )
    return upload_staged_dataset(dataset, repo_id, **kwargs)
