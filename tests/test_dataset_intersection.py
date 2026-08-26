from __future__ import annotations

import sqlite3
from pathlib import Path

import ezc3d
import numpy as np
import pytest

from robocap_rerun_tools import dataset_intersection as intersection


def slice_tuple(value: intersection.FrameSlice) -> tuple[int, int, int]:
    return value.start, value.end_exclusive, value.count


def test_positive_offset_removes_leading_mocap_and_third_person_frames() -> None:
    robocap, mocap, third, alignment = intersection.calculate_aligned_frame_slices(
        robocap_count=3870,
        mocap_count=30960,
        third_person_count=3870,
        ratio=8,
        video_frame_offset=1,
    )

    assert slice_tuple(robocap) == (0, 3869, 3869)
    assert slice_tuple(mocap) == (8, 30960, 30952)
    assert third is not None
    assert slice_tuple(third) == (1, 3870, 3869)
    assert alignment.gt_frame_offset == 8


def test_negative_offset_removes_leading_robocap_frames() -> None:
    robocap, mocap, third, alignment = intersection.calculate_aligned_frame_slices(
        robocap_count=3870,
        mocap_count=30960,
        third_person_count=3870,
        ratio=8,
        video_frame_offset=-1,
    )

    assert slice_tuple(robocap) == (1, 3870, 3869)
    assert slice_tuple(mocap) == (0, 30952, 30952)
    assert third is not None
    assert slice_tuple(third) == (0, 3869, 3869)
    assert alignment.gt_frame_offset == -8


@pytest.mark.parametrize(
    ("suffix", "content", "expected_frames"),
    [
        (
            ".trc",
            (
                "DataRate\tNumFrames\n240\t5\nFrame#\tTime\tTimestamp\n\n"
                "1\t0.000\t1000\n2\t0.004\t1004\n3\t0.008\t1008\n"
                "4\t0.012\t1012\n5\t0.016\t1016\n"
            ),
            ["2", "3", "4"],
        ),
        (
            ".csv",
            (
                "#Hierarchical Translation and Rotation (.csv) file\n[Head]\n"
                "NumFrames,NumSegments,DataFrameRate\n5,1,240\n[SegmentData]\n"
                " Frame# ,,Body\n,Timestamp,XToGlobal1\n"
                "1,1000,1\n2,1004,2\n3,1008,3\n4,1012,4\n5,1016,5\n"
            ),
            ["2", "3", "4"],
        ),
        (
            ".xrs",
            (
                "# Hierarchical Translation and Rotation (.xrs) file\n[Head]\n"
                "NumFrames NumSegments DataFrameRate\n5 1 240\n[SegmentData]\n"
                "Frame# Body\nTimestamp XToGlobal1\n"
                "1 1000 1\n2 1004 2\n3 1008 3\n4 1012 4\n5 1016 5\n"
            ),
            ["2", "3", "4"],
        ),
        (
            ".bvh",
            (
                "HIERARCHY\nROOT Hips\n{\nOFFSET 0 0 0\nCHANNELS 3 Xposition "
                "Yposition Zposition\n}\nMOTION\nFrames: 5\nFrame Time: 0.004166667\n"
                "1 0 0\n2 0 0\n3 0 0\n4 0 0\n5 0 0\n"
            ),
            ["2", "3", "4"],
        ),
    ],
)
def test_crop_mocap_text_updates_count_and_keeps_source_frame_identity(
    tmp_path: Path,
    suffix: str,
    content: str,
    expected_frames: list[str],
) -> None:
    source = tmp_path / f"motion{suffix}"
    target = tmp_path / f"staged{suffix}"
    source.write_text(content, encoding="utf-8", newline="")

    written = intersection.crop_mocap_text(source, target, intersection.FrameSlice(1, 4, 5))

    assert written == 3
    assert intersection.mocap_frame_count(target) == 3
    if suffix == ".bvh":
        rows = target.read_text(encoding="utf-8").split("Frame Time:", 1)[1].splitlines()[1:]
        actual_frames = [row.split()[0] for row in rows if row.strip()]
    else:
        text = target.read_text(encoding="utf-8")
        if suffix in {".csv", ".xrs"}:
            text = text.split("[SegmentData]", 1)[1]
        else:
            text = text.split("Frame#", 1)[1]
        actual_frames = [
            row.split(",", 1)[0].split()[0] for row in text.splitlines() if row and row[0].isdigit()
        ]
    assert actual_frames == expected_frames
    assert not target.read_bytes().startswith(b"\xef\xbb\xbf")


def test_crop_sqlite_database_preserves_metadata_and_clips_every_timestamp_table(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sensor.db"
    target = tmp_path / "staged.db"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE acc_data (id INTEGER PRIMARY KEY, timestamp INTEGER, x REAL);
            CREATE TABLE gyro_data (id INTEGER PRIMARY KEY, timestamp INTEGER, x REAL);
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO metadata VALUES ('deviceid', 'abc');
            """
        )
        rows = [(1, 90, 1.0), (2, 100, 2.0), (3, 150, 3.0), (4, 200, 4.0)]
        connection.executemany("INSERT INTO acc_data VALUES (?, ?, ?)", rows)
        connection.executemany("INSERT INTO gyro_data VALUES (?, ?, ?)", rows)

    records = intersection.crop_sqlite_database(source, target, 100, 200)

    assert records["acc_data"]["source_rows"] == 4
    assert records["acc_data"]["staged_rows"] == 2
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT timestamp FROM acc_data ORDER BY id").fetchall() == [
            (100,),
            (150,),
        ]
        assert connection.execute("SELECT timestamp FROM gyro_data ORDER BY id").fetchall() == [
            (100,),
            (150,),
        ]
        assert connection.execute("SELECT value FROM metadata").fetchone() == ("abc",)


def test_crop_video_uses_frame_trim_without_30_fps_resampling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    target = tmp_path / "target.mp4"
    source.write_bytes(b"source")
    commands: list[list[str]] = []

    def fake_run(command: list[str], check: bool):
        assert check is True
        commands.append(command)
        Path(command[-1]).write_bytes(b"video")

    monkeypatch.setattr(intersection.subprocess, "run", fake_run)
    monkeypatch.setattr(
        intersection,
        "read_video_frame_timestamps_ns",
        lambda _path: (0, 33_000_000, 66_000_000),
    )

    written = intersection.crop_video(
        source,
        target,
        "ffmpeg",
        intersection.FrameSlice(2, 5, 8),
        raw_video=False,
        proxy_height=540,
        proxy_crf=28,
        proxy_bitrate="1400k",
        source_capture_start_ns=1_000_000_000,
        staged_capture_start_ns=1_066_000_000,
    )

    assert written == 3
    command = commands[0]
    video_filter = command[command.index("-vf") + 1]
    assert "trim=start_frame=2:end_frame=5" in video_filter
    assert "setpts=PTS-STARTPTS" in video_filter
    assert "scale=-2:540" in video_filter
    assert "fps=30" not in video_filter
    assert command[command.index("-metadata") + 1] == "comment=1066000"


def test_crop_c3d_preserves_source_frame_identity_and_crops_all_sample_axes(
    tmp_path: Path,
) -> None:
    ascii_source = tmp_path / "source.c3d"
    source = tmp_path / "中文动作.c3d"
    target = tmp_path / "暂存动作.c3d"
    capture = ezc3d.c3d()
    capture["header"]["points"]["first_frame"] = 11
    capture["parameters"]["POINT"]["RATE"]["value"] = np.asarray([240.0])
    capture["parameters"]["POINT"]["LABELS"]["value"] = ["marker"]
    capture["data"]["points"] = np.arange(20, dtype=float).reshape(4, 1, 5)
    capture.write(str(ascii_source))
    ascii_source.replace(source)

    written = intersection.crop_c3d(source, target, intersection.FrameSlice(1, 4, 5))

    staged = intersection._load_c3d(target)
    assert written == 3
    assert staged["data"]["points"].shape == (4, 1, 3)
    assert staged["header"]["points"]["first_frame"] == 12
    np.testing.assert_array_equal(
        staged["data"]["points"][:3], capture["data"]["points"][:3, ..., 1:4]
    )


def test_build_plan_rejects_unknown_mocap_timeline_format(tmp_path: Path) -> None:
    session = tmp_path / "session"
    mocap = session / "mocap"
    mocap.mkdir(parents=True)
    reference = session / "robocap_segment1_video_left.mp4"
    reference.write_bytes(b"video")
    unsupported = mocap / "motion.trb"
    unsupported.write_bytes(b"binary")

    with pytest.raises(intersection.DatasetIntersectionError, match=r"motion\.trb"):
        intersection.build_aligned_intersection_plan(
            session,
            [reference, unsupported],
            segment="segment1",
            ratio=8,
            video_frame_offset=1,
            ffprobe="ffprobe",
        )


def test_third_person_crop_updates_numeric_capture_start_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    mocap_dir = session / "mocap"
    mocap_dir.mkdir(parents=True)
    reference = session / "robocap_segment1_video_left.mp4"
    other_robocap = session / "robocap_segment1_video_right.mp4"
    motion = mocap_dir / "motion.trc"
    third_person = mocap_dir / "third.mp4"
    for video in (reference, other_robocap, third_person):
        video.write_bytes(b"video")
    motion.write_text("motion", encoding="utf-8")
    timestamps = (0, 33_000_000, 66_000_000)
    monkeypatch.setattr(intersection, "read_video_frame_timestamps_ns", lambda _path: timestamps)
    monkeypatch.setattr(intersection, "mocap_frame_count", lambda _path: 24)
    monkeypatch.setattr(
        intersection,
        "video_comment_us",
        lambda path, _ffprobe: (
            100_000 if path == reference else 99_999 if path == other_robocap else 200_000
        ),
    )

    plan = intersection.build_aligned_intersection_plan(
        session,
        [reference, other_robocap, motion, third_person],
        segment="segment1",
        ratio=8,
        video_frame_offset=1,
        ffprobe="ffprobe",
    )

    selection = plan.video_slice("mocap/third.mp4")
    assert slice_tuple(selection.frames) == (1, 3, 2)
    assert selection.source_capture_start_ns == 200_000_000
    assert selection.staged_capture_start_ns == 233_000_000
    other_selection = plan.video_slice(other_robocap.name)
    assert slice_tuple(other_selection.frames) == (0, 2, 2)
    assert other_selection.staged_capture_start_ns == 99_999_000
