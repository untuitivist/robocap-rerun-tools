import json
import sys
from pathlib import Path

from robocap_rerun_tools import web_app
from robocap_rerun_tools.cli import (
    build_parser,
    choose_time_column,
    csv_summary,
    discover_files,
    find_nokov_source,
    load_frame_ratio_estimate,
    nearest_multiple_of_ten,
    resolve_ffprobe,
    trc_summary,
    video_to_gt_frame,
)
from robocap_rerun_tools.data_packager import discover_package_files


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


def test_frame_alignment_ratio_defaults_to_auto() -> None:
    parser = build_parser()
    session = "Z:/DATASETS/Frodobots/nokov/session"

    assert parser.parse_args(["export", session]).ratio == "auto"
    assert parser.parse_args(["inspect-offset", session, "--offset", "0"]).ratio == "auto"
    assert (
        parser.parse_args(["sweep-offset", session, "--offset-min", "0", "--offset-max", "1"]).ratio
        == "auto"
    )


def write_test_fps_report(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Robocap/NOKOV inspection",
                "",
                "| file | kind | frames | fps | start_s | end_s | median_dt_ms | abnormal |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
                "| `nokov\\left.bvh` | bvh | 100 | 59.999880 | 0 | 1 | 16.667 | 0 |",
                "| `nokov\\left.trc` | trc | 100 | 58.823529 | 0 | 1 | 17.000 | 0 |",
                "| `nokov\\left.csv` | csv | 100 | 58.823668 | 0 | 1 | 17.000 | 0 |",
                "| `robocap_segment1_video_left.mp4` | video | 50 | 30.017021 | 0 | 1 | 33.314 | 0 |",
                "| `robocap_segment1_video_left_eye.mp4` | video | 40 | 21.923118 | 0 | 1 | 45.614 | 0 |",
                "| `robocap_segment1_video_right.mp4` | video | 50 | 30.017004 | 0 | 1 | 33.314 | 0 |",
                "",
                "## Abnormal intervals",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_auto_ratio_reads_markdown_means_and_rounds_to_tens(tmp_path: Path) -> None:
    report_path = tmp_path / "frame_rate_report.md"
    write_test_fps_report(report_path)

    estimate = load_frame_ratio_estimate(report_path)

    assert estimate is not None
    assert estimate.gt_sample_count == 3
    assert estimate.robocap_sample_count == 3
    assert abs(estimate.gt_fps_mean - 59.21569233333333) < 1e-9
    assert abs(estimate.robocap_fps_mean - 27.319047666666665) < 1e-9
    assert estimate.gt_fps_rounded_10 == 60
    assert estimate.robocap_fps_rounded_10 == 30
    assert estimate.ratio == 2.0


def test_nearest_multiple_of_ten_uses_half_up_rounding() -> None:
    assert nearest_multiple_of_ten(234.9) == 230
    assert nearest_multiple_of_ten(235.0) == 240


def test_offset_is_measured_in_robocap_video_frames() -> None:
    assert video_to_gt_frame(video_frame=0, ratio=8.0, video_frame_offset=5) == 40
    assert video_to_gt_frame(video_frame=10, ratio=8.0, video_frame_offset=5) == 120


def test_offset_mapping_uses_source_script_rounding_order() -> None:
    assert video_to_gt_frame(video_frame=1, ratio=2.4, video_frame_offset=1) == 4


def test_export_auto_passes_report_ratio_to_exporter(tmp_path: Path, monkeypatch) -> None:
    report_path = tmp_path / "_artifacts" / "segment1" / "inspection" / "frame_rate_report.md"
    write_test_fps_report(report_path)
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
        ]
    )
    captured: list[str] = []

    from robocap_rerun_tools import exporter

    def fake_exporter_main() -> None:
        captured.extend(sys.argv[1:])

    monkeypatch.setattr(exporter, "main", fake_exporter_main)
    assert args.func(args) == 0

    ratio_index = captured.index("--gt-frame-ratio")
    assert captured[ratio_index + 1] == "2.000000000"
    offset_index = captured.index("--gt-video-frame-offset")
    assert captured[offset_index + 1] == "-5"
    assert "--gt-frame-offset" not in captured
    start_index = captured.index("--robocap-start-frame")
    end_index = captured.index("--robocap-end-frame")
    assert captured[start_index + 1] == "10"
    assert captured[end_index + 1] == "20"


def test_export_parser_accepts_sensor_filters() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["export", "Z:/DATASETS/Frodobots/nokov/session", "--no-mag", "--no-imu"]
    )
    assert args.no_mag is True
    assert args.no_imu is True


def test_find_nokov_source_prefers_hand_bvh(tmp_path: Path) -> None:
    test_dir = tmp_path / "test1"
    test_dir.mkdir()
    body = test_dir / "sample-Body0_Left.trc"
    hand_bvh = test_dir / "sample-hand.bvh"
    body.write_text("", encoding="utf-8")
    hand_bvh.write_text("", encoding="utf-8")
    assert find_nokov_source(tmp_path, None) == hand_bvh


def test_package_parser_defaults_to_compressed_video() -> None:
    parser = build_parser()
    args = parser.parse_args(["package-data", "Z:/DATASETS/Frodobots/nokov/session"])
    assert args.raw_video is False
    assert args.proxy_height == 540


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
    (artifact_dir / "frame_rate_report.tsv").write_text("", encoding="utf-8")
    (tmp_path / "data.trc").write_text("", encoding="utf-8")
    assert [path.name for path in discover_files(tmp_path)] == ["data.trc"]


def test_choose_time_column_accepts_common_tracker_names() -> None:
    assert choose_time_column(["Frame", "Time (Seconds)", "X", "Y", "Z"]) == "Time (Seconds)"
    assert choose_time_column(["frame", "capture_time_ns", "value"]) == "capture_time_ns"


def test_resolve_ffprobe_from_ffmpeg_sibling(tmp_path: Path) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_text("", encoding="utf-8")
    ffprobe.write_text("", encoding="utf-8")
    assert resolve_ffprobe("missing-ffprobe", str(ffmpeg)) == str(ffprobe)


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

    def fake_run_cli(args: list[str]) -> str:
        captured.extend(args)
        return "Done."

    monkeypatch.setattr(web_app, "run_cli", fake_run_cli)
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
        "display": True,
        "gt_dir": "",
        "selected_gt_files": [],
        "retarget_model": "none",
        "include_third_person": False,
        "third_person_video": "",
        "include_robowrist": True,
        "include_mag": False,
        "include_imu": False,
        "mano_model_dir": "",
        "proxy_height": 540,
    }
    result = web_app.export_rrd(**export_kwargs)

    assert result == "Done."
    assert "--no-mag" in captured
    assert "--no-imu" in captured
    assert "--no-robowrist" not in captured
    assert captured[captured.index("--offset") + 1] == "-5"
    assert captured[captured.index("--robocap-start-frame") + 1] == "10"
    assert captured[captured.index("--robocap-end-frame") + 1] == "20"

    captured.clear()
    export_kwargs["limit_robocap_frames"] = False
    web_app.export_rrd(**export_kwargs)
    assert "--robocap-start-frame" not in captured
    assert "--robocap-end-frame" not in captured


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
