import csv
import json

import pytest

from robocap_rerun_tools import dataset_statistics as statistics
from robocap_rerun_tools.file_integrity import inspect_file
from robocap_rerun_tools.inspection_issues import CLASSIFICATION_VERSION, classify_inspection
from robocap_rerun_tools.session_integrity import INTEGRITY_VERSION, source_signature
from robocap_rerun_tools.statistics_report import generate_statistics_report
from robocap_rerun_tools.timestamp_anomaly import delimited_samples, trc_samples


def payload_for(checks=(), files=None):
    return {
        "referenceFrames": 9,
        "mocapFrames": 80,
        "thirdFrames": 10,
        "ratio": 8,
        "classificationVersion": CLASSIFICATION_VERSION,
        "integrity": {"version": INTEGRITY_VERSION, "checks": list(checks)},
        "files": files if files is not None else [{"path": "body.csv", "sampleCount": 80}],
    }


@pytest.mark.parametrize("prefix", ["", "Frame#,", ","])
@pytest.mark.parametrize("suffix,delimiter", [(".csv", ","), (".xrs", "\t")])
def test_content_and_report_read_same_timestamp_column(tmp_path, prefix, suffix, delimiter):
    path = tmp_path / ("Body0" + suffix)
    text = (
        prefix + "Timestamp,XToGlobal1,YToGlobal1,ZToGlobal1\n"
        "1,1000,1,2,3\n2,0,1,2,3\n3,1000,1,2,3\n4,996,1,2,3\n"
    )
    path.write_text(text.replace(",", delimiter), encoding="utf-8")
    checked = inspect_file(path)
    samples = delimited_samples(path, delimiter)
    assert checked["state"] == "timestamp_error"
    assert checked["timestamp_detail"]["timestamp_column"] == 2
    assert checked["timestamp_detail"]["missing_timestamps"] == 1
    assert checked["timestamp_detail"]["nonincreasing_timestamps"] == 2
    assert [sample.raw_timestamp for sample in samples] == ["1000", "0", "1000", "996"]
    assert samples[1].timestamp_s is None
    classified = classify_inspection(payload_for([{**checked, "path": path.name}]))
    assert {row["code"] for row in classified["issues"]} == {"timestamp_missing", "timestamp_order"}


def test_trc_absolute_timestamp_is_checked_even_when_relative_time_is_valid(tmp_path):
    path = tmp_path / "Body0.trc"
    path.write_text(
        "NumFrames\tNumMarkers\n2\t1\nFrame#\tTime\tTimestamp\tMarker1\t\t\n"
        "\t\t\tX1\tY1\tZ1\n1\t0\t0\t1\t2\t3\n2\t0.008\t108\t\t\t\n",
        encoding="utf-8",
    )
    checked = inspect_file(path)
    samples = trc_samples(path)
    assert checked["state"] == "timestamp_error"
    assert checked["missing_samples"] == 1
    assert samples[0].timestamp_s is None
    result = classify_inspection(payload_for([{**checked, "path": path.name}]))
    assert {row["code"] for row in result["issues"]} == {"timestamp_missing", "trajectory_missing"}


def test_standard_trc_relative_time_can_start_at_zero(tmp_path):
    path = tmp_path / "Body.trc"
    path.write_text(
        "Frame#\tTime\tMarker\t\t\n\t\tX1\tY1\tZ1\n1\t0\t1\t2\t3\n2\t0.008\t1\t2\t3\n",
        encoding="utf-8",
    )
    assert inspect_file(path)["state"] == "ok"
    assert [sample.timestamp_s for sample in trc_samples(path)] == [0.0, 0.008]


def test_frame_errors_content_errors_and_notes_are_independent():
    payload = payload_for(
        [
            {
                "path": "body.trc",
                "state": "column_mismatch",
                "missing_samples": 2,
                "timestamp_detail": {"missing_timestamps": 3},
                "warnings": ["已兼容的表头"],
            },
            {"path": "empty.trc", "state": "empty_data"},
            {"path": "absent.csv", "state": "missing"},
        ],
        [{"path": "body.trc", "missing": 3, "sampleCount": 80}],
    )
    payload.update(mocapFrames=81, thirdFrames=9)
    result = classify_inspection(payload)
    codes = [row["code"] for row in result["issues"]]
    assert result["status"] == "error"
    assert set(codes) == {
        "mocap_extra",
        "third_person_missing",
        "trajectory_missing",
        "format_error",
        "timestamp_missing",
        "empty_data",
        "file_missing",
        "format_note",
    }
    assert codes.count("timestamp_missing") == 1
    assert next(row for row in result["issues"] if row["code"] == "timestamp_missing")["count"] == 3


def test_compatible_header_is_info_and_intervals_are_review_not_missing_frames():
    payload = payload_for([{"path": "body.trc", "state": "ok", "warnings": ["冗余表头已兼容"]}])
    assert classify_inspection(payload)["status"] == "passed"
    payload["files"][0]["eventCounts"] = {"timestamp_too_long": 2}
    result = classify_inspection(payload)
    assert result["status"] == "review"
    assert {row["code"] for row in result["issues"]} == {"format_note", "timestamp_interval"}
    assert statistics.classify_frame_count_anomalies(payload) == ()


@pytest.mark.parametrize("change", ["old", "missing_integrity", "parse_error", "no_samples"])
def test_unverified_reports_are_never_clean(change):
    payload = payload_for()
    if change == "old":
        payload["integrity"]["version"] = INTEGRITY_VERSION - 1
    elif change == "missing_integrity":
        payload["integrity"] = None
    elif change == "parse_error":
        payload["files"][0]["parseError"] = "read failed"
    else:
        payload["files"][0]["sampleCount"] = 0
    assert classify_inspection(payload)["status"] == "unchecked"


@pytest.mark.parametrize(
    "checks",
    [["invalid row"], [{"state": ["invalid"]}], [{"state": "ok", "timestamp_detail": "bad"}]],
)
def test_malformed_optional_report_records_do_not_stop_statistics(checks):
    payload = payload_for()
    payload["files"] = None
    payload["integrity"]["checks"] = checks
    assert classify_inspection(payload)["status"] == "unchecked"


def write_fixture_report(session, segment, payload):
    report = statistics.timestamp_report_path(session, segment)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        "<script>const report="
        + json.dumps(payload, ensure_ascii=False)
        + "; const eventTypes=[];</script>",
        encoding="utf-8",
    )


def test_statistics_count_multiple_defects_once_per_session_and_export_evidence(
    tmp_path, monkeypatch
):
    session = tmp_path / "session1"
    session.mkdir()
    for segment in ("segment1", "segment2"):
        (session / f"robocap_{segment}_video_left.mp4").write_bytes(b"fixture")
    payload = payload_for(
        [
            {"path": "body.trc", "state": "partial_data", "missing_samples": 7},
            {
                "path": "body.csv",
                "state": "timestamp_error",
                "timestamp_detail": {"missing_timestamps": 3},
                "detail": "line=5, frame=2",
            },
        ]
    )
    payload["integrity"]["source_signature"] = source_signature(session)
    for segment in ("segment1", "segment2"):
        write_fixture_report(session, segment, payload)
    monkeypatch.setattr(statistics, "probe_video_duration", lambda *args: (10.0, None))
    summarized = statistics.summarize_session(tmp_path, session, "ffprobe")
    assert summarized.clean_duration_s == 20
    assert summarized.quality_status == "error"
    assert statistics.quality_durations([summarized]) == (0, 20, 0)
    assert not statistics.session_is_error_free(summarized)
    counts = statistics.quality_count_summary([summarized])
    assert counts == {"total": 1, "passed": 0, "error": 1, "review": 0, "unchecked": 0}
    kinds = {row["code"]: row for row in statistics.quality_type_summary([summarized])}
    assert kinds["timestamp_missing"]["sessions"] == 1
    assert kinds["trajectory_missing"]["duration_s"] == 20
    result = generate_statistics_report(tmp_path, [session], "ffprobe")
    with result.sessions_csv.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["inspection_status"] == "clean"
    assert rows[0]["quality_status"] == "error"
    with result.issues_csv.open(encoding="utf-8", newline="") as stream:
        issues = list(csv.DictReader(stream))
    assert len(issues) == 4
    assert any(row["path"] == "body.csv" and "frame=2" in row["detail"] for row in issues)
    assert rows[0]["error_free_duration_s"] == "0.0"
    assert rows[0]["error_duration_s"] == "20.0"
    markdown = statistics.render_statistics_markdown(
        tmp_path,
        statistics.aggregate_by_primitive([summarized]),
        language="中文",
    )
    assert "- 无误时长：**00:00:00.000**" in markdown
    assert "- 有错误时长：**00:00:20.000**" in markdown
    assert "轨迹点部分缺失" in markdown and "时间戳缺失或无效" in markdown
    assert "body.csv" in markdown and "frame=2" in markdown
    assert "综合质量统计" not in markdown and "帧数一致时长" not in markdown
    assert markdown.count("| 动作基元 |") == 1
    assert "问题分类" in result.html_path.read_text(encoding="utf-8")
    (session / "robocap_segment1_video_left.mp4").write_bytes(b"changed source")
    updated = statistics.summarize_session(tmp_path, session, "ffprobe")
    assert any(row["code"] == "inspection_stale" for row in updated.quality_issues)


@pytest.mark.parametrize(
    "finding, expected",
    [
        ("passed", (0, 0, 10)),
        ("format_note", (0, 0, 10)),
        ("missing_file", (0, 10, 0)),
        ("empty_file", (0, 10, 0)),
        ("timestamp", (0, 10, 0)),
        ("review", (10, 0, 0)),
        ("legacy", (10, 0, 0)),
        ("stale", (10, 0, 0)),
    ],
)
def test_unified_error_free_duration_and_upload_selection(tmp_path, monkeypatch, finding, expected):
    session = tmp_path / "session1"
    session.mkdir()
    (session / "robocap_segment1_video_left.mp4").write_bytes(b"fixture")
    payload = payload_for()
    payload["integrity"]["source_signature"] = source_signature(session)
    if finding in {"missing_file", "empty_file"}:
        payload["integrity"]["checks"] = [
            {
                "path": "body.csv",
                "state": "missing" if finding == "missing_file" else "empty_data",
            }
        ]
    elif finding == "timestamp":
        payload["files"][0]["eventCounts"] = {"timestamp_reversed": 1}
    elif finding == "review":
        payload["files"][0]["eventCounts"] = {"timestamp_too_long": 1}
    elif finding == "legacy":
        payload["integrity"]["version"] -= 1
    elif finding == "stale":
        payload["integrity"]["source_signature"] = "stale"
    elif finding == "format_note":
        payload["integrity"]["checks"] = [
            {
                "path": "body.csv",
                "state": "ok",
                "warnings": ["已兼容的导出格式"],
            }
        ]
    write_fixture_report(session, "segment1", payload)
    monkeypatch.setattr(statistics, "probe_video_duration", lambda *args: (10.0, None))
    summarized = statistics.summarize_session(tmp_path, session, "ffprobe")
    # Matching frames alone never establish error-free duration.
    assert statistics.session_has_clean_frame_counts(summarized)
    assert statistics.quality_durations([summarized]) == expected
    assert statistics.session_is_error_free(summarized) == (expected[2] > 0)
    assert statistics.session_count_summary((summarized,), language="中文")["无误"] == (
        expected[2] > 0
    )


def test_one_bad_segment_excludes_whole_session_without_double_counting_types(
    tmp_path, monkeypatch
):
    session = tmp_path / "session1"
    session.mkdir()
    for segment in ("segment1", "segment2"):
        (session / f"robocap_{segment}_video_left.mp4").write_bytes(b"fixture")
    clean = payload_for()
    clean["integrity"]["source_signature"] = source_signature(session)
    write_fixture_report(session, "segment1", clean)
    broken = payload_for(
        [
            {"path": "body.csv", "state": "missing"},
            {"path": "tracker.csv", "state": "missing"},
        ]
    )
    broken["integrity"]["source_signature"] = source_signature(session)
    broken.update(mocapFrames=72)
    write_fixture_report(session, "segment2", broken)
    monkeypatch.setattr(statistics, "probe_video_duration", lambda *args: (10.0, None))
    summarized = statistics.summarize_session(tmp_path, session, "ffprobe")
    assert statistics.quality_durations([summarized]) == (0, 20, 0)
    counts, durations = statistics.issue_summaries([summarized], language="中文")
    assert counts["缺文件或仅有占位文件"] == counts["mocap少帧"] == 1
    assert durations["缺文件或仅有占位文件"] == durations["mocap少帧"] == "00:00:10.000"
