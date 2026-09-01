from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from robocap_rerun_tools import MEDIA_TOOLS
from robocap_rerun_tools.frame_comparison import (
    FrameComparisonError,
    create_frame_comparison,
    discover_video_files,
    iter_frame_comparison,
)


def make_test_video(path: Path, color: str, frame_count: int = 4) -> Path:
    assert MEDIA_TOOLS.ffmpeg is not None
    result = subprocess.run(
        [
            MEDIA_TOOLS.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=80x40:r=1",
            "-frames:v",
            str(frame_count),
            "-c:v",
            "ffv1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    return path


def test_frame_comparison_uses_video_columns_and_inclusive_frame_rows(tmp_path: Path) -> None:
    red = make_test_video(tmp_path / "red.mkv", "red")
    green = make_test_video(tmp_path / "green.mkv", "green")

    output = create_frame_comparison(
        [red, green],
        1,
        3,
        cell_width=160,
        cell_height=90,
        output_dir=tmp_path / "output",
    )

    assert output.name.startswith("frame_comparison_v2_f1-3_cell160x90_cfg-")
    with Image.open(output) as image:
        assert image.size == (320, 270)
        pixels = np.asarray(image.convert("RGB"))

    assert pixels[45, 80, 0] > 200
    assert pixels[45, 80, 1] < 40
    assert pixels[45, 240, 1] > 90
    assert pixels[45, 240, 0] < 40
    assert np.max(pixels[0:30, 0:90]) > 240
    assert np.max(pixels[90:120, 0:90]) > 240
    assert np.max(pixels[180:210, 160:250]) > 240
    assert np.max(pixels[0:4, 120:155]) < 30
    assert not list((tmp_path / "output").glob(".frame_comparison_*"))


def test_frame_comparison_reports_cell_level_progress(tmp_path: Path) -> None:
    video = make_test_video(tmp_path / "video.mkv", "blue", frame_count=3)

    progress = list(
        iter_frame_comparison(
            [video],
            0,
            2,
            cell_width=64,
            cell_height=64,
            output_dir=tmp_path / "output",
        )
    )

    assert [(item.completed, item.frame_index) for item in progress[:-1]] == [
        (1, 0),
        (2, 1),
        (3, 2),
    ]
    assert progress[-1].completed == progress[-1].total == 3
    assert progress[-1].output_path is not None


def test_frame_comparison_rejects_a_range_past_the_video_end(tmp_path: Path) -> None:
    video = make_test_video(tmp_path / "short.mkv", "white", frame_count=2)

    with pytest.raises(FrameComparisonError, match="ended before requested frame 2"):
        list(
            iter_frame_comparison(
                [video],
                0,
                2,
                cell_width=64,
                cell_height=64,
                output_dir=tmp_path / "output",
            )
        )


def test_frame_comparison_validates_selection_range_and_jpeg_dimensions(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"")

    with pytest.raises(FrameComparisonError, match="Select at least one video"):
        list(iter_frame_comparison([], 0, 1, output_dir=tmp_path))
    with pytest.raises(FrameComparisonError, match="End frame"):
        list(iter_frame_comparison([video], 2, 1, output_dir=tmp_path))
    with pytest.raises(FrameComparisonError, match="Start frame"):
        list(iter_frame_comparison([video], -1, 1, output_dir=tmp_path))
    with pytest.raises(FrameComparisonError, match="65,500"):
        list(iter_frame_comparison([video], 0, 121, output_dir=tmp_path))


def test_video_discovery_excludes_generated_and_tooling_directories(tmp_path: Path) -> None:
    expected = tmp_path / "capture" / "front.MP4"
    expected.parent.mkdir()
    expected.write_bytes(b"")
    second = tmp_path / "mocap" / "third.mov"
    second.parent.mkdir()
    second.write_bytes(b"")
    generated = tmp_path / "_artifacts" / "proxy.mp4"
    generated.parent.mkdir()
    generated.write_bytes(b"")
    environment = tmp_path / ".venv" / "sample.mkv"
    environment.parent.mkdir()
    environment.write_bytes(b"")

    assert discover_video_files(tmp_path) == [expected.resolve(), second.resolve()]
