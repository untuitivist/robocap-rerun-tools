from pathlib import Path

from robocap_rerun_tools.cli import build_parser, choose_time_column, find_nokov_source, resolve_ffprobe
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
        ]
    )
    assert args.mode == "frame"
    assert args.ratio == "8"
    assert args.offset == 40


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


def test_choose_time_column_accepts_common_tracker_names() -> None:
    assert choose_time_column(["Frame", "Time (Seconds)", "X", "Y", "Z"]) == "Time (Seconds)"
    assert choose_time_column(["frame", "capture_time_ns", "value"]) == "capture_time_ns"


def test_resolve_ffprobe_from_ffmpeg_sibling(tmp_path: Path) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_text("", encoding="utf-8")
    ffprobe.write_text("", encoding="utf-8")
    assert resolve_ffprobe("missing-ffprobe", str(ffmpeg)) == str(ffprobe)
