"""Build frame-by-frame contact sheets from multiple videos."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from robocap_rerun_tools import MEDIA_TOOLS

VIDEO_SUFFIXES = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})
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
    video_index: int
    video_count: int
    video_path: Path
    frame_index: int
    output_path: Path | None = None


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


def _selected_videos(values: Sequence[str | Path]) -> list[Path]:
    selected: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        path = Path(value).expanduser().resolve()
        if path in seen:
            continue
        if not path.is_file():
            raise FrameComparisonError(f"Video file does not exist: {path}")
        if path.suffix.casefold() not in VIDEO_SUFFIXES:
            raise FrameComparisonError(f"Unsupported video format: {path}")
        selected.append(path)
        seen.add(path)
    if not selected:
        raise FrameComparisonError("Select at least one video.")
    return selected


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
    start_frame: int,
    end_frame: int,
    cell_width: int,
    cell_height: int,
) -> Path:
    fingerprint = hashlib.blake2s(digest_size=5)
    for video in videos:
        fingerprint.update(str(video).encode("utf-8"))
        fingerprint.update(b"\0")
    fingerprint.update(f"{start_frame}:{end_frame}:{cell_width}:{cell_height}".encode("ascii"))
    name = (
        f"frame_comparison_v{len(videos)}_f{start_frame}-{end_frame}_"
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
                process.wait()
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


def _label_frame(image, frame_index: int, font) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    text = f"frame {frame_index}"
    margin = max(4, round(image.height * 0.02))
    padding = max(3, round(image.height * 0.008))
    left, top, right, bottom = draw.textbbox((margin, margin), text, font=font, stroke_width=1)
    draw.rectangle(
        (left - padding, top - padding, right + padding, bottom + padding), fill=(0, 0, 0)
    )
    draw.text(
        (margin, margin),
        text,
        fill=(255, 255, 255),
        font=font,
        stroke_width=1,
        stroke_fill=(0, 0, 0),
    )


def iter_frame_comparison(
    videos: Sequence[str | Path],
    start_frame: object,
    end_frame: object,
    *,
    cell_width: object = 960,
    cell_height: object = 540,
    output_dir: str | Path,
    ffmpeg: str | Path | None = None,
) -> Iterator[FrameComparisonProgress]:
    """Generate a video-column by frame-row JPEG and yield cell-level progress."""
    selected = _selected_videos(videos)
    first_frame = _integer(start_frame, "Start frame", minimum=0)
    last_frame = _integer(end_frame, "End frame", minimum=0)
    width = _integer(cell_width, "Cell width", minimum=1)
    height = _integer(cell_height, "Cell height", minimum=1)
    if last_frame < first_frame:
        raise FrameComparisonError("End frame must be greater than or equal to start frame.")

    frame_count = last_frame - first_frame + 1
    canvas_width = width * len(selected)
    canvas_height = height * frame_count
    if canvas_width > JPEG_MAX_DIMENSION or canvas_height > JPEG_MAX_DIMENSION:
        raise FrameComparisonError(
            "Output exceeds JPEG's 65,500-pixel dimension limit: "
            f"{canvas_width}x{canvas_height}. Reduce the video count, frame range, or cell size."
        )

    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    output_path = _output_path(destination, selected, first_frame, last_frame, width, height)
    resolved_ffmpeg = _resolve_ffmpeg(ffmpeg)
    total = len(selected) * frame_count
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

        for video_index, video in enumerate(selected):
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
                _label_frame(image, frame_index, font)
                row_index = frame_index - first_frame
                y_start = row_index * height
                canvas[y_start : y_start + height, x_start : x_start + width] = np.asarray(image)
                image.close()
                completed += 1
                yield FrameComparisonProgress(
                    completed=completed,
                    total=total,
                    video_index=video_index + 1,
                    video_count=len(selected),
                    video_path=video,
                    frame_index=frame_index,
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
        yield FrameComparisonProgress(
            completed=total,
            total=total,
            video_index=len(selected),
            video_count=len(selected),
            video_path=selected[-1],
            frame_index=last_frame,
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
