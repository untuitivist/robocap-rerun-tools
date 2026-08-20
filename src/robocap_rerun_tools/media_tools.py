"""Make uv-installed FFmpeg binaries available to subprocesses."""

from __future__ import annotations

import os
import shutil
import warnings
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: str | None
    ffprobe: str | None
    source: str


def _existing_path(value: object) -> str | None:
    if value is None:
        return None
    path = Path(value)
    if not path.is_file():
        return None
    return str(path.resolve())


def bundled_media_tools() -> MediaTools:
    """Return binaries supplied by ffmpeg-binaries-compat without downloading at runtime."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import ffmpeg_binaries
    except ImportError:
        return MediaTools(None, None, "not installed")

    ffmpeg = _existing_path(getattr(ffmpeg_binaries, "FFMPEG_PATH", None))
    ffprobe = _existing_path(getattr(ffmpeg_binaries, "FFPROBE_PATH", None))
    source = "uv bundled" if ffmpeg and ffprobe else "unsupported platform wheel"
    return MediaTools(ffmpeg, ffprobe, source)


def activate_media_tools() -> MediaTools:
    """Prefer the locked uv binaries and fall back to a complete system installation."""
    bundled = bundled_media_tools()
    if bundled.ffmpeg and bundled.ffprobe:
        binary_dir = str(Path(bundled.ffmpeg).parent)
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        if binary_dir not in path_entries:
            os.environ["PATH"] = os.pathsep.join([binary_dir, *path_entries])
        return bundled

    system_ffmpeg = shutil.which("ffmpeg")
    system_ffprobe = shutil.which("ffprobe")
    source = "system PATH" if system_ffmpeg and system_ffprobe else bundled.source
    return MediaTools(system_ffmpeg, system_ffprobe, source)
