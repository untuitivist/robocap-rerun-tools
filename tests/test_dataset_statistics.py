import json
from pathlib import Path

from robocap_rerun_tools import dataset_statistics as statistics


def write_report(path: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "mocapDelta": 0,
        "thirdDelta": 0,
        "estimatedDropped": 0,
        "expectedDropped": 0,
        "droppedMatch": True,
        "abnormalDiffs": 0,
        "missingTimestamps": 0,
        "frameIssues": 0,
        "files": [],
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<script>const report="
        + json.dumps(payload, separators=(",", ":"))
        + "; const eventTypes=[];</script>",
        encoding="utf-8",
    )


def test_discover_segment_references_uses_one_video_per_segment(tmp_path: Path) -> None:
    names = [
        "robocap_segment1_video_right.mp4",
        "robocap_segment1_video_left.mp4",
        "robocap_segment1_video_left_eye.mp4",
        "robocap_segment2_video_right.mp4",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"")

    references = statistics.discover_segment_references(tmp_path)

    assert [(item.segment, item.video_path.name) for item in references] == [
        ("segment1", "robocap_segment1_video_left.mp4"),
        ("segment2", "robocap_segment2_video_right.mp4"),
    ]


def test_infer_action_primitive_uses_path_and_mocap_directory(tmp_path: Path) -> None:
    from_path = tmp_path / "EgoMotionActions" / "P04" / "session04"
    from_path.mkdir(parents=True)
    from_mocap = tmp_path / "session03"
    (from_mocap / "mocap-P03-St-user").mkdir(parents=True)
    conflicting = tmp_path / "P04" / "session-conflict"
    (conflicting / "mocap-P03-St-user").mkdir(parents=True)

    assert statistics.infer_action_primitive(tmp_path, from_path) == "P04"
    assert statistics.infer_action_primitive(tmp_path, from_mocap) == "P03"
    assert (
        statistics.infer_action_primitive(tmp_path, conflicting) == statistics.UNASSIGNED_PRIMITIVE
    )


def test_report_problem_detection_covers_frame_and_timestamp_issues() -> None:
    clean = {
        "mocapDelta": 0,
        "thirdDelta": 0,
        "estimatedDropped": 0,
        "expectedDropped": 0,
        "droppedMatch": True,
        "abnormalDiffs": 0,
        "missingTimestamps": 0,
        "frameIssues": 0,
        "files": [],
    }

    assert statistics.report_has_frame_problem(clean) is False
    for key, value in (
        ("mocapDelta", -8),
        ("thirdDelta", -1),
        ("estimatedDropped", 1),
        ("abnormalDiffs", 1),
        ("missingTimestamps", 1),
        ("frameIssues", 1),
    ):
        assert statistics.report_has_frame_problem({**clean, key: value}) is True


def test_session_statistics_group_duration_and_unchecked_or_problem_time(
    tmp_path: Path, monkeypatch
) -> None:
    clean_session = tmp_path / "session-clean"
    problem_session = tmp_path / "session-problem"
    missing_session = tmp_path / "session-missing"
    for index, session in enumerate((clean_session, problem_session, missing_session), start=1):
        session.mkdir()
        (session / f"mocap-P0{index}-St-user").mkdir()
        (session / "robocap_segment1_video_left.mp4").write_bytes(b"")
    write_report(statistics.timestamp_report_path(clean_session, "segment1"))
    write_report(
        statistics.timestamp_report_path(problem_session, "segment1"),
        mocapDelta=-8,
        estimatedDropped=1,
    )
    monkeypatch.setattr(statistics, "probe_video_duration", lambda path, ffprobe: (10.0, None))

    rows = [
        statistics.summarize_session(tmp_path, session, "ffprobe")
        for session in (clean_session, problem_session, missing_session)
    ]
    grouped = statistics.aggregate_by_primitive(rows)
    markdown = statistics.render_statistics_markdown(tmp_path, grouped, language="中文")

    assert [item.primitive_id for item in grouped] == ["P01", "P02", "P03"]
    assert [item.unchecked_or_problem_duration_s for item in grouped] == [0.0, 10.0, 10.0]
    assert "未检查/差帧时长" in markdown
    assert "00:00:30.000" in markdown
    assert '"session-clean": "00:00:10.000"' in markdown
