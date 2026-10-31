from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import errno
import pytest

from robocap_rerun_tools import mocap_recovery as recovery


def _utc_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)


def _session(root: Path, name: str) -> Path:
    session = root / name
    session.mkdir(parents=True)
    (session / "robocap_segment1_video_left.mp4").write_bytes(b"video")
    return session


def test_recovery_plan_moves_exports_and_preserves_recording_directory(
    tmp_path: Path, monkeypatch
) -> None:
    dataset_root = tmp_path / "dataset"
    source_root = tmp_path / "exports"
    dataset_root.mkdir()
    source_root.mkdir()
    missing = _session(dataset_root, "20260914_100000_session1")
    missing_report = (
        missing / "_artifacts" / "segment1" / "inspection" / recovery.TIMESTAMP_REPORT_NAME
    )
    missing_report.parent.mkdir(parents=True)
    missing_report.write_text("stale", encoding="utf-8")
    empty = _session(dataset_root, "20260914_101000_session2")
    existing_empty = empty / "mocap-empty"
    existing_empty.mkdir()
    valid = _session(dataset_root, "20260914_102000_session3")
    (valid / "mocap-valid").mkdir()
    (valid / "mocap-valid" / "existing.bvh").write_text("data", encoding="utf-8")
    unparseable = _session(dataset_root, "session-without-time")
    ambiguous = _session(dataset_root, "20260914_103000_session4")
    (ambiguous / "mocap-a").mkdir()
    (ambiguous / "mocap-b").mkdir()

    first = source_root / "mocap-first"
    second = source_root / "day" / "Mocap-second"
    unused = source_root / "mocap-unused"
    invalid = source_root / "other" / "mocap-no-motion"
    for candidate in (first, second, unused, invalid):
        candidate.mkdir(parents=True)
    (first / "body.trc").write_text("first", encoding="utf-8")
    (first / "camera.MP4").write_bytes(b"video")
    (first / "recording.raw").write_bytes(b"original")
    (first / "project.xrs").write_bytes(b"project")
    (second / "nested").mkdir()
    (second / "nested" / "body.CSV").write_text("second", encoding="utf-8")
    (unused / "body.bvh").write_text("unused", encoding="utf-8")
    (invalid / "notes.txt").write_text("invalid", encoding="utf-8")
    creation_times = {
        "mocap-first": _utc_datetime("20260914_100100"),
        "Mocap-second": _utc_datetime("20260914_100900"),
        "mocap-unused": _utc_datetime("20260914_120000"),
    }
    monkeypatch.setattr(
        recovery,
        "candidate_creation_time",
        lambda path: creation_times[path.name],
    )

    plan = recovery.build_recovery_plan(
        dataset_root,
        source_root,
        [missing, empty, valid, unparseable, ambiguous],
    )

    assert [(match.target.session_dir, match.candidate.path.name) for match in plan.matches] == [
        (missing, "mocap-first"),
        (empty, "Mocap-second"),
        (ambiguous, "mocap-unused"),
    ]
    assert [match.delta_seconds for match in plan.matches] == [60.0, 60.0, 5_400.0]
    assert plan.unparseable_sessions == (unparseable,)
    assert plan.unmatched_sessions == ()
    assert plan.unmatched_candidates == ()
    assert plan.candidates_without_motion == (invalid.resolve(),)

    destinations = [recovery.move_recovery_match(match) for match in plan.matches]

    assert destinations == [
        missing / "mocap-first",
        empty / "Mocap-second",
        ambiguous / "mocap-unused",
    ]
    assert (missing / "mocap-first" / "body.trc").read_text(encoding="utf-8") == "first"
    assert not missing_report.exists()
    assert existing_empty.is_dir()
    assert (empty / "Mocap-second" / "nested" / "body.CSV").read_text(
        encoding="utf-8"
    ) == "second"
    assert not (first / "body.trc").exists()
    assert not (second / "nested" / "body.CSV").exists()
    assert (second / "nested").is_dir()
    assert (first / "camera.MP4").read_bytes() == b"video"
    assert (destinations[0] / "camera.MP4").read_bytes() == b"video"
    assert (first / "recording.raw").read_bytes() == b"original"
    assert (first / "project.xrs").read_bytes() == b"project"
    assert not (destinations[0] / "recording.raw").exists()
    assert not (destinations[0] / "project.xrs").exists()
    assert (ambiguous / "mocap-a").is_dir()
    assert (ambiguous / "mocap-b").is_dir()
    assert (ambiguous / "mocap-unused" / "body.bvh").is_file()


def test_recovery_copy_replaces_existing_directory_with_different_name(tmp_path: Path) -> None:
    source = tmp_path / "source" / "mocap-candidate"
    destination = tmp_path / "session" / "mocap-existing"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / "body.trc").write_text("new", encoding="utf-8")
    (destination / "body.trc").write_text("old", encoding="utf-8")
    target = recovery.RecoveryTarget(
        tmp_path / "session",
        _utc_datetime("20260914_100000"),
        (destination,),
    )
    match = recovery.RecoveryMatch(
        target,
        recovery.MocapCandidate(source, _utc_datetime("20260914_100000")),
        0.0,
    )

    copied = recovery.move_recovery_match(match)

    assert copied == tmp_path / "session" / "mocap-candidate"
    assert (destination / "body.trc").read_text() == "old"
    assert (copied / "body.trc").read_text(encoding="utf-8") == "new"


def test_recovery_plan_skips_same_name_and_includes_frame_difference(
    tmp_path: Path, monkeypatch
) -> None:
    dataset_root = tmp_path / "dataset"
    source_root = tmp_path / "exports"
    dataset_root.mkdir()
    source_root.mkdir()
    same = _session(dataset_root, "20260914_100000_session1")
    same_mocap = same / "mocap-same"
    same_mocap.mkdir()
    (same_mocap / "old.trc").write_text("old", encoding="utf-8")
    same_report = same / "_artifacts" / "segment1" / "inspection" / recovery.TIMESTAMP_REPORT_NAME
    same_report.parent.mkdir(parents=True)
    same_report.write_text("valid", encoding="utf-8")
    different = _session(dataset_root, "20260914_101000_session2")
    different_mocap = different / "mocap-old"
    different_mocap.mkdir()
    (different_mocap / "old.bvh").write_text("old", encoding="utf-8")

    same_candidate = source_root / "mocap-same"
    new_candidate = source_root / "mocap-new"
    for candidate in (same_candidate, new_candidate):
        candidate.mkdir()
        (candidate / "body.csv").write_text("new", encoding="utf-8")
    creation_times = {
        "mocap-same": _utc_datetime("20260914_100001"),
        "mocap-new": _utc_datetime("20260914_101001"),
    }
    monkeypatch.setattr(recovery, "candidate_creation_time", lambda path: creation_times[path.name])
    monkeypatch.setattr(recovery, "session_has_frame_count_difference", lambda _path: True)

    plan = recovery.build_recovery_plan(dataset_root, source_root, [same, different])

    assert plan.skipped_same_name == ()
    assert plan.matches[0].target.report_paths == (same_report,)
    assert [(item.target.session_dir, item.candidate.path.name) for item in plan.matches] == [
        (same, "mocap-same"),
        (different, "mocap-new")
    ]
    assert same_report.is_file()


def test_recovery_copy_replaces_all_existing_mocap_directories(tmp_path: Path) -> None:
    session = tmp_path / "session"
    first = session / "mocap-old-a"
    second = session / "mocap-old-b"
    candidate = tmp_path / "source" / "mocap-new"
    for directory in (first, second, candidate):
        directory.mkdir(parents=True)
    (first / "a.trc").write_text("a", encoding="utf-8")
    (second / "b.bvh").write_text("b", encoding="utf-8")
    (candidate / "new.csv").write_text("new", encoding="utf-8")
    report = session / "_artifacts" / "segment1" / "inspection" / recovery.TIMESTAMP_REPORT_NAME
    report.parent.mkdir(parents=True)
    report.write_text("stale", encoding="utf-8")
    match = recovery.RecoveryMatch(
        recovery.RecoveryTarget(
            session,
            _utc_datetime("20260914_100000"),
            (first, second),
            (report,),
        ),
        recovery.MocapCandidate(candidate, _utc_datetime("20260914_100000")),
        0.0,
    )

    events: list[tuple[str, int, int | None, str]] = []
    copied = recovery.move_recovery_match(
        match,
        lambda stage, current, total, detail: events.append(
            (stage, current, total, detail)
        ),
    )

    assert copied == session / "mocap-new"
    assert (first / "a.trc").read_text() == "a"
    assert (second / "b.bvh").read_text() == "b"
    assert (copied / "new.csv").read_text(encoding="utf-8") == "new"
    assert not report.exists()
    copy_events = [event for event in events if event[0] == "move_bytes"]
    assert copy_events[0][1:3] == (0, 3)
    assert copy_events[-1][1:3] == (3, 3)


def test_recovery_copy_restores_all_existing_directories_on_copy_failure(
    tmp_path: Path, monkeypatch
) -> None:
    session = tmp_path / "session"
    first = session / "mocap-old-a"
    second = session / "mocap-old-b"
    candidate = tmp_path / "source" / "mocap-new"
    for directory in (first, second, candidate):
        directory.mkdir(parents=True)
    (first / "a.trc").write_text("a", encoding="utf-8")
    (second / "b.bvh").write_text("b", encoding="utf-8")
    (candidate / "new.csv").write_text("new", encoding="utf-8")
    (candidate / "second.trc").write_text("second", encoding="utf-8")
    report = session / "_artifacts" / "segment1" / "inspection" / recovery.TIMESTAMP_REPORT_NAME
    report.parent.mkdir(parents=True)
    report.write_text("stale", encoding="utf-8")
    match = recovery.RecoveryMatch(
        recovery.RecoveryTarget(
            session,
            _utc_datetime("20260914_100000"),
            (first, second),
            (report,),
        ),
        recovery.MocapCandidate(candidate, _utc_datetime("20260914_100000")),
        0.0,
    )

    real_move = recovery.shutil.move

    def fail_move(source, destination, **kwargs):
        if Path(source) == candidate / "second.trc":
            raise OSError("move failed")
        return real_move(source, destination, **kwargs)

    monkeypatch.setattr(recovery.shutil, "move", fail_move)

    try:
        recovery.move_recovery_match(match)
    except OSError as exc:
        assert str(exc) == "move failed"
    else:
        raise AssertionError("move_recovery_match should propagate move failure")
    assert (candidate / "new.csv").read_text() == "new"
    assert (candidate / "second.trc").read_text() == "second"
    assert (first / "a.trc").read_text(encoding="utf-8") == "a"
    assert (second / "b.bvh").read_text(encoding="utf-8") == "b"
    assert list((session / "mocap-new").iterdir()) == []
    assert report.read_text(encoding="utf-8") == "stale"


def test_session_utc_matches_creation_time_in_utc_plus_8(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "20260914_100000_session1"
    candidate = tmp_path / "mocap-candidate"
    candidate.mkdir()
    east_8 = timezone(timedelta(hours=8))
    creation_timestamp = datetime(2026, 9, 14, 18, 0, tzinfo=east_8).timestamp()

    class Stat:
        st_birthtime = creation_timestamp
        st_ctime = creation_timestamp

    monkeypatch.setattr(Path, "stat", lambda _self: Stat())

    session_time = recovery.parse_session_timestamp(session)
    creation_time = recovery.candidate_creation_time(candidate)

    assert session_time == datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
    assert creation_time.isoformat() == "2026-09-14T18:00:00+08:00"
    assert (session_time - creation_time).total_seconds() == 0


@pytest.mark.parametrize("fail_second", [False, True])
def test_move_merges_existing_directory_and_restores_overwrites(tmp_path, monkeypatch, fail_second):
    source = tmp_path / "recording" / "mocap-new"
    session = tmp_path / "session"
    destination = session / "mocap-new"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / "a.trc").write_bytes(b"new")
    (source / "b.csv").write_bytes(b"csv")
    (destination / "a.trc").write_bytes(b"old")
    (destination / "keep.mp4").write_bytes(b"keep")
    (source / "a0.MP4").write_bytes(b"new video")
    (destination / "a0.MP4").write_bytes(b"old video")
    match = recovery.RecoveryMatch(
        recovery.RecoveryTarget(session, _utc_datetime("20260914_100000"), (destination,)),
        recovery.MocapCandidate(source, _utc_datetime("20260914_100000")), 0.0,
    )
    real_move = recovery.shutil.move

    def move(src, dst, **kwargs):
        if fail_second and Path(src) == source / "b.csv":
            raise OSError("failed second file")
        return real_move(src, dst, **kwargs)

    monkeypatch.setattr(recovery.shutil, "move", move)
    if fail_second:
        with pytest.raises(OSError, match="failed second file"):
            recovery.move_recovery_match(match)
        assert (source / "a.trc").read_bytes() == b"new"
        assert (destination / "a.trc").read_bytes() == b"old"
        assert (destination / "a0.MP4").read_bytes() == b"old video"
    else:
        recovery.move_recovery_match(match)
        assert (destination / "a.trc").read_bytes() == b"new"
        assert (destination / "b.csv").read_bytes() == b"csv"
        assert not (source / "a.trc").exists()
        assert (destination / "a0.MP4").read_bytes() == b"new video"
    assert (source / "a0.MP4").read_bytes() == b"new video"
    assert (destination / "keep.mp4").read_bytes() == b"keep"
    assert not list(destination.glob(".*backup*"))


@pytest.mark.parametrize("fail_second", [False, True])
def test_export_move_across_volumes(tmp_path: Path, monkeypatch, fail_second: bool) -> None:
    source = tmp_path / "recording" / "mocap-new"
    source.mkdir(parents=True)
    session = tmp_path / "session"
    session.mkdir()
    (source / "a.trc").write_bytes(b"trajectory")
    (source / "b.mp4").write_bytes(b"video")
    (source / "recording.raw").write_bytes(b"raw")
    match = recovery.RecoveryMatch(
        recovery.RecoveryTarget(session, _utc_datetime("20260914_100000"), ()),
        recovery.MocapCandidate(source, _utc_datetime("20260914_100000")), 0.0,
    )

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "Different volumes")

    monkeypatch.setattr(recovery.os, "rename", cross_device)
    original_copystat = recovery.shutil.copystat

    def copystat(src, dst, **kwargs):
        if fail_second and Path(src) == source / "b.mp4":
            raise OSError("Cross-volume copy failed")
        return original_copystat(src, dst, **kwargs)

    monkeypatch.setattr(recovery.shutil, "copystat", copystat)
    if fail_second:
        with pytest.raises(OSError, match="Cross-volume copy failed"):
            recovery.move_recovery_match(match)
        assert (source / "a.trc").read_bytes() == b"trajectory"
        assert (source / "b.mp4").read_bytes() == b"video"
        assert list((session / "mocap-new").iterdir()) == []
    else:
        destination = recovery.move_recovery_match(match)
        assert (destination / "a.trc").read_bytes() == b"trajectory"
        assert (destination / "b.mp4").read_bytes() == b"video"
        assert not (source / "a.trc").exists()
        assert (source / "b.mp4").read_bytes() == b"video"
    assert (source / "recording.raw").read_bytes() == b"raw"


def test_recovery_plan_reports_scan_and_matching_progress(tmp_path: Path) -> None:
    dataset_root = tmp_path / "dataset"
    source_root = tmp_path / "source"
    dataset_root.mkdir()
    source_root.mkdir()
    session = _session(dataset_root, "20260914_100000_session1")
    candidate = source_root / "nested" / "mocap-candidate"
    candidate.mkdir(parents=True)
    (candidate / "body.trc").write_text("data", encoding="utf-8")
    events: list[tuple[str, int, int | None, str]] = []

    recovery.build_recovery_plan(
        dataset_root,
        source_root,
        [session],
        lambda stage, current, total, detail: events.append(
            (stage, current, total, detail)
        ),
    )

    assert any(stage == "session" and current == total == 1 for stage, current, total, _ in events)
    assert any(stage == "candidate_scan" for stage, *_ in events)
    assert any(stage == "candidate" and "mocap-candidate" in detail for stage, _, _, detail in events)
    assert any(stage == "matching" and current == total for stage, current, total, _ in events)
