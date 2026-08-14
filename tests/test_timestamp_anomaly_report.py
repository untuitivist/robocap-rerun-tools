import json
import sqlite3
from pathlib import Path

from robocap_rerun_tools import cli
from robocap_rerun_tools.cli import StreamSummary
from robocap_rerun_tools.timestamp_anomaly import (
    MOCAP_EXPECTED_FPS,
    TimestampSample,
    delimited_samples,
    inspect_samples,
    sqlite_samples,
    write_timestamp_anomaly_report,
)


def make_summary(
    path: Path,
    kind: str,
    frames: int,
    fps: float,
    *,
    stream: str = "",
) -> StreamSummary:
    interval_ms = 1000.0 / fps
    return StreamSummary(
        path=path,
        kind=kind,
        frame_count=frames,
        fps=fps,
        start_s=0.0,
        end_s=(frames - 1) / fps,
        median_dt_ms=interval_ms,
        min_dt_ms=interval_ms,
        max_dt_ms=interval_ms,
        abnormal_count=0,
        abnormal_reason="",
        dropped_frames=0,
        stream=stream,
    )


def embedded_report(document: str) -> dict[str, object]:
    prefix = "const report="
    start = document.index(prefix) + len(prefix)
    end = document.index("; const eventTypes=", start)
    return json.loads(document[start:end])


def test_diff_only_uses_adjacent_valid_rows_and_estimates_dropped_frames() -> None:
    samples = [
        TimestampSample(10, 1, "1.000", 1.000),
        TimestampSample(11, 2, "1.004", 1.004),
        TimestampSample(12, 3, "", None, "empty"),
        TimestampSample(13, 4, "1.012", 1.012),
        TimestampSample(14, 5, "1.020", 1.020),
    ]

    result = inspect_samples(Path("motion.csv"), "motion.csv", "csv", "Body", 5, 240, samples)

    assert result.diff_count == 2
    assert result.skipped_diff_count == 2
    assert result.missing_timestamp_count == 1
    long_events = [event for event in result.events if event.event_type == "timestamp_too_long"]
    assert len(long_events) == 1
    assert long_events[0].previous is samples[3]
    assert long_events[0].current is samples[4]
    assert long_events[0].estimated_dropped_frames == 1
    assert all(
        not (event.previous is samples[1] and event.current is samples[3])
        for event in result.events
    )


def test_mocap_12ms_interval_estimates_two_dropped_frames() -> None:
    samples = [
        TimestampSample(1, 1, "1.000", 1.000),
        TimestampSample(2, 2, "1.012", 1.012),
    ]

    result = inspect_samples(Path("motion.csv"), "motion.csv", "csv", "Body", 2, 240, samples)

    assert result.abnormal_diff_count == 1
    assert result.events[0].estimated_dropped_frames == 2


def test_nokov_integer_timestamp_is_milliseconds_and_missing_row_is_not_bridged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "motion.csv"
    path.write_text(
        "Frame#,Timestamp,X\n1,1000,0\n2,1004,0\n3,0,0\n4,1012,0\n",
        encoding="utf-8",
        newline="\n",
    )

    samples = delimited_samples(path, ",")
    result = inspect_samples(path, path.name, "csv", "Body", 4, 240, samples)

    assert [sample.timestamp_s for sample in samples] == [1.0, 1.004, None, 1.012]
    assert result.diff_count == 1
    assert result.skipped_diff_count == 2
    assert result.abnormal_diff_count == 0


def test_sqlite_sensor_samples_preserve_null_rows(tmp_path: Path) -> None:
    path = tmp_path / "sensors.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE acc_data (frame_index INTEGER PRIMARY KEY, capture_time_ns INTEGER)"
        )
        connection.executemany(
            "INSERT INTO acc_data VALUES (?, ?)",
            [
                (1, 1_000_000_000),
                (2, 1_004_000_000),
                (3, None),
                (4, 1_012_000_000),
            ],
        )

    samples = sqlite_samples(path, "acc_data")

    assert [sample.timestamp_s for sample in samples] == [1.0, 1.004, None, 1.012]
    assert [sample.frame_index for sample in samples] == [1, 2, 3, 4]


def test_report_is_one_standalone_html_with_fixed_240fps_mocap_baseline(
    tmp_path: Path, monkeypatch
) -> None:
    session = tmp_path / "session"
    mocap_dir = session / "test1"
    mocap_dir.mkdir(parents=True)
    mocap = mocap_dir / "capture-Body.csv"
    mocap.write_text(
        "Frame#,Timestamp,X\n1,1000,0\n2,1004,0\n3,1012,0\n",
        encoding="utf-8",
        newline="\n",
    )
    video = session / "robocap_segment1_video_left.mp4"
    sensor_db = session / "robowrist_left.sqlite"
    with sqlite3.connect(sensor_db) as connection:
        connection.execute(
            "CREATE TABLE acc_data (frame_index INTEGER PRIMARY KEY, capture_time_ns INTEGER)"
        )
        connection.executemany(
            "INSERT INTO acc_data VALUES (?, ?)",
            [(1, 1_000_000_000), (2, 1_005_000_000), (3, 1_010_000_000)],
        )
    summaries = [
        make_summary(video, "video", 50, 30.053, stream="robocap_video"),
        make_summary(mocap, "csv", 400, 239.7, stream="Body"),
        make_summary(sensor_db, "imu_acc", 3, 200.0, stream="acc_data"),
    ]
    monkeypatch.setattr(
        "robocap_rerun_tools.timestamp_anomaly.video_samples",
        lambda _path: [
            TimestampSample(None, index, str(index), index / 30.0) for index in range(50)
        ],
    )
    out_dir = tmp_path / "inspection"

    output = write_timestamp_anomaly_report(session, "segment1", summaries, out_dir, None)

    assert output.name == "timestamp_anomaly_detail_table.html"
    assert [path.name for path in out_dir.iterdir()] == [output.name]
    raw = output.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    document = raw.decode("utf-8")
    assert "<script src=" not in document
    assert "frame_rate_report" not in document
    assert "固定基线：动捕 240 FPS、视频 30 FPS" in document
    report = embedded_report(document)
    assert report["ratio"] == 8
    assert report["expectedMocap"] == 408
    assert report["mocapExpectedFps"] == MOCAP_EXPECTED_FPS
    assert {file["kind"] for file in report["files"]} == {
        "robocap_video",
        "csv",
        "imu_acc",
    }


def test_inspect_command_writes_no_legacy_markdown_or_tsv(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "session"
    session.mkdir()
    mocap = session / "motion.csv"
    mocap.write_text(
        "Frame#,Timestamp,X\n1,1000,0\n2,1004,0\n",
        encoding="utf-8",
        newline="\n",
    )
    summary = make_summary(mocap, "csv", 2, 240.0, stream="Body")
    monkeypatch.setattr(cli, "inspection_files", lambda *_args: [mocap])
    monkeypatch.setattr(cli, "summarize_path", lambda *_args: [summary])
    output_dir = tmp_path / "inspection"
    args = cli.build_parser().parse_args(
        ["inspect", str(session), "--segment", "segment1", "--output", str(output_dir)]
    )

    assert args.func(args) == 0

    assert [path.name for path in output_dir.iterdir()] == ["timestamp_anomaly_detail_table.html"]
