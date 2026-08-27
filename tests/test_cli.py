import json
import sqlite3
import sys
from pathlib import Path

import pytest

from robocap_rerun_tools import cli, web_app
from robocap_rerun_tools.alignment import round_positive_ratio
from robocap_rerun_tools.cli import (
    FpsRecord,
    build_parser,
    choose_time_column,
    csv_summary,
    discover_files,
    estimate_frame_ratio,
    find_nokov_source,
    inspection_files,
    nearest_multiple_of_ten,
    resolve_ffprobe,
    sqlite_sensor_summaries,
    trc_summary,
    video_capture_start_s,
    video_stream_name,
    video_to_gt_frame,
)
from robocap_rerun_tools.data_packager import discover_package_files


def make_inspection_summary(
    path: Path | str,
    kind: str,
    frames: int | None,
    stream: str = "",
    fps: float | None = None,
) -> cli.StreamSummary:
    dt_ms = 1000.0 / fps if fps else None
    end_s = (frames - 1) / fps if frames is not None and fps else None
    return cli.StreamSummary(
        Path(path),
        kind,
        frames,
        fps,
        0.0,
        end_s,
        dt_ms,
        dt_ms,
        dt_ms,
        0,
        "",
        dropped_frames=0,
        stream=stream,
    )


def test_export_parser_accepts_frame_offset() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "export",
            "Z:/DATASETS/Frodobots/nokov/session",
            "--mode",
            "frame",
            "--ratio",
            "8",
            "--offset",
            "40",
            "--robocap-start-frame",
            "100",
            "--robocap-end-frame",
            "250",
        ]
    )
    assert args.mode == "frame"
    assert args.ratio == "8"
    assert args.offset == 40
    assert args.robocap_start_frame == 100
    assert args.robocap_end_frame == 250


def test_export_parser_accepts_negative_frame_offset() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "export",
            "Z:/DATASETS/Frodobots/nokov/session",
            "--mode",
            "frame",
            "--offset",
            "-5",
        ]
    )

    assert args.offset == -5


def test_export_parser_has_no_display_layout_option() -> None:
    parser = build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "command")
    export_parser = subparsers.choices["export"]

    assert "--display" not in export_parser._option_string_actions


def test_frame_alignment_ratio_defaults_to_auto() -> None:
    parser = build_parser()
    session = "Z:/DATASETS/Frodobots/nokov/session"

    assert parser.parse_args(["export", session]).ratio == "auto"
    assert parser.parse_args(["inspect-offset", session, "--offset", "0"]).ratio == "auto"
    assert (
        parser.parse_args(["sweep-offset", session, "--offset-min", "0", "--offset-max", "1"]).ratio
        == "auto"
    )


def test_inspection_mocap_ratio_defaults_to_8_and_accepts_4() -> None:
    parser = build_parser()
    session = "Z:/DATASETS/Frodobots/nokov/session"

    assert parser.parse_args(["inspect", session]).mocap_ratio == 8
    assert parser.parse_args(["inspect", session, "--mocap-ratio", "4"]).mocap_ratio == 4


def test_nearest_multiple_of_ten_uses_half_up_rounding() -> None:
    assert nearest_multiple_of_ten(234.9) == 230
    assert nearest_multiple_of_ten(235.0) == 240


def test_auto_ratio_rounds_quotient_to_nearest_positive_integer() -> None:
    estimate = estimate_frame_ratio(
        [
            FpsRecord("motion.trc", "trc", "gt", 80.0),
            FpsRecord("robocap_video.mp4", "video", "robocap", 30.0),
        ],
        "test",
    )

    assert estimate is not None
    assert abs(estimate.ratio_before_rounding - (8 / 3)) < 1e-9
    assert estimate.ratio == 3
    assert round_positive_ratio(2.49) == 2
    assert round_positive_ratio(2.5) == 3
    assert round_positive_ratio(0.4) == 1


def test_offset_is_measured_in_robocap_video_frames() -> None:
    assert video_to_gt_frame(video_frame=0, ratio=8.0, video_frame_offset=5) == 40
    assert video_to_gt_frame(video_frame=10, ratio=8.0, video_frame_offset=5) == 120


def test_offset_mapping_uses_source_script_rounding_order() -> None:
    assert video_to_gt_frame(video_frame=1, ratio=2.4, video_frame_offset=1) == 4


def test_export_auto_passes_live_ratio_to_exporter(tmp_path: Path, monkeypatch) -> None:
    estimate = estimate_frame_ratio(
        [
            FpsRecord("motion.trc", "trc", "gt", 60.0),
            FpsRecord("robocap_video.mp4", "video", "robocap", 30.0),
        ],
        "test live scan",
    )
    assert estimate is not None
    monkeypatch.setattr(cli, "resolve_session_auto_ratio", lambda *_args: estimate)
    parser = build_parser()
    args = parser.parse_args(
        [
            "export",
            str(tmp_path),
            "--segment",
            "segment1",
            "--mode",
            "frame",
            "--offset",
            "-5",
            "--robocap-start-frame",
            "10",
            "--robocap-end-frame",
            "20",
            "--interpolate-dropped-frames",
        ]
    )
    captured: list[str] = []

    from robocap_rerun_tools import exporter

    def fake_exporter_main() -> None:
        captured.extend(sys.argv[1:])

    monkeypatch.setattr(exporter, "main", fake_exporter_main)
    assert args.func(args) == 0

    ratio_index = captured.index("--gt-frame-ratio")
    assert captured[ratio_index + 1] == "2"
    offset_index = captured.index("--gt-video-frame-offset")
    assert captured[offset_index + 1] == "-5"
    assert "--gt-frame-offset" not in captured
    start_index = captured.index("--robocap-start-frame")
    end_index = captured.index("--robocap-end-frame")
    assert captured[start_index + 1] == "10"
    assert captured[end_index + 1] == "20"
    assert "--interpolate-dropped-frames" in captured


def test_export_parser_accepts_sensor_filters() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["export", "Z:/DATASETS/Frodobots/nokov/session", "--no-mag", "--no-imu"]
    )
    assert args.no_mag is True
    assert args.no_imu is True


def test_export_parser_accepts_dropped_frame_interpolation() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "export",
            "Z:/DATASETS/Frodobots/nokov/session",
            "--interpolate-dropped-frames",
        ]
    )

    assert args.interpolate_dropped_frames is True


def test_find_nokov_source_prefers_hand_bvh(tmp_path: Path) -> None:
    test_dir = tmp_path / "test1"
    test_dir.mkdir()
    body = test_dir / "sample-Body0_Left.trc"
    hand_bvh = test_dir / "sample-hand.bvh"
    body.write_text("", encoding="utf-8")
    hand_bvh.write_text("", encoding="utf-8")
    assert find_nokov_source(tmp_path, None) == hand_bvh


def test_find_nokov_source_reads_single_mocap_prefix_directory(tmp_path: Path) -> None:
    mocap_dir = tmp_path / "mocap_take01"
    mocap_dir.mkdir()
    hand_bvh = mocap_dir / "sample-hand.bvh"
    hand_bvh.write_text("", encoding="utf-8")

    assert find_nokov_source(tmp_path, None) == hand_bvh


def test_find_nokov_source_rejects_multiple_mocap_prefix_directories(tmp_path: Path) -> None:
    for name in ("mocap_left", "Mocap-Right"):
        mocap_dir = tmp_path / name
        mocap_dir.mkdir()
        (mocap_dir / "sample-hand.bvh").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match=r"Multiple mocap\* directories"):
        find_nokov_source(tmp_path, None)


def test_package_parser_defaults_to_compressed_video() -> None:
    parser = build_parser()
    args = parser.parse_args(["package-data", "Z:/DATASETS/Frodobots/nokov/session"])
    assert args.raw_video is False
    assert args.proxy_height == 540


def test_modelscope_stage_parser_defaults_to_compressed_video() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "modelscope-stage",
            "Z:/DATASETS/Frodobots/nokov/session",
            "--primitive-id",
            "P01",
        ]
    )
    assert args.raw_video is False
    assert args.proxy_height == 540
    assert args.refresh_inspection is False
    assert args.inspection_mocap_ratio == 8
    assert args.mocap_files is None
    assert args.rrd_files is None
    assert args.aligned_intersection is False
    assert args.ratio == "auto"
    assert args.offset == 0
    assert args.reference_video == "left"


def test_modelscope_stage_parser_accepts_aligned_intersection() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "modelscope-stage",
            "Z:/DATASETS/Frodobots/nokov/session",
            "--primitive-id",
            "P01",
            "--aligned-intersection",
            "--ratio",
            "8",
            "--offset",
            "-2",
            "--reference-video",
            "right",
        ]
    )

    assert args.aligned_intersection is True
    assert args.ratio == "8"
    assert args.offset == -2
    assert args.reference_video == "right"


def test_c3d_summary_supplies_gt_fps_for_auto_ratio(tmp_path: Path, monkeypatch) -> None:
    from robocap_rerun_tools import dataset_intersection

    path = tmp_path / "motion.c3d"
    path.write_bytes(b"c3d")
    monkeypatch.setattr(dataset_intersection, "c3d_metadata", lambda _path: (2400, 240.0, 1))

    summary = cli.c3d_summary(path)

    assert summary.kind == "c3d"
    assert summary.frame_count == 2400
    assert summary.fps == 240.0
    assert abs((summary.median_dt_ms or 0.0) - 1000 / 240) < 1e-12
    assert cli.infer_fps_source(Path("mocap_take01") / path.name, summary.kind) == "gt"


def test_modelscope_stage_parser_accepts_selected_rrd_files() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "modelscope-stage",
            "Z:/DATASETS/Frodobots/nokov/session",
            "--primitive-id",
            "P01",
            "--rrd-file",
            "_artifacts/segment1/inspection/frame.rrd",
            "--rrd-file",
            "_artifacts/segment1/inspection/time.rrd",
        ]
    )

    assert args.include_rrd is False
    assert args.rrd_files == [
        Path("_artifacts/segment1/inspection/frame.rrd"),
        Path("_artifacts/segment1/inspection/time.rrd"),
    ]


def test_modelscope_stage_parser_accepts_selected_mocap_files() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "modelscope-stage",
            "Z:/DATASETS/Frodobots/nokov/session",
            "--primitive-id",
            "P01",
            "--mocap-file",
            "Mocap-NOKOV/motion.trc",
            "--mocap-file",
            "Mocap-NOKOV/rigid-body.csv",
        ]
    )

    assert args.mocap_files == [
        Path("Mocap-NOKOV/motion.trc"),
        Path("Mocap-NOKOV/rigid-body.csv"),
    ]


def test_modelscope_upload_parser_uses_resumable_cache() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "modelscope-upload",
            "Z:/DATASETS/Frodobots/nokov/_modelscope_dataset",
            "--repo-id",
            "owner/egomocap",
        ]
    )
    assert args.use_cache is True
    assert args.create_if_missing is False
    assert args.visibility == "private"


def test_modelscope_upload_repo_id_defaults_to_env_configuration() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["modelscope-upload", "Z:/DATASETS/Frodobots/nokov/_modelscope_dataset"]
    )

    assert args.repo_id is None


def test_modelscope_auth_reports_missing_token_without_traceback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from robocap_rerun_tools import modelscope_publisher

    settings = modelscope_publisher.ModelScopeSettings(
        None, "https://modelscope.cn", tmp_path / ".env", "missing"
    )
    monkeypatch.setattr(modelscope_publisher, "load_modelscope_settings", lambda: settings)
    parser = build_parser()
    args = parser.parse_args(["modelscope-auth"])

    assert args.func(args) == 2
    captured = capsys.readouterr()
    assert "not configured" in captured.err
    assert "Traceback" not in captured.err


def test_web_parser_defaults_to_localhost() -> None:
    parser = build_parser()
    args = parser.parse_args(["web"])
    assert args.host == "127.0.0.1"
    assert args.port == 7860


def test_package_discovery_excludes_artifacts_by_default(tmp_path: Path) -> None:
    (tmp_path / "robocap_segment1_video_left.mp4").write_text("", encoding="utf-8")
    artifacts = tmp_path / "_artifacts" / "segment1" / "inspection"
    artifacts.mkdir(parents=True)
    (artifacts / "old.rrd").write_text("", encoding="utf-8")
    files = discover_package_files(tmp_path, "segment1", include_artifacts=False, include_rrd=False)
    assert [path.name for path in files] == ["robocap_segment1_video_left.mp4"]


def test_inspection_discovery_ignores_package_manifest(tmp_path: Path) -> None:
    (tmp_path / "manifest.tsv").write_text("source\tpackaged_as\n", encoding="utf-8")
    artifact_dir = tmp_path / "_artifacts" / "segment1" / "inspection"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "timestamp_anomaly_detail_table.html").write_text("", encoding="utf-8")
    (tmp_path / "data.trc").write_text("", encoding="utf-8")
    assert [path.name for path in discover_files(tmp_path)] == ["data.trc"]


def test_inspection_includes_sensor_databases_and_non_segment_third_person_video(
    tmp_path: Path,
) -> None:
    mocap_dir = tmp_path / "mocap"
    mocap_dir.mkdir()
    third_person = mocap_dir / "capture-1.mp4"
    third_person.write_bytes(b"")
    segment1_db = tmp_path / "robocap_segment1_imu_left.db"
    segment1_db.write_bytes(b"")
    segment2_db = tmp_path / "robocap_segment2_imu_left.db"
    segment2_db.write_bytes(b"")
    segment1_video = tmp_path / "robocap_segment1_video_left.mp4"
    segment1_video.write_bytes(b"")
    segment2_video = tmp_path / "robocap_segment2_video_left.mp4"
    segment2_video.write_bytes(b"")

    selected = inspection_files(tmp_path, "segment1")

    assert third_person in selected
    assert segment1_db in selected
    assert segment1_video in selected
    assert segment2_db not in selected
    assert segment2_video not in selected
    assert video_stream_name(third_person) == "third_person_video"
    assert video_stream_name(segment1_video) == "robocap_video"


def test_sqlite_sensor_summary_checks_each_imu_and_mag_table(tmp_path: Path) -> None:
    path = tmp_path / "robocap_segment1_sensors.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE acc_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                x INTEGER,
                y INTEGER,
                z INTEGER,
                timestamp INTEGER
            );
            CREATE TABLE gyro_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                x INTEGER,
                y INTEGER,
                z INTEGER,
                timestamp INTEGER
            );
            CREATE TABLE mag_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mag_x INTEGER,
                mag_y INTEGER,
                mag_z INTEGER,
                timestamp INTEGER
            );
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        regular = [1_000_000_000, 1_005_000_000, 1_010_000_000, 1_015_000_000]
        delayed = [1_000_000_000, 1_005_000_000, 1_010_000_000, 1_030_000_000]
        connection.executemany(
            "INSERT INTO acc_data (x, y, z, timestamp) VALUES (0, 0, 0, ?)",
            [(timestamp,) for timestamp in regular],
        )
        connection.executemany(
            "INSERT INTO gyro_data (x, y, z, timestamp) VALUES (0, 0, 0, ?)",
            [(timestamp,) for timestamp in delayed],
        )
        connection.executemany(
            "INSERT INTO mag_data (mag_x, mag_y, mag_z, timestamp) VALUES (0, 0, 0, ?)",
            [(timestamp,) for timestamp in regular],
        )

    summaries = sqlite_sensor_summaries(path)
    by_stream = {summary.stream: summary for summary in summaries}

    assert set(by_stream) == {"acc_data", "gyro_data", "mag_data"}
    assert by_stream["acc_data"].kind == "imu_acc"
    assert by_stream["gyro_data"].kind == "imu_gyro"
    assert by_stream["mag_data"].kind == "mag"
    assert by_stream["acc_data"].frame_count == 4
    assert abs((by_stream["acc_data"].fps or 0.0) - 200.0) < 1e-6
    assert by_stream["acc_data"].abnormal_count == 0
    assert by_stream["gyro_data"].abnormal_count == 1
    assert by_stream["gyro_data"].dropped_frames == 3
    assert "likely dropped-frame intervals" in by_stream["gyro_data"].abnormal_reason
    assert by_stream["acc_data"].time_basis.startswith("timestamp")


def test_summarize_times_counts_dropped_frames(tmp_path: Path) -> None:
    dropped_one = cli.summarize_times(
        tmp_path / "one.trc", "trc", [0.0, 0.0167, 0.0333, 0.0667, 0.0833, 0.1000]
    )
    dropped_two = cli.summarize_times(
        tmp_path / "two.trc", "trc", [0.0, 0.0167, 0.0333, 0.0833, 0.1000]
    )

    assert dropped_one.dropped_frames == 1
    assert dropped_one.abnormal_count == 1
    assert "likely dropped-frame intervals" in dropped_one.abnormal_reason
    assert "estimated 1 missing frames" in dropped_one.abnormal_reason
    assert dropped_two.dropped_frames == 2
    assert "estimated 2 missing frames" in dropped_two.abnormal_reason


def test_video_capture_start_uses_mp4_comment_microseconds() -> None:
    assert video_capture_start_s({"format": {"tags": {"comment": "18690178780"}}}) == (18_690.17878)
    assert video_capture_start_s({"format": {"tags": {}}}) is None


def test_video_summary_keeps_average_fps_and_checks_real_frame_intervals(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeAssetVideo:
        def __init__(self, path: Path):
            self.path = path

        def read_frame_timestamps_nanos(self):
            return [0, 33_000_000, 70_000_000, 100_000_000]

    class FakeRerun:
        AssetVideo = FakeAssetVideo

    monkeypatch.setitem(sys.modules, "rerun", FakeRerun)
    monkeypatch.setattr(
        cli,
        "ffprobe_video",
        lambda path, ffprobe: (
            {
                "streams": [{"avg_frame_rate": "30000/1001", "nb_frames": "4"}],
                "format": {"duration": "0.1", "tags": {"comment": "10000000"}},
            },
            None,
        ),
    )

    summary = cli.video_summary(tmp_path / "third-person.mp4", "ffprobe")

    assert abs((summary.fps or 0.0) - 30000 / 1001) < 1e-9
    assert abs((summary.median_dt_ms or 0.0) - 33.0) < 1e-9
    assert summary.start_s == 10.0
    assert summary.end_s == 10.1
    assert summary.stream == "third_person_video"
    assert summary.time_basis.startswith("capture_time")


def test_video_summary_reports_dropped_frames(tmp_path: Path, monkeypatch) -> None:
    class FakeAssetVideo:
        def __init__(self, path: Path):
            self.path = path

        def read_frame_timestamps_nanos(self):
            return [0, 33_000_000, 66_000_000, 99_000_000, 165_000_000, 198_000_000]

    class FakeRerun:
        AssetVideo = FakeAssetVideo

    monkeypatch.setitem(sys.modules, "rerun", FakeRerun)
    monkeypatch.setattr(
        cli,
        "ffprobe_video",
        lambda path, ffprobe: (
            {
                "streams": [{"avg_frame_rate": "30000/1001", "nb_frames": "6"}],
                "format": {"duration": "0.2", "tags": {"comment": "10000000"}},
            },
            None,
        ),
    )

    summary = cli.video_summary(tmp_path / "third-person.mp4", "ffprobe")

    assert summary.dropped_frames == 1
    assert "likely dropped-frame intervals" in summary.abnormal_reason


def test_choose_time_column_accepts_common_tracker_names() -> None:
    assert choose_time_column(["Frame", "Time (Seconds)", "X", "Y", "Z"]) == "Time (Seconds)"
    assert choose_time_column(["frame", "capture_time_ns", "value"]) == "capture_time_ns"


def test_resolve_ffprobe_from_ffmpeg_sibling(tmp_path: Path) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_text("", encoding="utf-8")
    ffprobe.write_text("", encoding="utf-8")
    assert resolve_ffprobe("missing-ffprobe", str(ffmpeg)) == str(ffprobe)


def test_ffprobe_video_requests_capture_comment_metadata(tmp_path: Path, monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(
        cli,
        "run_json",
        lambda command: captured.extend(command) or {"streams": [{}], "format": {}},
    )

    data, error = cli.ffprobe_video(tmp_path / "video.mp4", "ffprobe")

    assert data is not None
    assert error is None
    assert any("format_tags=comment" in argument for argument in captured)


def test_web_language_values_include_docs() -> None:
    assert "中文说明" in web_app.language_values("中文")["doc"]
    assert "Basic Workflow" in web_app.language_values("English")["doc"]
    assert "前移" in web_app.language_values("中文")["offset_help"]
    assert "后移" in web_app.language_values("中文")["offset_help"]
    assert "advances NOKOV/GT" in web_app.language_values("English")["offset_help"]
    assert "delays NOKOV/GT" in web_app.language_values("English")["offset_help"]


def test_web_default_offset_persists_and_synchronizes(tmp_path: Path) -> None:
    settings_path = tmp_path / "web_settings.json"

    assert web_app.load_default_offset(settings_path) == 5
    message, export_offset, inspect_offset = web_app.set_default_offset(-12, "中文", settings_path)

    assert "-12" in message
    assert export_offset == -12
    assert inspect_offset == -12
    assert web_app.load_default_offset(settings_path) == -12
    assert json.loads(settings_path.read_text(encoding="utf-8"))["default_offset"] == -12
    assert json.loads(settings_path.read_text(encoding="utf-8"))["offset_unit"] == (
        "robocap_video_frames"
    )
    assert not settings_path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_web_default_offset_ignores_invalid_settings(tmp_path: Path) -> None:
    settings_path = tmp_path / "web_settings.json"
    settings_path.write_text('{"default_offset": 1.5}', encoding="utf-8")

    assert web_app.load_default_offset(settings_path) == 5


def test_web_default_offset_migrates_legacy_gt_frames(tmp_path: Path) -> None:
    settings_path = tmp_path / "web_settings.json"
    settings_path.write_text('{"default_offset": 40}', encoding="utf-8")

    assert web_app.load_default_offset(settings_path) == 5
    migrated = json.loads(settings_path.read_text(encoding="utf-8"))
    assert migrated == {"default_offset": 5, "offset_unit": "robocap_video_frames"}


def test_web_viewer_port_uses_requested_available_port(monkeypatch) -> None:
    monkeypatch.setattr(web_app, "tcp_port_is_available", lambda port: port == 8080)

    port, message = web_app.choose_web_viewer_port(8080)

    assert port == 8080
    assert "requested" in message.lower()


def test_web_viewer_port_falls_back_when_requested_port_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(web_app, "tcp_port_is_available", lambda _port: False)
    monkeypatch.setattr(web_app, "available_tcp_port", lambda: 49160)

    port, message = web_app.choose_web_viewer_port(9090)

    assert port == 49160
    assert "9090" in message
    assert "49160" in message


def test_web_export_forwards_sensor_filters(tmp_path: Path, monkeypatch) -> None:
    captured: list[str] = []
    wrist_dir = tmp_path / "robowrist_device_left"
    wrist_dir.mkdir()
    (wrist_dir / "robowrist_segment1_video_left_down.mp4").write_bytes(b"")

    def fake_stream_cli(args: list[str]):
        captured.extend(args)
        yield "Done."
        return web_app.StreamCommandResult(0, "Done.", "Done.")

    monkeypatch.setattr(web_app, "stream_cli_command", fake_stream_cli)
    export_kwargs = {
        "session_dir": str(tmp_path),
        "segment": "segment1",
        "mode": "frame",
        "ratio": "auto",
        "offset": -5,
        "limit_robocap_frames": True,
        "robocap_start_frame": 10,
        "robocap_end_frame": 20,
        "save_path": "",
        "use_proxy": False,
        "interpolate_dropped_frames": True,
        "gt_dir": "",
        "selected_gt_files": [],
        "include_third_person": False,
        "third_person_video": "",
        "include_robowrist": True,
        "include_mag": False,
        "include_imu": False,
        "proxy_height": 540,
    }
    result = list(web_app.export_rrd(**export_kwargs))[-1]

    assert result == "Done."
    assert "--no-mag" in captured
    assert "--no-imu" in captured
    assert "--no-robowrist" not in captured
    assert "--interpolate-dropped-frames" in captured
    assert captured[captured.index("--retarget-model") + 1] == "none"
    assert "--mano-model-dir" not in captured
    assert captured[captured.index("--offset") + 1] == "-5"
    assert captured[captured.index("--robocap-start-frame") + 1] == "10"
    assert captured[captured.index("--robocap-end-frame") + 1] == "20"

    captured.clear()
    export_kwargs["include_robowrist"] = False
    list(web_app.export_rrd(**export_kwargs))
    assert "--no-robowrist" in captured

    captured.clear()
    export_kwargs["include_robowrist"] = True
    export_kwargs["limit_robocap_frames"] = False
    list(web_app.export_rrd(**export_kwargs))
    assert "--robocap-start-frame" not in captured
    assert "--robocap-end-frame" not in captured

    captured.clear()
    session_without_wrist = tmp_path / "without_wrist"
    session_without_wrist.mkdir()
    export_kwargs["session_dir"] = str(session_without_wrist)
    result = list(web_app.export_rrd(**export_kwargs))[-1]
    assert "--no-robowrist" in captured
    assert result.startswith("Robowrist: no matching video or sensor streams")


def test_hierarchical_csv_ignores_zero_timestamps_and_keeps_header_count(tmp_path: Path) -> None:
    path = tmp_path / "Tracker0.csv"
    content = (
        "#Hierarchical Translation and Rotation (.csv) file\n"
        "[Head]\n"
        "NumFrames,NumSegments,DataFrameRate\n"
        "6,1,250\n"
        "[SegmentData]\n"
        " Frame# ,,Segment1\n"
        ",Timestamp,XToGlobal1\n"
        "1,100,1.0\n"
        "2,104,2.0\n"
        "3,0,0.0\n"
        "4,0,0.0\n"
        "5,116,3.0\n"
        "6,120,4.0\n"
    )
    path.write_text(content, encoding="utf-8", newline="")

    summary = csv_summary(path)

    assert summary.frame_count == 6
    assert summary.dropped_frames == 2
    assert "2 rows have missing/zero Timestamp values" in summary.abnormal_reason
    assert "likely dropped-frame intervals" in summary.abnormal_reason


def test_hierarchical_nokov_csv_summary_uses_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "Tracker0.csv"
    path.write_text(
        "\n".join(
            [
                "#Hierarchical Translation and Rotation (.csv) file",
                "[Head]",
                "NumFrames,NumSegments,DataFrameRate",
                "3,1,90",
                "[SegmentData]",
                " Frame# ,,Segment1",
                ",Timestamp,XToGlobal1",
                "1,1785727304130,1.0",
                "2,1785727304141,2.0",
                "3,1785727304152,3.0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    summary = csv_summary(path)
    assert summary.frame_count == 3
    assert abs((summary.fps or 0) - 90.909) < 0.01


def test_expected_frame_counts_use_reference_robocap_n(tmp_path: Path) -> None:
    session = tmp_path / "session"
    (session / "mocap").mkdir(parents=True)
    summaries = [
        make_inspection_summary(
            session / "robocap_segment1_video_left.mp4", "video", 50, "robocap_video", 30.0
        ),
        make_inspection_summary(
            session / "robocap_segment1_video_right.mp4", "video", 50, "robocap_video", 30.0
        ),
        make_inspection_summary(session / "mocap" / "motion.trc", "trc", 408, "", 60.0),
        make_inspection_summary(
            session / "mocap" / "capture-1.mp4", "video", 51, "third_person_video", 30.0
        ),
    ]

    n = cli.reference_robocap_frame_count(session, summaries)

    assert n == 50
    assert cli.expected_frame_count("robocap", "video", "robocap_video", n) == 50
    assert cli.expected_frame_count("gt", "trc", "", n) == 408
    assert cli.expected_frame_count("gt", "trc", "", n, ratio=4) == 204
    assert cli.expected_frame_count("gt", "video", "third_person_video", n) == 51


def test_bvh_summary_marks_dropped_detection_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "motion.bvh"
    content = (
        "HIERARCHY\r\n"
        "ROOT root\r\n"
        "{\r\n"
        " OFFSET 0 0 0\r\n"
        " CHANNELS 3 Xposition Yposition Zposition\r\n"
        "}\r\n"
        "MOTION\r\n"
        "Frames: 3\r\n"
        "Frame Time: 0.0166667\r\n"
    )
    path.write_text(content, encoding="utf-8", newline="")

    summary = cli.bvh_summary(path)

    assert summary.frame_count == 3
    assert summary.dropped_frames is None
    assert summary.abnormal_reason == ""


def test_trc_summary_reports_dropped_frames(tmp_path: Path) -> None:
    path = tmp_path / "Tracker0.trc"
    content = (
        "PathFileType\t4\t(X/Y/Z)\tD:/Tracker0.trc\r\n"
        "DataRate\tCameraRate\tNumFrames\tNumMarkers\tUnits\r\n"
        "60\t60\t6\t1\tmm\r\n"
        "Frame#\tTime\tMarker1\t\t\r\n"
        "\t\tX1\tY1\tZ1\r\n"
        "1\t0.000\t1\t2\t3\r\n"
        "2\t0.0167\t4\t5\t6\r\n"
        "3\t0.0333\t7\t8\t9\r\n"
        "4\t0.0667\t10\t11\t12\r\n"
        "5\t0.0833\t13\t14\t15\r\n"
        "6\t0.1000\t16\t17\t18\r\n"
    )
    path.write_text(content, encoding="utf-8", newline="")

    summary = trc_summary(path)

    assert summary.frame_count == 6
    assert summary.dropped_frames == 1
    assert "likely dropped-frame intervals" in summary.abnormal_reason


def test_trc_summary_tolerates_gbk_path_metadata(tmp_path: Path) -> None:
    path = tmp_path / "Tracker0.trc"
    content = (
        "PathFileType\t4\t(X/Y/Z)\tD:/\u6d4b\u8bd5/Tracker0.trc\r\n"
        "DataRate\tCameraRate\tNumFrames\tNumMarkers\tUnits\r\n"
        "90\t90\t2\t1\tmm\r\n"
        "Frame#\tTime\tMarker1\t\t\r\n"
        "\t\tX1\tY1\tZ1\r\n"
        "1\t0.000\t1\t2\t3\r\n"
        "2\t0.011\t4\t5\t6\r\n"
    )
    path.write_bytes(content.encode("gbk"))

    summary = trc_summary(path)

    assert summary.frame_count == 2
    assert summary.abnormal_count == 0
