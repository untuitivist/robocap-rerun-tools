from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from robocap_rerun_tools import MEDIA_TOOLS
from robocap_rerun_tools.frame_comparison import (
    FrameComparisonError,
    _resolve_up_axis,
    create_frame_comparison,
    discover_mocap_files,
    discover_video_files,
    iter_frame_comparison,
    load_mocap_frame_track,
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


def make_test_bvh(path: Path, frame_count: int = 8) -> Path:
    rows = "\n".join(f"{index} 0 0 0 0 0 0 0 0" for index in range(frame_count))
    path.write_text(
        "HIERARCHY\n"
        "ROOT HandRoot\n"
        "{\n"
        "  OFFSET 0 0 0\n"
        "  CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation\n"
        "  JOINT FingerIndex1\n"
        "  {\n"
        "    OFFSET 0 10 0\n"
        "    CHANNELS 3 Zrotation Xrotation Yrotation\n"
        "    End Site\n"
        "    {\n"
        "      OFFSET 0 5 0\n"
        "    }\n"
        "  }\n"
        "}\n"
        "MOTION\n"
        f"Frames: {frame_count}\n"
        "Frame Time: 0.004166667\n"
        f"{rows}\n",
        encoding="utf-8",
    )
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

    assert output.name.startswith("frame_comparison_v2_t0_m0_f1-3_r8_mo0_to0_cell160x90_cfg-")
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

    with pytest.raises(FrameComparisonError, match="Select at least one robocap video"):
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


def test_mocap_discovery_supports_exports_and_excludes_generated_sensor_trees(
    tmp_path: Path,
) -> None:
    mocap = tmp_path / "mocap-A01"
    mocap.mkdir()
    expected = [mocap / "body.bvh", mocap / "body.C3D", mocap / "body.csv", mocap / "body.trc"]
    for path in expected:
        path.write_bytes(b"")
    generated = tmp_path / "_artifacts" / "body.xrs"
    generated.parent.mkdir()
    generated.write_bytes(b"")
    wrist = tmp_path / "robowrist_left" / "sensor.csv"
    wrist.parent.mkdir()
    wrist.write_bytes(b"")

    assert discover_mocap_files(tmp_path) == sorted(
        (path.resolve() for path in expected),
        key=lambda path: str(path.relative_to(tmp_path)).casefold(),
    )


def test_frame_comparison_aligns_third_person_and_mocap_to_robocap_frames(
    tmp_path: Path,
) -> None:
    robocap = make_test_video(tmp_path / "robocap.mkv", "black", frame_count=4)
    third_person = make_test_video(tmp_path / "third.mkv", "white", frame_count=4)
    mocap = make_test_bvh(tmp_path / "hand.bvh")

    progress = list(
        iter_frame_comparison(
            [robocap],
            0,
            1,
            third_person_videos=[third_person],
            mocap_files=[mocap],
            mocap_ratio=2,
            mocap_offset=1,
            third_person_offset=1,
            cell_width=120,
            cell_height=100,
            output_dir=tmp_path / "output",
        )
    )

    cells = progress[:-1]
    assert [(item.source_kind, item.robocap_frame_index, item.frame_index) for item in cells] == [
        ("robocap_video", 0, 0),
        ("robocap_video", 1, 1),
        ("third_person_video", 0, 1),
        ("third_person_video", 1, 2),
        ("mocap", 0, 2),
        ("mocap", 1, 4),
    ]
    output = progress[-1].output_path
    assert output is not None
    assert "_r2_mo1_to1_" in output.name
    with Image.open(output) as image:
        assert image.size == (360, 200)
        pixels = np.asarray(image.convert("RGB"))
    mocap_pixels = pixels[:, 240:]
    assert np.count_nonzero(
        (mocap_pixels[:, :, 0] > 180)
        & (mocap_pixels[:, :, 1] < 100)
        & (mocap_pixels[:, :, 2] < 100)
    ) > 5
    assert np.count_nonzero(
        (mocap_pixels[:, :, 2] > 150)
        & (mocap_pixels[:, :, 0] < 100)
        & (mocap_pixels[:, :, 1] < 140)
    ) > 5


def test_c3d_mocap_track_loads_point_frames_and_known_hand_connections(tmp_path: Path) -> None:
    import ezc3d

    path = tmp_path / "hand.c3d"
    capture = ezc3d.c3d()
    capture["parameters"]["POINT"]["RATE"]["value"] = [240.0]
    capture["parameters"]["POINT"]["LABELS"]["value"] = ["WristM", "FingerIndex1"]
    points = np.zeros((4, 2, 3), dtype=np.float64)
    points[0, 1] = [10.0, 20.0, 30.0]
    points[1, 1] = [100.0, 100.0, 100.0]
    points[3] = 1.0
    capture["data"]["points"] = points
    capture.write(str(path))

    track = load_mocap_frame_track(path)

    assert track.positions.shape == (3, 2, 3)
    np.testing.assert_allclose(track.positions[:, 1, 0], [0.01, 0.02, 0.03])
    assert track.point_names == ("WristM", "FingerIndex1")
    assert track.connections == ((0, 1),)


def test_nokov_zero_timestamp_rows_are_retained_but_marked_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "body.csv"
    path.write_text(
        "#Hierarchical Translation and Rotation (.csv) file\n"
        "[Head]\n"
        "NumFrames,NumSegments,DataFrameRate,EulerRotationOrder,BoneAxis,TranslationUnits\n"
        "2,1,240,ZYX,Z,mm\n\n"
        "[SegmentNames&Hierarchy]\n"
        "Segment,Parent\n"
        "Body,\n\n"
        "[SegmentData]\n"
        " Frame# ,,Body\n"
        ",Timestamp,XToGlobal1,YToGlobal1,ZToGlobal1\n"
        "1,0,0,0,0\n"
        "2,1000,1,2,3\n",
        encoding="utf-8",
    )

    track = load_mocap_frame_track(path)

    assert track.positions.shape == (2, 1, 3)
    assert track.valid_frame_mask.tolist() == [False, True]


def test_semantic_up_axis_can_select_x_axis_zero() -> None:
    positions = np.asarray(
        [
            [
                [2.0, 0.0, 0.0],
                [-2.0, 0.0, 0.0],
                [0.0, 0.0, 20.0],
                [0.0, 0.0, 20.0],
                [0.0, -10.0, 0.0],
                [0.0, 10.0, 0.0],
            ]
        ],
        dtype=np.float32,
    )

    marker_names = ("Head", "LeftHeel", "LeftHeel_1", "LeftHeel_2", "WideA", "WideB")
    assert _resolve_up_axis(marker_names, positions) == 0
