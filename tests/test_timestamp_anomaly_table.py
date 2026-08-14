from scripts.build_timestamp_anomaly_table import (
    Sample,
    SourceData,
    build_session_payloads,
    estimated_dropped_frames,
    inspect_source,
    problem_text,
    write_html,
)


def test_inspect_source_classifies_every_point_level_problem() -> None:
    samples = [
        Sample(10, 0, "1.000", 1.000),
        Sample(11, 1, "1.004", 1.004),
        Sample(12, 2, "0", None, "zero"),
        Sample(13, 3, "1.012", 1.012),
        Sample(14, 4, "1.012", 1.012),
        Sample(15, 5, "1.010", 1.010),
        Sample(16, 7, "1.011", 1.011),
    ]
    source = SourceData(
        session="session",
        relative_path="motion.csv",
        kind="csv",
        stream="motion",
        frame_count=7,
        declared_fps=240.0,
        samples=samples,
        expected_timestamp_count=6,
        expected_missing_count=1,
        expected_diff_count=4,
        expected_skipped_diff_count=2,
    )

    result = inspect_source(source, file_index=0)

    assert result.abnormal_interval_count == 3
    assert result.missing_count == 1
    assert result.frame_index_issue_count == 1
    assert {event.event_type for event in result.events} == {
        "timestamp_missing",
        "timestamp_too_short",
        "timestamp_duplicate",
        "timestamp_reversed",
        "frame_index_gap",
    }
    missing = next(event for event in result.events if event.event_type == "timestamp_missing")
    assert missing.previous.source_row == 11
    assert missing.current.source_row == 12
    assert missing.following.source_row == 13
    assert "不会跨过该行" in problem_text(missing)
    timestamp_events = [
        event for event in result.events if event.event_type.startswith("timestamp_")
    ]
    assert not any(
        event.previous.source_row == 11 and event.current.source_row == 13
        for event in timestamp_events
    )


def test_video_uses_strict_30_fps_interval_baseline() -> None:
    samples = [
        Sample(None, 0, "0", 0.0),
        Sample(None, 1, "0.033", 0.033),
        Sample(None, 2, "0.067", 0.067),
        Sample(None, 3, "0.102", 0.102),
    ]
    source = SourceData(
        session="session",
        relative_path="third.mp4",
        kind="third_person_video",
        stream="third",
        frame_count=4,
        declared_fps=None,
        samples=samples,
        expected_timestamp_count=4,
        expected_missing_count=0,
        expected_diff_count=3,
        expected_skipped_diff_count=0,
    )

    result = inspect_source(source, file_index=0)

    assert result.abnormal_interval_count == 1
    assert len(result.events) == 1
    assert result.events[0].event_type == "timestamp_too_long"
    assert result.events[0].current.frame_index == 3
    assert "30 FPS" in problem_text(result.events[0])


def test_html_contains_session_data_without_external_assets(tmp_path) -> None:
    source = SourceData(
        session="session1",
        relative_path="motion.csv",
        kind="csv",
        stream="motion",
        frame_count=2,
        declared_fps=240.0,
        samples=[
            Sample(2, 0, "1.000", 1.000),
            Sample(3, 1, "1.010", 1.010),
        ],
        expected_timestamp_count=2,
        expected_missing_count=0,
        expected_diff_count=1,
        expected_skipped_diff_count=0,
    )
    result = inspect_source(source, file_index=0)
    payloads = build_session_payloads({"session1": [result]})
    output = tmp_path / "report.html"

    write_html(output, [{"session": "session1"}], len(result.events), payloads)

    document = output.read_text(encoding="utf-8")
    assert 'id="session-data-session1"' in document
    assert 'type="application/json"' in document
    assert "timestamp_anomaly_details.tsv" not in document
    assert "script.src" not in document
    assert "motion.csv" in document


def test_estimated_dropped_frames_uses_240_fps_period_multiples() -> None:
    source = SourceData(
        session="session",
        relative_path="Body0.csv",
        kind="csv",
        stream="Body0",
        frame_count=3,
        declared_fps=240.0,
        samples=[
            Sample(10, 0, "1.000", 1.000),
            Sample(11, 1, "1.008", 1.008),
            Sample(12, 2, "1.020", 1.020),
        ],
        expected_timestamp_count=3,
        expected_missing_count=0,
        expected_diff_count=2,
        expected_skipped_diff_count=0,
    )

    result = inspect_source(source, file_index=0)

    assert result.abnormal_interval_count == 2
    assert estimated_dropped_frames(result) == 3
