import json
from pathlib import Path

from robocap_rerun_tools import dataset_statistics as statistics


def write_report(path: Path, **overrides: object) -> None:
    payload: dict[str, object] = {
        "ratio": 8,
        "referenceFrames": 9,
        "mocapFrames": 80,
        "thirdFrames": 10,
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
    from_path = tmp_path / "EgoMotionActions" / "A04" / "session04"
    from_path.mkdir(parents=True)
    from_mocap = tmp_path / "session03"
    (from_mocap / "mocap-b03-St-user").mkdir(parents=True)
    conflicting = tmp_path / "A04" / "session-conflict"
    (conflicting / "mocap-B03-St-user").mkdir(parents=True)

    assert statistics.infer_action_primitive(tmp_path, from_path) == "A04"
    assert statistics.infer_action_primitive(tmp_path, from_mocap) == "B03"
    assert (
        statistics.infer_action_primitive(tmp_path, conflicting) == statistics.UNASSIGNED_PRIMITIVE
    )


def test_report_frame_count_difference_ignores_other_inspection_issues() -> None:
    clean = {
        "ratio": 8,
        "referenceFrames": 9,
        "mocapFrames": 80,
        "thirdFrames": 10,
        "mocapDelta": 0,
        "thirdDelta": 0,
        "estimatedDropped": 7,
        "expectedDropped": 0,
        "droppedMatch": False,
        "abnormalDiffs": 11,
        "missingTimestamps": 3,
        "frameIssues": 5,
        "files": [{"stream": "broken (parse error: example)"}],
    }

    assert statistics.classify_frame_count_anomalies(clean) == ()
    assert statistics.classify_frame_count_anomalies(
        {**clean, "mocapFrames": 88, "thirdFrames": 9}
    ) == ("mocap_extra", "third_person_missing")
    assert statistics.classify_frame_count_anomalies(
        {**clean, "mocapFrames": 72, "thirdFrames": 11}
    ) == ("mocap_missing", "third_person_extra")
    assert statistics.report_has_frame_count_difference(clean) is False
    assert statistics.report_has_frame_count_difference({**clean, "mocapFrames": 72}) is True
    assert statistics.report_has_frame_count_difference({**clean, "thirdFrames": 9}) is True
    assert statistics.report_has_frame_count_difference({**clean, "ratio": 6}) is True
    assert statistics.report_has_frame_count_difference({**clean, "referenceFrames": None}) is True


def test_session_statistics_group_duration_categories_sum_to_total(
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
        mocapFrames=72,
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
    english = statistics.render_statistics_markdown(tmp_path, grouped, language="English")

    assert [item.primitive_id for item in grouped] == ["P01", "P02", "P03"]
    assert [item.unchecked_duration_s for item in grouped] == [0.0, 0.0, 10.0]
    assert [item.frame_difference_duration_s for item in grouped] == [0.0, 10.0, 0.0]
    assert [item.clean_duration_s for item in grouped] == [10.0, 0.0, 0.0]
    assert all(
        item.duration_s
        == item.unchecked_duration_s
        + item.frame_difference_duration_s
        + item.clean_duration_s
        for item in grouped
    )
    assert statistics.session_has_clean_frame_counts(rows[0]) is True
    assert statistics.session_has_clean_frame_counts(rows[1]) is False
    assert statistics.session_has_clean_frame_counts(rows[2]) is False
    assert statistics.session_frame_anomaly_labels(rows[0], language="中文") == ("正常",)
    assert statistics.session_frame_anomaly_labels(rows[1], language="English") == (
        "mocap missing",
    )
    assert "| 动作基元 | 未检查时长 | 差帧时长 | 无误时长 | 总时长 | Session 数 |" in markdown
    assert "- 未检查时长：**00:00:10.000**" in markdown
    assert "- 差帧时长：**00:00:10.000**" in markdown
    assert "- 无误时长：**00:00:10.000**" in markdown
    assert "未检查时长 + 差帧时长 + 无误时长 = 总时长" in markdown
    assert "Unchecked duration" in english
    assert "Frame-count-difference duration" in english
    assert "Error-free duration" in english
    assert "00:00:30.000" in markdown
    assert '"session-clean": "00:00:10.000"' in markdown
    assert "{Session: [异常s](正常, mocap多帧, mocap少帧, 第三人称多帧, 第三人称少帧)}" in markdown
    assert '"session-clean": ["正常"]' in markdown
    assert '"session-problem": ["mocap少帧"]' in markdown
    assert '"session-missing": ["未检查"]' in markdown
