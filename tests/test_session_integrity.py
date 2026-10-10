import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing

import pytest

from robocap_rerun_tools import cli
from robocap_rerun_tools import file_integrity as content
from robocap_rerun_tools import mocap_integrity as mocap_checks
from robocap_rerun_tools import session_inspection as batch
from robocap_rerun_tools import session_integrity as layout
from robocap_rerun_tools.media_tools import activate_media_tools


def make_database(path, *, empty=(), omit=(), invalid=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tables = ("mag_data",) if "_mag_" in path.name else ("acc_data", "gyro_data")
    with closing(sqlite3.connect(path)) as connection:
        for table in tables:
            if table in omit:
                continue
            columns = content.SENSOR_COLUMNS[table]
            connection.execute(f"CREATE TABLE {table} ({','.join(columns)})")
            if table not in empty:
                connection.execute(
                    f"INSERT INTO {table} VALUES (?,?,?,?)",
                    (1000000000, None if invalid else 0, 0, 0),
                )
        connection.commit()
    return path


def pose_text(count=2, declared=2):
    return (
        "[Head]\nNumFrames,NumSegments,DataFrameRate\n"
        f"{declared},1,120\n[SegmentNames&Hierarchy]\nSegment,Parent\nBox,\n"
        "[SegmentData]\n,Timestamp,XToGlobal1,YToGlobal1,ZToGlobal1,\n"
        + "".join(f"{i + 1},{100 + i * 8},1,2,3,\n" for i in range(count))
    )


def trc_text(count=2, declared=2, xyz="1\t2\t3"):
    return (
        f"DataRate\tCameraRate\tNumFrames\tNumMarkers\n120\t120\t{declared}\t1\n"
        "Frame#\tTime\tTimestamp\tMarker1\t\t\n\t\t\tX1\tY1\tZ1\n\n"
        + "".join(f"{i + 1}\t{i / 120}\t{100 + i * 8}\t{xyz}\n" for i in range(count))
    )


def bvh_text(count=2, declared=2):
    return (
        "HIERARCHY\nROOT Hips\n{\nOFFSET 0 0 0\nCHANNELS 3 Xposition Yposition Zposition\n}\n"
        f"MOTION\nFrames: {declared}\nFrame Time: 0.008333\n" + "1 2 3\n" * count
    )


def full_session(tmp_path, monkeypatch, *, segments=(1,)):
    directory = tmp_path / "20261010_010000_session1"
    directory.mkdir(parents=True)
    for segment in segments:
        name = f"segment{segment}"
        for camera in layout.CAMERAS:
            (directory / f"robocap_{name}_video_{camera}.mp4").write_bytes(b"video")
        for side in ("left", "right"):
            make_database(directory / f"robocap_{name}_imu_{side}.db")
            wrist = directory / f"robowrist_device_{side}"
            wrist.mkdir(exist_ok=True)
            (wrist / f"robowrist_{name}_video_{side}_down.mp4").write_bytes(b"video")
            make_database(wrist / f"robowrist_{name}_imu_{side}.db")
            make_database(wrist / f"robowrist_{name}_mag_{side}.db")
        make_database(directory / f"robocap_{name}_mag_middle.db")
    mocap = directory / "mocap-P01-S01-person-1p"
    mocap.mkdir()
    (mocap / "Body0.csv").write_text(pose_text(), encoding="utf-8")
    (mocap / "Body0.trc").write_text(trc_text(), encoding="utf-8")
    (mocap / "Body0.bvh").write_text(bvh_text(), encoding="utf-8")
    (mocap / "Tracker0.csv").write_text(pose_text(), encoding="utf-8")
    (mocap / "Tracker0.trc").write_text(trc_text(), encoding="utf-8")
    (mocap / "third.mp4").write_bytes(b"third")
    rules = tmp_path / "mocap_requirements.csv"
    rules.write_text("action_id,additional_bodies,objects\nP01,,\n", encoding="utf-8")
    monkeypatch.setattr(mocap_checks, "DEFAULT_REQUIREMENTS", rules)
    monkeypatch.setattr(content, "inspect_video", lambda *args: content.result(frames=3))
    return directory


def audit(directory, **kwargs):
    return layout.audit_session(directory, progress=lambda _: None, **kwargs)


@pytest.mark.parametrize(
    ("suffix", "writer"), [(".trc", trc_text), (".csv", pose_text), (".bvh", bvh_text)]
)
@pytest.mark.parametrize(
    ("count", "declared", "state"),
    [
        (2, 2, "ok"),
        (0, 2, "empty_data"),
        (1, 2, "count_mismatch"),
        (3, 2, "count_mismatch"),
    ],
)
def test_motion_checks_actual_rows_not_only_headers(
    tmp_path, suffix, writer, count, declared, state
):
    path = tmp_path / ("Body0" + suffix)
    path.write_text(writer(count, declared), encoding="utf-8")
    checked = content.inspect_file(path)
    assert checked["state"] == state
    assert checked["rows"] == count


def test_xrs_double_tabs_and_gbk_text_are_supported(tmp_path):
    path = tmp_path / "Box.xrs"
    text = pose_text().replace(
        "NumFrames,NumSegments,DataFrameRate", "NumFrames\t\tNumSegments\t\tDataFrameRate"
    )
    text = text.replace("2,1,120", "2\t\t1\t\t120").replace(",", "\t")
    path.write_bytes(("# 导出路径\n" + text).encode("gb18030"))
    assert content.inspect_file(path)["state"] == "ok"


def test_trc_missing_coordinates_distinguish_empty_partial_and_malformed(tmp_path):
    path = tmp_path / "Marker.trc"
    path.write_text(trc_text(xyz="nan\tnan\tnan"), encoding="utf-8")
    assert content.inspect_file(path)["state"] == "empty_data"
    path.write_text(trc_text().replace("1\t2\t3", "nan\tnan\tnan", 1), encoding="utf-8")
    assert content.inspect_file(path)["state"] == "partial_data"
    path.write_text(trc_text(xyz="bad\t2\t3"), encoding="utf-8")
    assert content.inspect_file(path)["state"] == "invalid_format"
    path.write_text(trc_text().rstrip()[:-2], encoding="utf-8")
    assert content.inspect_file(path)["state"] == "invalid_format"


def test_trc_omitted_untracked_rows_and_inconsistent_extra_labels(tmp_path):
    path = tmp_path / "Tracker0.trc"
    path.write_text(trc_text().replace("1\t2\t3", "", 1), encoding="utf-8")
    checked = content.inspect_file(path)
    assert checked["state"] == "partial_data" and checked["rows"] == 2
    text = trc_text().replace("Marker1\t\t\n", "Marker1\t\t\tRCalc::Marker1\t\t\n")
    path.write_text(text, encoding="utf-8")
    checked = content.inspect_file(path)
    assert checked["state"] == "column_mismatch" and checked["rows"] == 2
    assert "header columns=9" in checked["detail"]


@pytest.mark.parametrize(
    ("kwargs", "state"),
    [
        ({}, "ok"),
        ({"empty": ("acc_data",)}, "empty_data"),
        ({"omit": ("gyro_data",)}, "schema_error"),
        ({"invalid": True}, "invalid_data"),
    ],
)
def test_database_requires_real_values_and_all_expected_tables(tmp_path, kwargs, state):
    path = make_database(tmp_path / "robocap_segment1_imu_left.db", **kwargs)
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert content.inspect_file(path)["state"] == state
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert list(tmp_path.iterdir()) == [path]


def test_active_database_and_broken_database_do_not_pass(tmp_path):
    path = make_database(tmp_path / "robowrist_segment1_imu_right.db")
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO acc_data VALUES (1000000001,1,2,3)")
        connection.commit()
        assert content.inspect_file(path)["state"] == "active_database"
        assert "Unmerged database log" in cli.sqlite_sensor_summaries(path)[0].abnormal_reason
    broken = tmp_path / "robocap_segment2_imu_left.db"
    broken.write_bytes(b"not a database")
    assert content.inspect_file(broken)["state"] == "invalid_format"
    assert not (tmp_path / "missing.db").exists()
    cli.sqlite_sensor_summaries(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_full_structure_requires_body0_three_formats_and_tracker0_pair(tmp_path, monkeypatch):
    directory = full_session(tmp_path, monkeypatch)
    info = audit(directory)
    assert info["status"] == "passed", [r for r in info["checks"] if r["state"] != "ok"]
    assert info["fileCount"] == 21
    assert info["segments"] == ["segment1"]


def test_unconfigured_objects_prevent_complete_pass_and_missing_objects_fail(tmp_path, monkeypatch):
    directory = full_session(tmp_path, monkeypatch)
    rules = tmp_path / "mocap_requirements.csv"
    rules.write_text("action_id,additional_bodies,objects\n", encoding="utf-8")
    info = audit(directory)
    assert info["status"] == "incomplete" and info["warnings"] == 1
    assert any(row["state"] == "requirements_unknown" for row in info["checks"])
    rules.write_text("action_id,additional_bodies,objects\nP01,,BigBox0\n", encoding="utf-8")
    info = audit(directory)
    assert info["status"] == "failed" and info["errors"] == 2
    assert {row["path"] for row in info["checks"] if row["state"] == "missing"} == {
        "mocap*/BigBox0.trc", "mocap*/BigBox0.csv",
    }


def test_complete_export_names_still_fail_for_empty_required_body(tmp_path, monkeypatch):
    directory = full_session(tmp_path, monkeypatch)
    (directory / "mocap-P01-S01-person-1p" / "Body0.bvh").write_bytes(b"")
    info = audit(directory)
    assert info["status"] == "failed"
    assert any(
        row["path"].endswith("Body0.bvh") and row["state"] == "zero_byte"
        for row in info["checks"]
    )


def test_missing_slot_cannot_be_hidden_by_another_segment(tmp_path, monkeypatch):
    directory = full_session(tmp_path, monkeypatch, segments=(1, 10))
    # A misplaced filename must not satisfy a root-level camera requirement.
    path = directory / "robocap_segment1_video_right_front.mp4"
    path.rename(directory / "mocap-P01-S01-person-1p" / path.name)
    info = audit(directory, segment="segment1")
    problems = [r for r in info["checks"] if r["state"] == "missing"]
    assert [r["path"] for r in problems] == ["robocap_segment1_video_right_front.mp4"]
    assert not any("segment10" in r["path"] for r in info["checks"])
    assert not cli.inspection_file_matches_segment(
        directory / "robocap_segment10_imu_left.db", "segment1"
    )


def test_multiple_devices_and_mocap_directories_are_explicitly_ambiguous(tmp_path, monkeypatch):
    directory = full_session(tmp_path, monkeypatch)
    (directory / "mocap-extra").mkdir()
    extra = directory / "robowrist_second_right"
    extra.mkdir()
    make_database(extra / "robowrist_segment1_imu_right.db")
    info = audit(directory)
    problems = [r for r in info["checks"] if r["state"] == "ambiguous"]
    assert any(r["path"] == "mocap*/" for r in problems)
    assert any(r["requirement"] == "腕部 IMU" for r in problems)


def test_each_file_failure_is_reported_without_hiding_other_results(tmp_path, monkeypatch):
    directory = full_session(tmp_path, monkeypatch)
    imu = directory / "robocap_segment1_imu_right.db"
    imu.write_bytes(b"corrupt")
    video = directory / "robocap_segment1_video_left.mp4"
    video.write_bytes(b"")
    extra = directory / "additional.abc"
    extra.write_bytes(b"unknown data")
    info = audit(directory)
    by_name = {r["path"]: r for r in info["checks"] if r["category"] == "content"}
    assert info["status"] == "failed"
    assert by_name[imu.name]["state"] == "invalid_format"
    assert by_name[video.name]["state"] == "zero_byte"
    assert by_name[extra.name]["state"] == "not_checked"
    assert by_name["robocap_segment1_imu_left.db"]["state"] == "ok"


def test_published_layout_and_manifest_check(tmp_path, monkeypatch):
    directory = full_session(tmp_path, monkeypatch)
    info = audit(directory, layout="published")
    assert {r["path"] for r in info["checks"] if r["state"] == "missing"} == {
        "manifest.json",
        "timestamp_anomaly_detail_table.html",
    }
    document = {
        "session_id": directory.name,
        "files": [
            {"packaged_as": "robocap_segment1_video_left.mp4", "packaged_bytes": 123456},
            {"packaged_as": "mocap/missing.trc", "packaged_bytes": 20},
            {"packaged_as": "../../outside.db", "packaged_bytes": 20},
        ],
    }
    (directory / "manifest.json").write_text(json.dumps(document), encoding="utf-8")
    checks = [r for r in audit(directory)["checks"] if r["category"] == "manifest"]
    assert {r["state"] for r in checks} == {"manifest_mismatch", "missing", "invalid_format"}
    assert any("Unsafe manifest path" in r["detail"] for r in checks)


def test_published_report_must_contain_readable_matching_session_data(tmp_path, monkeypatch):
    directory = full_session(tmp_path, monkeypatch)
    report = directory / "timestamp_anomaly_detail_table.html"
    report.write_bytes(b"")
    rows = [
        r
        for r in audit(directory, layout="published")["checks"]
        if r["path"] == report.name and r["category"] == "content"
    ]
    assert rows[0]["state"] == "zero_byte"
    report.write_text('const report={"session":"wrong"}; const eventTypes=[];', encoding="utf-8")
    rows = [
        r
        for r in audit(directory, layout="published")["checks"]
        if r["path"] == report.name and r["category"] == "content"
    ]
    assert rows[0]["state"] == "manifest_mismatch"


def test_motion_changes_during_inspection_cannot_be_passed(tmp_path, monkeypatch):
    path = tmp_path / "Body0.csv"
    path.write_text(pose_text(), encoding="utf-8")
    original = content.inspect_motion

    def modify(target):
        checked = original(target)
        with target.open("a", encoding="utf-8") as stream:
            stream.write("changed")
        return checked

    monkeypatch.setattr(content, "inspect_motion", modify)
    assert content.inspect_file(path)["state"] == "changed"


def test_c3d_contains_actual_tracked_points(tmp_path):
    import ezc3d
    import numpy as np

    path = tmp_path / "points.c3d"
    document = ezc3d.c3d()
    document["parameters"]["POINT"]["RATE"]["value"] = [120]
    document["parameters"]["POINT"]["LABELS"]["value"] = ["Box"]
    document["data"]["points"] = np.ones((4, 1, 2))
    document.write(str(path))
    checked = content.inspect_file(path)
    assert checked["state"] == "ok" and checked["rows"] == 2


def test_scan_deduplicates_roots_skips_artifacts_and_reports_missing_roots(tmp_path, monkeypatch):
    directory = full_session(tmp_path / "raw", monkeypatch)
    full_session(tmp_path / "raw" / "_modelscope_dataset", monkeypatch)
    paths, errors = layout.discover_sessions(
        [directory, tmp_path / "raw", directory, tmp_path / "missing"],
        tmp_path / "out",
        lambda _: None,
    )
    assert paths == [directory]
    assert len(errors) == 1


def test_valid_and_truncated_real_video(tmp_path):
    tools = activate_media_tools()
    if not tools.ffmpeg or not tools.ffprobe:
        pytest.skip("FFmpeg unavailable")
    path = tmp_path / "clip.mp4"
    subprocess.run(
        [
            tools.ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=32x32:r=30",
            "-t",
            "0.2",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    checked = content.inspect_file(path, ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe)
    assert checked["state"] == "ok" and checked["frames"] == 6
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(path.read_bytes()[:100])
    assert (
        content.inspect_file(broken, ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe)["state"]
        == "invalid_format"
    )
    assert content.inspect_file(path)["state"] == "tool_unavailable"
    irregular = tmp_path / "irregular.mp4"
    subprocess.run(
        [
            tools.ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=32x32:r=30",
            "-frames:v",
            "6",
            "-vf",
            r"setpts=PTS+gte(N\,3)*0.2/TB",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "mpeg4",
            str(irregular),
        ],
        check=True,
        capture_output=True,
    )
    checked = content.inspect_file(irregular, ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe)
    assert checked["state"] == "ok" and checked["frames"] == 6


def test_bvh_actual_count_integrates_into_original_timestamp_report(tmp_path, monkeypatch):
    source = tmp_path / "session"
    mocap = source / "mocap"
    mocap.mkdir(parents=True)
    (mocap / "Body0.bvh").write_text(bvh_text(count=1, declared=5), encoding="utf-8")
    report, integrity = cli.inspect_session_report(
        source, None, tmp_path / "report", progress=lambda _: None
    )
    document = report.read_text(encoding="utf-8")
    raw = document.split("const report=", 1)[1].split("; const eventTypes=", 1)[0]
    payload = json.loads(raw)
    assert payload["mocapFrames"] == 1
    assert payload["integrity"] == integrity
    assert any(row["state"] == "count_mismatch" for row in integrity["checks"])
    assert "renderIntegrity();" in document
    assert not (source / "_artifacts").exists()


def test_decoded_video_count_is_used_in_frame_comparison(tmp_path, monkeypatch):
    source = tmp_path / "session"
    source.mkdir()
    video = source / "robocap_segment1_video_left.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(content, "inspect_video", lambda *args: content.result(frames=6))
    fake_summary = cli.StreamSummary(video, "video", 99, 30, 0, 1, 33.33, 33.33, 33.33, 0, "")
    monkeypatch.setattr(cli, "summarize_path", lambda *args: [fake_summary])
    monkeypatch.setattr("robocap_rerun_tools.timestamp_anomaly.video_samples", lambda path: [])
    report, _ = cli.inspect_session_report(source, None, tmp_path / "out", progress=lambda _: None)
    document = report.read_text(encoding="utf-8")
    payload = json.loads(document.split("const report=", 1)[1].split("; const eventTypes=", 1)[0])
    assert payload["referenceFrames"] == 6


def test_batch_creates_logs_reports_and_preserves_sources(tmp_path, monkeypatch):
    directory = full_session(tmp_path / "raw", monkeypatch)
    # Exercise the report workflow with real motion/database parsing, avoiding fake MP4 parsing.
    original = cli.summarize_path
    monkeypatch.setattr(
        cli,
        "summarize_path",
        lambda path, ffprobe: [] if path.suffix == ".mp4" else original(path, ffprobe),
    )
    before = {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file()
    }
    output = tmp_path / "reports"
    assert batch.main(["--root", str(directory), "--output", str(output)]) == 0
    after = {
        p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file()
    }
    assert before == after
    for name in (
        "report.html",
        "run.log",
        "sessions.csv",
        "issues.csv",
        "summary.json",
        "scan_errors.csv",
    ):
        payload = (output / name).read_bytes()
        assert not payload.startswith(b"\xef\xbb\xbf")
        payload.decode("utf-8")
    assert json.loads((output / "summary.json").read_text(encoding="utf-8"))["all_passed"]
    with pytest.raises(SystemExit):
        batch.main(["--root", str(directory), "--output", str(output)])


def test_batch_empty_or_missing_root_is_not_a_success(tmp_path):
    assert batch.main(["--root", str(tmp_path / "missing"), "--output", str(tmp_path / "out")]) == 1
