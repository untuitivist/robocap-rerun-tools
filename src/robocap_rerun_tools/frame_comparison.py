"""Build frame-by-frame contact sheets from video and motion-capture sources."""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np

from robocap_rerun_tools import MEDIA_TOOLS
from robocap_rerun_tools.alignment import FrameAlignment

VIDEO_SUFFIXES = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})
MOCAP_SUFFIXES = frozenset({".bvh", ".c3d", ".csv", ".trc", ".xrs"})
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        "_analysis",
        "_artifacts",
        "_modelscope_dataset",
        "_prepared",
        "build",
        "dist",
        "node_modules",
    }
)
JPEG_MAX_DIMENSION = 65_500


class FrameComparisonError(RuntimeError):
    """Raised when a requested contact sheet cannot be generated."""


@dataclass(frozen=True)
class FrameComparisonProgress:
    completed: int
    total: int
    source_index: int
    source_count: int
    source_path: Path
    source_kind: str
    frame_index: int
    robocap_frame_index: int
    output_path: Path | None = None

    @property
    def video_index(self) -> int:
        """Backward-compatible alias for callers written before Mocap columns existed."""
        return self.source_index

    @property
    def video_count(self) -> int:
        """Backward-compatible alias for callers written before Mocap columns existed."""
        return self.source_count

    @property
    def video_path(self) -> Path:
        """Backward-compatible alias for callers written before Mocap columns existed."""
        return self.source_path


@dataclass(frozen=True)
class MocapFrameTrack:
    path: Path
    positions: np.ndarray
    point_names: tuple[str, ...]
    connections: tuple[tuple[int, int], ...]
    up_axis: int
    valid_frame_mask: np.ndarray


@dataclass(frozen=True)
class MocapProjection:
    center: np.ndarray
    camera_right: np.ndarray
    camera_up: np.ndarray
    camera_forward: np.ndarray
    projected_center: np.ndarray
    pixels_per_unit: float


def discover_video_files(root: str | Path) -> list[Path]:
    """Return supported videos below *root* while excluding generated/tooling trees."""
    candidate = Path(root).expanduser()
    if not candidate.is_dir():
        raise FrameComparisonError(f"Session directory does not exist: {candidate}")

    videos: list[Path] = []
    for directory, names, files in os.walk(candidate):
        names[:] = [
            name
            for name in names
            if name.casefold() not in EXCLUDED_DIRECTORY_NAMES and not name.startswith(".")
        ]
        directory_path = Path(directory)
        videos.extend(
            path.resolve()
            for name in files
            if (path := directory_path / name).suffix.casefold() in VIDEO_SUFFIXES
            and path.is_file()
        )
    return sorted(videos, key=lambda path: str(path.relative_to(candidate)).casefold())


def discover_mocap_files(root: str | Path) -> list[Path]:
    """Return supported motion-capture files below *root* outside generated/tooling trees."""
    candidate = Path(root).expanduser()
    if not candidate.is_dir():
        raise FrameComparisonError(f"Session directory does not exist: {candidate}")

    files_found: list[Path] = []
    for directory, names, files in os.walk(candidate):
        names[:] = [
            name
            for name in names
            if name.casefold() not in EXCLUDED_DIRECTORY_NAMES and not name.startswith(".")
        ]
        directory_path = Path(directory)
        files_found.extend(
            path.resolve()
            for name in files
            if (path := directory_path / name).suffix.casefold() in MOCAP_SUFFIXES
            and path.is_file()
            and not any(
                part.casefold().startswith("robowrist_")
                for part in path.relative_to(candidate).parts[:-1]
            )
        )
    return sorted(files_found, key=lambda path: str(path.relative_to(candidate)).casefold())


def _integer(value: object, label: str, *, minimum: int) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise FrameComparisonError(f"{label} must be an integer.") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise FrameComparisonError(f"{label} must be an integer.")
    result = int(numeric)
    if result < minimum:
        raise FrameComparisonError(f"{label} must be at least {minimum}.")
    return result


def _selected_files(
    values: Sequence[str | Path],
    *,
    suffixes: frozenset[str],
    label: str,
    required: bool,
) -> list[Path]:
    selected: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        path = Path(value).expanduser().resolve()
        if path in seen:
            continue
        if not path.is_file():
            raise FrameComparisonError(f"{label} file does not exist: {path}")
        if path.suffix.casefold() not in suffixes:
            raise FrameComparisonError(f"Unsupported {label.lower()} format: {path}")
        selected.append(path)
        seen.add(path)
    if required and not selected:
        raise FrameComparisonError(f"Select at least one {label.lower()}.")
    return selected


def _selected_videos(values: Sequence[str | Path], *, required: bool = True) -> list[Path]:
    return _selected_files(
        values,
        suffixes=VIDEO_SUFFIXES,
        label="Robocap video" if required else "Third-person video",
        required=required,
    )


def _selected_mocap_files(values: Sequence[str | Path]) -> list[Path]:
    return _selected_files(
        values,
        suffixes=MOCAP_SUFFIXES,
        label="Mocap",
        required=False,
    )


def _resolve_ffmpeg(value: str | Path | None) -> str:
    if value is not None:
        candidate = str(value)
    else:
        candidate = MEDIA_TOOLS.ffmpeg or shutil.which("ffmpeg") or ""
    if not candidate:
        raise FrameComparisonError(
            "FFmpeg was not found. Run `uv sync --extra web` before generating a frame comparison."
        )
    return candidate


def _output_path(
    output_dir: Path,
    videos: Sequence[Path],
    third_person_videos: Sequence[Path],
    mocap_files: Sequence[Path],
    start_frame: int,
    end_frame: int,
    cell_width: int,
    cell_height: int,
    alignment: FrameAlignment,
) -> Path:
    fingerprint = hashlib.blake2s(digest_size=5)
    for kind, paths in (
        ("robocap", videos),
        ("third", third_person_videos),
        ("mocap", mocap_files),
    ):
        fingerprint.update(kind.encode("ascii"))
        fingerprint.update(b"\0")
        for path in paths:
            fingerprint.update(str(path).encode("utf-8"))
            fingerprint.update(b"\0")
    fingerprint.update(
        (
            f"{start_frame}:{end_frame}:{cell_width}:{cell_height}:"
            f"{alignment.ratio}:{alignment.video_frame_offset}:"
            f"{alignment.third_person_video_frame_offset}"
        ).encode("ascii")
    )
    name = (
        f"frame_comparison_v{len(videos)}_t{len(third_person_videos)}_m{len(mocap_files)}_"
        f"f{start_frame}-{end_frame}_r{alignment.ratio:g}_"
        f"mo{alignment.video_frame_offset}_to{alignment.third_person_video_frame_offset}_"
        f"cell{cell_width}x{cell_height}_cfg-{fingerprint.hexdigest()}.jpg"
    )
    return output_dir / name


def _read_exact(stream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _decode_frames(
    video: Path,
    start_frame: int,
    end_frame: int,
    cell_width: int,
    cell_height: int,
    ffmpeg: str,
    *,
    allow_short: bool = False,
) -> Iterator[tuple[int, bytes]]:
    frame_count = end_frame - start_frame + 1
    frame_size = cell_width * cell_height * 3
    video_filter = (
        f"trim=start_frame={start_frame}:end_frame={end_frame + 1},"
        "setpts=PTS-STARTPTS,"
        f"scale={cell_width}:{cell_height}:force_original_aspect_ratio=decrease,"
        f"pad={cell_width}:{cell_height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1,format=rgb24"
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-vf",
        video_filter,
        "-an",
        "-sn",
        "-dn",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise FrameComparisonError(f"Could not start FFmpeg for {video}: {exc}") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    try:
        for offset in range(frame_count):
            payload = _read_exact(process.stdout, frame_size)
            if len(payload) != frame_size:
                error = process.stderr.read().decode("utf-8", errors="replace").strip()
                returncode = process.wait()
                if allow_short and returncode == 0 and not error:
                    return
                detail = f" FFmpeg: {error}" if error else ""
                raise FrameComparisonError(
                    f"{video.name} ended before requested frame {start_frame + offset}.{detail}"
                )
            yield start_frame + offset, payload

        if process.stdout.read(1):
            process.kill()
            process.wait()
            raise FrameComparisonError(f"FFmpeg returned unexpected extra frames for {video}.")
        error = process.stderr.read().decode("utf-8", errors="replace").strip()
        returncode = process.wait()
        if returncode != 0:
            detail = error or f"exit code {returncode}"
            raise FrameComparisonError(f"FFmpeg failed for {video}: {detail}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def _load_font(cell_height: int):
    from PIL import ImageFont

    size = max(14, min(48, round(cell_height * 0.055)))
    candidates = [
        "DejaVuSans-Bold.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit_text(draw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    suffix = "..."
    candidate = text
    while candidate and draw.textlength(candidate + suffix, font=font) > max_width:
        candidate = candidate[:-1]
    return candidate + suffix


def _label_frame(
    image,
    frame_index: int,
    font,
    *,
    robocap_frame_index: int | None = None,
    source_label: str | None = None,
) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    lines = [f"frame {frame_index}"]
    if robocap_frame_index is not None and robocap_frame_index != frame_index:
        lines.append(f"Robocap {robocap_frame_index}")
    text = "\n".join(lines)
    margin = max(4, round(image.height * 0.02))
    padding = max(3, round(image.height * 0.008))
    left, top, right, bottom = draw.multiline_textbbox(
        (margin, margin), text, font=font, stroke_width=1, spacing=2
    )
    draw.rectangle(
        (left - padding, top - padding, right + padding, bottom + padding), fill=(0, 0, 0)
    )
    draw.multiline_text(
        (margin, margin),
        text,
        fill=(255, 255, 255),
        font=font,
        stroke_width=1,
        stroke_fill=(0, 0, 0),
        spacing=2,
    )
    if source_label:
        fitted = _fit_text(draw, source_label, font, image.width - margin * 2)
        label_box = draw.textbbox((0, 0), fitted, font=font)
        label_height = label_box[3] - label_box[1]
        label_y = image.height - margin - label_height
        draw.rectangle(
            (
                margin - padding,
                label_y - padding,
                margin + draw.textlength(fitted, font=font) + padding,
                image.height - margin + padding,
            ),
            fill=(0, 0, 0),
        )
        draw.text((margin, label_y), fitted, fill=(210, 218, 229), font=font)


def _unavailable_frame(
    width: int,
    height: int,
    frame_index: int,
    robocap_frame_index: int,
    source_label: str,
    font,
):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (16, 19, 25))
    draw = ImageDraw.Draw(image)
    message = "source frame unavailable"
    box = draw.textbbox((0, 0), message, font=font)
    draw.text(
        ((width - (box[2] - box[0])) / 2, (height - (box[3] - box[1])) / 2),
        message,
        fill=(180, 188, 200),
        font=font,
    )
    _label_frame(
        image,
        frame_index,
        font,
        robocap_frame_index=robocap_frame_index,
        source_label=source_label,
    )
    return image


def _canonical_marker_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.casefold().split(":")[-1])


def _hand_connections(marker_names: Sequence[str]) -> tuple[tuple[int, int], ...]:
    canonical_to_index = {
        _canonical_marker_name(name): index for index, name in enumerate(marker_names)
    }
    wrist_names = ("wristm", "wristin", "wristout", "handoffset")
    wrist_indexes = [canonical_to_index[name] for name in wrist_names if name in canonical_to_index]
    connections: list[tuple[int, int]] = list(pairwise(wrist_indexes))
    palm_anchor = wrist_indexes[0] if wrist_indexes else None
    for finger in ("fingerthumb", "fingerindex", "fingermiddle", "fingerring", "fingerpinky"):
        chain = [
            canonical_to_index[f"{finger}{index}"]
            for index in range(1, 5)
            if f"{finger}{index}" in canonical_to_index
        ]
        if palm_anchor is not None and chain:
            connections.append((palm_anchor, chain[0]))
        connections.extend(pairwise(chain))
    return tuple(connections)


def _infer_up_axis(positions: np.ndarray, *, conventional_y_up: bool = False) -> int:
    if conventional_y_up:
        return 1
    finite = np.where(np.isfinite(positions), positions, np.nan)
    with np.errstate(all="ignore"):
        frame_spans = np.nanpercentile(finite, 95, axis=1) - np.nanpercentile(
            finite, 5, axis=1
        )
        structural_spans = np.nanmedian(frame_spans, axis=0)
    if not np.isfinite(structural_spans).any() or float(np.nanmax(structural_spans)) <= 1e-8:
        return 1
    return int(np.nanargmax(structural_spans))


def _semantic_up_axis(marker_names: Sequence[str], positions: np.ndarray) -> int | None:
    unique_indexes: dict[str, int] = {}
    for index, name in enumerate(marker_names):
        local_name = name.casefold().split(":")[-1]
        base_name = re.sub(r"_\d+$", "", local_name)
        unique_indexes.setdefault(re.sub(r"[^a-z0-9]", "", base_name), index)

    head_indexes = [index for name, index in unique_indexes.items() if "head" in name]
    foot_indexes = [
        index
        for name, index in unique_indexes.items()
        if any(token in name for token in ("foot", "ankle", "heel", "toe"))
    ]
    if not head_indexes or not foot_indexes:
        return None
    with np.errstate(all="ignore"):
        head_center = np.nanmedian(positions[:, head_indexes, :], axis=(0, 1))
        foot_center = np.nanmedian(positions[:, foot_indexes, :], axis=(0, 1))
    difference = np.abs(head_center - foot_center)
    if not np.isfinite(difference).any() or float(np.nanmax(difference)) <= 1e-8:
        return None
    return int(np.nanargmax(difference))


def _resolve_up_axis(marker_names: Sequence[str], positions: np.ndarray) -> int:
    semantic_axis = _semantic_up_axis(marker_names, positions)
    return _infer_up_axis(positions) if semantic_axis is None else semantic_axis


def _load_c3d_track(path: Path) -> tuple[np.ndarray, tuple[str, ...]]:
    from robocap_rerun_tools.dataset_intersection import _load_c3d
    from robocap_rerun_tools.exporter import fill_missing_marker_positions

    capture = _load_c3d(path)
    point_data = np.asarray(capture["data"]["points"], dtype=np.float32)
    if point_data.ndim != 3 or point_data.shape[0] < 3 or point_data.shape[2] == 0:
        raise FrameComparisonError(f"C3D file has no point frames: {path}")
    positions = np.transpose(point_data[:3], (2, 1, 0)).copy()
    if point_data.shape[0] >= 4:
        positions[np.transpose(point_data[3], (1, 0)) < 0] = np.nan

    labels_raw = capture.get("parameters", {}).get("POINT", {}).get("LABELS", {}).get(
        "value", []
    )
    point_names = tuple(str(name).strip() for name in labels_raw)
    if len(point_names) != positions.shape[1]:
        point_names = tuple(f"point_{index:03d}" for index in range(positions.shape[1]))

    units_raw = capture.get("parameters", {}).get("POINT", {}).get("UNITS", {}).get(
        "value", ["mm"]
    )
    unit = str(units_raw[0] if len(units_raw) else "mm").strip().casefold()
    scale = {"m": 1.0, "meter": 1.0, "metre": 1.0, "cm": 0.01, "mm": 0.001}.get(
        unit, 0.001
    )
    positions *= scale
    if np.isnan(positions).any():
        positions = fill_missing_marker_positions(positions)
    return positions, point_names


def load_mocap_frame_track(path: str | Path) -> MocapFrameTrack:
    """Load one supported Mocap export into the common frame-comparison representation."""
    from robocap_rerun_tools.exporter import (
        infer_nokov_view_up_axis,
        load_bvh_track,
        load_csv_track,
        load_marker_track_from_trc,
        load_xrs_track,
    )

    source = Path(path).expanduser().resolve()
    suffix = source.suffix.casefold()
    if suffix == ".bvh":
        track = load_bvh_track(source, source.stem, "", "bvh", 0.01, None)
        positions = track.positions
        point_names = track.marker_names
        connections = track.connections
        up_axis = 1
        valid_frame_mask = np.ones(len(positions), dtype=bool)
    elif suffix == ".trc":
        track = load_marker_track_from_trc(
            source,
            source.stem,
            "",
            "trc",
            0.001,
            None,
            connect_hands=True,
        )
        positions = track.positions
        point_names = track.marker_names
        connections = track.connections or _hand_connections(point_names)
        up_axis = _resolve_up_axis(point_names, positions)
        valid_frame_mask = np.ones(len(positions), dtype=bool)
    elif suffix in {".csv", ".xrs"}:
        loader = load_csv_track if suffix == ".csv" else load_xrs_track
        track = loader(source, source.stem, "", suffix[1:], 0.001, None)
        positions = track.positions
        point_names = track.marker_names
        connections = track.connections
        inferred = infer_nokov_view_up_axis([source])
        up_axis = {"x": 0, "y": 1, "z": 2}.get(inferred or "")
        if up_axis is None:
            up_axis = _resolve_up_axis(point_names, positions)
        valid_frame_mask = np.asarray(track.timestamps_ns, dtype=np.int64) > 0
    elif suffix == ".c3d":
        positions, point_names = _load_c3d_track(source)
        connections = _hand_connections(point_names)
        up_axis = _resolve_up_axis(point_names, positions)
        valid_frame_mask = np.ones(len(positions), dtype=bool)
    else:
        raise FrameComparisonError(f"Unsupported Mocap format: {source}")

    positions = np.asarray(positions, dtype=np.float32)
    if positions.ndim != 3 or positions.shape[2] != 3 or positions.shape[0] == 0:
        raise FrameComparisonError(f"Mocap file has no 3D frames: {source}")
    return MocapFrameTrack(
        path=source,
        positions=positions,
        point_names=tuple(point_names),
        connections=tuple(connections),
        up_axis=up_axis,
        valid_frame_mask=valid_frame_mask,
    )


def _mocap_projection(
    track: MocapFrameTrack,
    source_frames: Sequence[int],
    width: int,
    height: int,
) -> MocapProjection:
    valid_frames = sorted(
        {
            index
            for index in source_frames
            if 0 <= index < len(track.positions) and bool(track.valid_frame_mask[index])
        }
    )
    if not valid_frames:
        return MocapProjection(
            center=np.zeros(3, dtype=np.float64),
            camera_right=np.asarray([1.0, 0.0, 0.0]),
            camera_up=np.asarray([0.0, 1.0, 0.0]),
            camera_forward=np.asarray([0.0, 0.0, 1.0]),
            projected_center=np.zeros(2, dtype=np.float64),
            pixels_per_unit=1.0,
        )

    points = np.asarray(track.positions[valid_frames], dtype=np.float64)
    flat_points = points.reshape(-1, 3)
    flat_points = flat_points[np.isfinite(flat_points).all(axis=1)]
    if not len(flat_points):
        raise FrameComparisonError(f"Mocap frames contain no finite 3D points: {track.path}")

    center = np.nanmedian(flat_points, axis=0)
    basis = np.eye(3, dtype=np.float64)
    up = basis[track.up_axis]
    horizontal_axes = [axis for axis in range(3) if axis != track.up_axis]
    centered_per_frame = points - np.nanmedian(points, axis=1, keepdims=True)
    with np.errstate(all="ignore"):
        structural_spans = np.nanmedian(
            np.nanpercentile(centered_per_frame, 95, axis=1)
            - np.nanpercentile(centered_per_frame, 5, axis=1),
            axis=0,
        )
    right_axis = max(horizontal_axes, key=lambda axis: float(structural_spans[axis]))
    depth_axis = next(axis for axis in horizontal_axes if axis != right_axis)
    angle = math.radians(28.0)
    camera_right = math.cos(angle) * basis[right_axis] - math.sin(angle) * basis[depth_axis]
    camera_forward = math.sin(angle) * basis[right_axis] + math.cos(angle) * basis[depth_axis]

    centered = flat_points - center
    projected = np.column_stack((centered @ camera_right, centered @ up))
    low = np.nanpercentile(projected, 1, axis=0)
    high = np.nanpercentile(projected, 99, axis=0)
    span = np.maximum(high - low, 1e-6)
    pixels_per_unit = min((width * 0.82) / span[0], (height * 0.72) / span[1])
    return MocapProjection(
        center=center,
        camera_right=camera_right,
        camera_up=up,
        camera_forward=camera_forward,
        projected_center=(low + high) / 2,
        pixels_per_unit=float(pixels_per_unit),
    )


def _project_mocap_points(
    points: np.ndarray,
    projection: MocapProjection,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    centered = np.asarray(points, dtype=np.float64) - projection.center
    projected = np.column_stack(
        (centered @ projection.camera_right, centered @ projection.camera_up)
    )
    pixels = np.empty_like(projected)
    pixels[:, 0] = (
        width / 2
        + (projected[:, 0] - projection.projected_center[0]) * projection.pixels_per_unit
    )
    pixels[:, 1] = (
        height / 2
        - (projected[:, 1] - projection.projected_center[1]) * projection.pixels_per_unit
    )
    depth = centered @ projection.camera_forward
    return pixels, depth


def _render_mocap_frame(
    track: MocapFrameTrack,
    source_frame: int,
    robocap_frame: int,
    projection: MocapProjection,
    width: int,
    height: int,
    font,
):
    from PIL import Image, ImageDraw

    if (
        source_frame < 0
        or source_frame >= len(track.positions)
        or not bool(track.valid_frame_mask[source_frame])
    ):
        return _unavailable_frame(
            width,
            height,
            source_frame,
            robocap_frame,
            f"Mocap | {track.path.name}",
            font,
        )

    points = np.asarray(track.positions[source_frame], dtype=np.float64)
    valid = np.isfinite(points).all(axis=1)
    if not valid.any():
        return _unavailable_frame(
            width,
            height,
            source_frame,
            robocap_frame,
            f"Mocap | {track.path.name}",
            font,
        )

    image = Image.new("RGB", (width, height), (16, 19, 25))
    draw = ImageDraw.Draw(image)
    margin = max(4, round(height * 0.012))
    draw.rectangle(
        (margin, margin, width - margin - 1, height - margin - 1),
        outline=(56, 65, 78),
        width=max(1, round(height / 360)),
    )
    pixels, depth = _project_mocap_points(points, projection, width, height)
    line_width = max(2, round(height / 180))
    valid_connections = [
        (first, second)
        for first, second in track.connections
        if 0 <= first < len(points)
        and 0 <= second < len(points)
        and valid[first]
        and valid[second]
    ]
    valid_connections.sort(key=lambda edge: float((depth[edge[0]] + depth[edge[1]]) / 2))
    for first, second in valid_connections:
        draw.line(
            (tuple(pixels[first]), tuple(pixels[second])),
            fill=(255, 45, 45),
            width=line_width,
        )
    radius = max(3, round(height / 90))
    _label_frame(
        image,
        source_frame,
        font,
        robocap_frame_index=robocap_frame,
        source_label=f"Mocap | {track.path.name}",
    )
    for point_index in np.argsort(depth):
        if not valid[point_index]:
            continue
        x, y = pixels[point_index]
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=(24, 72, 255),
        )
    return image


def iter_frame_comparison(
    videos: Sequence[str | Path],
    start_frame: object,
    end_frame: object,
    *,
    third_person_videos: Sequence[str | Path] = (),
    mocap_files: Sequence[str | Path] = (),
    mocap_ratio: object = 8,
    mocap_offset: object = 0,
    third_person_offset: object = 0,
    cell_width: object = 960,
    cell_height: object = 540,
    output_dir: str | Path,
    ffmpeg: str | Path | None = None,
) -> Iterator[FrameComparisonProgress]:
    """Generate a source-column by Robocap-frame-row JPEG and yield cell progress."""
    selected_videos = _selected_videos(videos)
    selected_third_person_videos = _selected_videos(third_person_videos, required=False)
    selected_mocap_files = _selected_mocap_files(mocap_files)
    first_frame = _integer(start_frame, "Start frame", minimum=0)
    last_frame = _integer(end_frame, "End frame", minimum=0)
    width = _integer(cell_width, "Cell width", minimum=1)
    height = _integer(cell_height, "Cell height", minimum=1)
    if last_frame < first_frame:
        raise FrameComparisonError("End frame must be greater than or equal to start frame.")
    try:
        alignment = FrameAlignment(
            float(mocap_ratio),
            mocap_offset,
            third_person_offset,
        )
    except (TypeError, ValueError) as exc:
        raise FrameComparisonError(f"Invalid frame alignment: {exc}") from exc

    mocap_tracks: list[MocapFrameTrack] = []
    for mocap_file in selected_mocap_files:
        try:
            mocap_tracks.append(load_mocap_frame_track(mocap_file))
        except FrameComparisonError:
            raise
        except Exception as exc:
            raise FrameComparisonError(f"Could not load Mocap file {mocap_file}: {exc}") from exc

    frame_count = last_frame - first_frame + 1
    source_count = (
        len(selected_videos) + len(selected_third_person_videos) + len(mocap_tracks)
    )
    canvas_width = width * source_count
    canvas_height = height * frame_count
    if canvas_width > JPEG_MAX_DIMENSION or canvas_height > JPEG_MAX_DIMENSION:
        raise FrameComparisonError(
            "Output exceeds JPEG's 65,500-pixel dimension limit: "
            f"{canvas_width}x{canvas_height}. Reduce the source count, frame range, or cell size."
        )

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    output_path = _output_path(
        destination,
        selected_videos,
        selected_third_person_videos,
        selected_mocap_files,
        first_frame,
        last_frame,
        width,
        height,
        alignment,
    )
    resolved_ffmpeg = _resolve_ffmpeg(ffmpeg)
    total = source_count * frame_count
    font = _load_font(height)

    with tempfile.NamedTemporaryFile(
        prefix=".frame_comparison_canvas_", suffix=".rgb", dir=destination, delete=False
    ) as raw_handle:
        raw_path = Path(raw_handle.name)
    with tempfile.NamedTemporaryFile(
        prefix=".frame_comparison_output_", suffix=".jpg", dir=destination, delete=False
    ) as output_handle:
        temporary_output = Path(output_handle.name)

    canvas: np.memmap | None = None
    try:
        canvas = np.memmap(
            raw_path,
            dtype=np.uint8,
            mode="w+",
            shape=(canvas_height, canvas_width, 3),
        )
        completed = 0
        from PIL import Image

        for video_index, video in enumerate(selected_videos):
            x_start = video_index * width
            for frame_index, payload in _decode_frames(
                video,
                first_frame,
                last_frame,
                width,
                height,
                resolved_ffmpeg,
            ):
                image = Image.frombytes("RGB", (width, height), payload)
                _label_frame(
                    image,
                    frame_index,
                    font,
                    robocap_frame_index=frame_index,
                    source_label=f"Robocap | {video.name}",
                )
                row_index = frame_index - first_frame
                y_start = row_index * height
                canvas[y_start : y_start + height, x_start : x_start + width] = np.asarray(image)
                image.close()
                completed += 1
                yield FrameComparisonProgress(
                    completed=completed,
                    total=total,
                    source_index=video_index + 1,
                    source_count=source_count,
                    source_path=video,
                    source_kind="robocap_video",
                    frame_index=frame_index,
                    robocap_frame_index=frame_index,
                )

        third_column_start = len(selected_videos)
        for third_index, video in enumerate(selected_third_person_videos):
            source_index = third_column_start + third_index
            x_start = source_index * width
            mapped_start = alignment.robocap_to_third_person_frame(first_frame)
            mapped_end = alignment.robocap_to_third_person_frame(last_frame)
            next_source_frame = mapped_start

            while next_source_frame < 0 and next_source_frame <= mapped_end:
                robocap_frame = next_source_frame - alignment.third_person_video_frame_offset
                image = _unavailable_frame(
                    width,
                    height,
                    next_source_frame,
                    robocap_frame,
                    f"Third-person | {video.name}",
                    font,
                )
                y_start = (robocap_frame - first_frame) * height
                canvas[y_start : y_start + height, x_start : x_start + width] = np.asarray(image)
                image.close()
                completed += 1
                yield FrameComparisonProgress(
                    completed=completed,
                    total=total,
                    source_index=source_index + 1,
                    source_count=source_count,
                    source_path=video,
                    source_kind="third_person_video",
                    frame_index=next_source_frame,
                    robocap_frame_index=robocap_frame,
                )
                next_source_frame += 1

            if next_source_frame <= mapped_end:
                for frame_index, payload in _decode_frames(
                    video,
                    next_source_frame,
                    mapped_end,
                    width,
                    height,
                    resolved_ffmpeg,
                    allow_short=True,
                ):
                    while next_source_frame < frame_index:
                        robocap_frame = (
                            next_source_frame - alignment.third_person_video_frame_offset
                        )
                        image = _unavailable_frame(
                            width,
                            height,
                            next_source_frame,
                            robocap_frame,
                            f"Third-person | {video.name}",
                            font,
                        )
                        y_start = (robocap_frame - first_frame) * height
                        canvas[y_start : y_start + height, x_start : x_start + width] = np.asarray(
                            image
                        )
                        image.close()
                        completed += 1
                        yield FrameComparisonProgress(
                            completed=completed,
                            total=total,
                            source_index=source_index + 1,
                            source_count=source_count,
                            source_path=video,
                            source_kind="third_person_video",
                            frame_index=next_source_frame,
                            robocap_frame_index=robocap_frame,
                        )
                        next_source_frame += 1

                    robocap_frame = frame_index - alignment.third_person_video_frame_offset
                    image = Image.frombytes("RGB", (width, height), payload)
                    _label_frame(
                        image,
                        frame_index,
                        font,
                        robocap_frame_index=robocap_frame,
                        source_label=f"Third-person | {video.name}",
                    )
                    y_start = (robocap_frame - first_frame) * height
                    canvas[
                        y_start : y_start + height, x_start : x_start + width
                    ] = np.asarray(image)
                    image.close()
                    completed += 1
                    yield FrameComparisonProgress(
                        completed=completed,
                        total=total,
                        source_index=source_index + 1,
                        source_count=source_count,
                        source_path=video,
                        source_kind="third_person_video",
                        frame_index=frame_index,
                        robocap_frame_index=robocap_frame,
                    )
                    next_source_frame = frame_index + 1

            while next_source_frame <= mapped_end:
                robocap_frame = next_source_frame - alignment.third_person_video_frame_offset
                image = _unavailable_frame(
                    width,
                    height,
                    next_source_frame,
                    robocap_frame,
                    f"Third-person | {video.name}",
                    font,
                )
                y_start = (robocap_frame - first_frame) * height
                canvas[y_start : y_start + height, x_start : x_start + width] = np.asarray(image)
                image.close()
                completed += 1
                yield FrameComparisonProgress(
                    completed=completed,
                    total=total,
                    source_index=source_index + 1,
                    source_count=source_count,
                    source_path=video,
                    source_kind="third_person_video",
                    frame_index=next_source_frame,
                    robocap_frame_index=robocap_frame,
                )
                next_source_frame += 1

        mocap_column_start = len(selected_videos) + len(selected_third_person_videos)
        robocap_frames = list(range(first_frame, last_frame + 1))
        mocap_source_frames = [
            alignment.robocap_to_mocap_frame(frame) for frame in robocap_frames
        ]
        for mocap_index, track in enumerate(mocap_tracks):
            source_index = mocap_column_start + mocap_index
            x_start = source_index * width
            projection = _mocap_projection(track, mocap_source_frames, width, height)
            for row_index, (robocap_frame, mocap_frame) in enumerate(
                zip(robocap_frames, mocap_source_frames)
            ):
                image = _render_mocap_frame(
                    track,
                    mocap_frame,
                    robocap_frame,
                    projection,
                    width,
                    height,
                    font,
                )
                y_start = row_index * height
                canvas[y_start : y_start + height, x_start : x_start + width] = np.asarray(image)
                image.close()
                completed += 1
                yield FrameComparisonProgress(
                    completed=completed,
                    total=total,
                    source_index=source_index + 1,
                    source_count=source_count,
                    source_path=track.path,
                    source_kind="mocap",
                    frame_index=mocap_frame,
                    robocap_frame_index=robocap_frame,
                )

        canvas.flush()
        final_image = Image.frombuffer(
            "RGB", (canvas_width, canvas_height), canvas, "raw", "RGB", 0, 1
        )
        try:
            final_image.save(
                temporary_output,
                format="JPEG",
                quality=90,
                subsampling=0,
                optimize=False,
            )
        finally:
            final_image.close()
        os.replace(temporary_output, output_path)
        if mocap_tracks:
            final_path = mocap_tracks[-1].path
            final_kind = "mocap"
            final_frame = mocap_source_frames[-1]
        elif selected_third_person_videos:
            final_path = selected_third_person_videos[-1]
            final_kind = "third_person_video"
            final_frame = alignment.robocap_to_third_person_frame(last_frame)
        else:
            final_path = selected_videos[-1]
            final_kind = "robocap_video"
            final_frame = last_frame
        yield FrameComparisonProgress(
            completed=total,
            total=total,
            source_index=source_count,
            source_count=source_count,
            source_path=final_path,
            source_kind=final_kind,
            frame_index=final_frame,
            robocap_frame_index=last_frame,
            output_path=output_path,
        )
    except MemoryError as exc:
        raise FrameComparisonError(
            f"Not enough memory to encode the {canvas_width}x{canvas_height} output image."
        ) from exc
    finally:
        if canvas is not None:
            canvas.flush()
            del canvas
        raw_path.unlink(missing_ok=True)
        temporary_output.unlink(missing_ok=True)


def create_frame_comparison(*args, **kwargs) -> Path:
    """Generate a contact sheet and return its final path."""
    output_path: Path | None = None
    for progress in iter_frame_comparison(*args, **kwargs):
        if progress.output_path is not None:
            output_path = progress.output_path
    if output_path is None:
        raise FrameComparisonError("Frame comparison did not produce an output file.")
    return output_path
