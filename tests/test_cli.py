from pathlib import Path

from robocap_rerun_tools.cli import build_parser, find_nokov_source


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

