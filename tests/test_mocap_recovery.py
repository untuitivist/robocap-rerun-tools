from datetime import datetime
from pathlib import Path

from robocap_rerun_tools import mocap_recovery as recovery


def _local_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d_%H%M%S").astimezone()


def _session(root: Path, name: str) -> Path:
    session = root / name
    session.mkdir(parents=True)
    (session / "robocap_segment1_video_left.mp4").write_bytes(b"video")
    return session


def test_recovery_plan_matches_nearest_candidates_and_copies_without_removing_source(
    tmp_path: Path, monkeypatch
) -> None:
    dataset_root = tmp_path / "dataset"
    source_root = tmp_path / "exports"
    dataset_root.mkdir()
    source_root.mkdir()
    missing = _session(dataset_root, "20260914_100000_session1")
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
    (second / "nested").mkdir()
    (second / "nested" / "body.CSV").write_text("second", encoding="utf-8")
    (unused / "body.bvh").write_text("unused", encoding="utf-8")
    (invalid / "notes.txt").write_text("invalid", encoding="utf-8")
    creation_times = {
        "mocap-first": _local_datetime("20260914_100100"),
        "Mocap-second": _local_datetime("20260914_100900"),
        "mocap-unused": _local_datetime("20260914_120000"),
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

    destinations = [recovery.copy_recovery_match(match) for match in plan.matches]

    assert destinations == [
        missing / "mocap-first",
        empty / "Mocap-second",
        ambiguous / "mocap-unused",
    ]
    assert (missing / "mocap-first" / "body.trc").read_text(encoding="utf-8") == "first"
    assert not existing_empty.exists()
    assert (empty / "Mocap-second" / "nested" / "body.CSV").read_text(
        encoding="utf-8"
    ) == "second"
    assert (first / "body.trc").is_file()
    assert (second / "nested" / "body.CSV").is_file()
    assert not (ambiguous / "mocap-a").exists()
    assert not (ambiguous / "mocap-b").exists()
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
        _local_datetime("20260914_100000"),
        (destination,),
    )
    match = recovery.RecoveryMatch(
        target,
        recovery.MocapCandidate(source, _local_datetime("20260914_100000")),
        0.0,
    )

    copied = recovery.copy_recovery_match(match)

    assert copied == tmp_path / "session" / "mocap-candidate"
    assert not destination.exists()
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
        "mocap-same": _local_datetime("20260914_100001"),
        "mocap-new": _local_datetime("20260914_101001"),
    }
    monkeypatch.setattr(recovery, "candidate_creation_time", lambda path: creation_times[path.name])
    monkeypatch.setattr(recovery, "session_has_frame_count_difference", lambda _path: True)

    plan = recovery.build_recovery_plan(dataset_root, source_root, [same, different])

    assert [(item.target.session_dir, item.candidate.path.name) for item in plan.skipped_same_name] == [
        (same, "mocap-same")
    ]
    assert [(item.target.session_dir, item.candidate.path.name) for item in plan.matches] == [
        (different, "mocap-new")
    ]


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
    match = recovery.RecoveryMatch(
        recovery.RecoveryTarget(
            session,
            _local_datetime("20260914_100000"),
            (first, second),
        ),
        recovery.MocapCandidate(candidate, _local_datetime("20260914_100000")),
        0.0,
    )

    copied = recovery.copy_recovery_match(match)

    assert copied == session / "mocap-new"
    assert not first.exists()
    assert not second.exists()
    assert (copied / "new.csv").read_text(encoding="utf-8") == "new"


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
    match = recovery.RecoveryMatch(
        recovery.RecoveryTarget(
            session,
            _local_datetime("20260914_100000"),
            (first, second),
        ),
        recovery.MocapCandidate(candidate, _local_datetime("20260914_100000")),
        0.0,
    )

    def fail_copy(*_args, **_kwargs) -> None:
        (session / "mocap-new").mkdir()
        raise OSError("copy failed")

    monkeypatch.setattr(recovery.shutil, "copytree", fail_copy)

    try:
        recovery.copy_recovery_match(match)
    except OSError as exc:
        assert str(exc) == "copy failed"
    else:
        raise AssertionError("copy_recovery_match should propagate copy failure")
    assert (first / "a.trc").read_text(encoding="utf-8") == "a"
    assert (second / "b.bvh").read_text(encoding="utf-8") == "b"
    assert not (session / "mocap-new").exists()
