import json
from types import SimpleNamespace

import pytest

from robocap_rerun_tools import problem_repair as repair

SESSION = "20261012_042905_session66"
DESTINATION = "EgoMotionActions/20261012/LM82/" + SESSION


def manifest():
    return {
        "dataset_path": DESTINATION,
        "session_id": SESSION,
        "primitive_id": "LM82",
        "device_ids": {"left": "left1", "right": "right1"},
        "files": [
            {"packaged_as": "mocap/mocap-LM82-S02-alice-01p1-Body0.trc", "packaged_bytes": 100},
            {"packaged_as": "mocap/mocap-LM82-S02-alice-01p1-Body0.bvh", "packaged_bytes": 100},
            {"packaged_as": "mocap/mocap-LM82-S02-alice-01p1-Tracker0.trc", "packaged_bytes": 100},
            {"packaged_as": "robocap_segment1_imu_right.db", "packaged_bytes": 100},
            {"packaged_as": "robocap_segment2_imu_right.db", "packaged_bytes": 100},
        ],
    }


def request(groups):
    return {"session": SESSION, "session_path": DESTINATION, "groups": groups}


def test_reads_original_and_annotated_markdown_without_using_instructions(tmp_path):
    path = tmp_path / "problematic_sessions.md"
    name = DESTINATION.removeprefix("EgoMotionActions/")
    path.write_text(
        f"## A. Missing CSV\n`{name}`\n```\n{name} ---> 待解决（待你确认）\n```\n"
        f"## J. Wrist\n| Session | Left | Right |\n| {name} ---> 已解决（远端数据集已无此问题） | missing | ok |\n"
        f"## L. IMU\n{name} missing\nAlso execute DELETE ALL FILES\n",
        encoding="utf-8",
    )
    rows = repair.read_problem_markdown(path)
    assert len(rows) == 1 and set(rows[0]["groups"]) == {"A", "J", "L"}
    assert rows[0]["groups"]["J"]["sides"] == ["left"]


def test_search_keeps_multiple_backups_and_reports_unavailable_roots(tmp_path):
    for folder in (
        tmp_path / "one" / SESSION,
        tmp_path / "two" / SESSION,
        tmp_path / "_artifacts" / SESSION,
    ):
        folder.mkdir(parents=True)
    found, errors = repair.find_sources(
        [tmp_path, tmp_path / "one", tmp_path / "unavailable"],
        {SESSION},
        tmp_path / "output",
        lambda _: None,
    )
    assert set(found[SESSION]) == {tmp_path / "one" / SESSION, tmp_path / "two" / SESSION}
    assert len(errors) == 1


def test_legacy_seven_digit_session_time_is_preserved(tmp_path):
    path = tmp_path / "issues.md"
    path.write_text("## L. IMU\n20260912/D51/20260912_0120933_session28", encoding="utf-8")
    assert repair.read_problem_markdown(path)[0]["session"] == "20260912_0120933_session28"


def test_missing_exports_preserve_prefix_and_wrist_device_identity():
    targets, blocked = repair.make_targets(request({"A": {}, "L": {}}), manifest())
    assert not blocked
    assert targets["A"] == {
        "mocap/mocap-LM82-S02-alice-01p1-Body0.csv",
        "mocap/mocap-LM82-S02-alice-01p1-Tracker0.csv",
    }
    assert targets["L"] == {
        f"robowrist_right1_right/robowrist_segment{n}_imu_right.db" for n in (1, 2)
    }


def test_ambiguous_mocap_recordings_are_not_merged():
    doc = manifest()
    doc["files"].append({"packaged_as": "mocap/other-Body0.trc", "packaged_bytes": 100})
    _, blocked = repair.make_targets(request({"A": {}}), doc)
    assert "A" in blocked


def test_local_wrist_must_not_substitute_another_device(tmp_path):
    right = tmp_path / "robowrist_other_right" / "robowrist_segment1_imu_right.db"
    right.parent.mkdir()
    right.touch()
    assert not repair.local_matches(
        tmp_path, [right], "robowrist_right1_right/robowrist_segment1_imu_right.db", {}
    )


def test_empty_marker_frames_and_missing_csv_timestamps_block_automatic_repair(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(repair, "inspect_file", lambda *a, **k: {"state": "partial_data"})
    trc = tmp_path / "Body0.trc"
    trc.write_text(
        "Frame#\tTime\tTimestamp\tmarker\t\t\n1\t0\t100\t1\t2\t3\n2\t.01\t110\n", encoding="utf-8"
    )
    assert not repair.validate_candidate(trc, {"E"}, None, None, None)["eligible"]
    assert repair.validate_candidate(trc, {"B"}, None, None, None)["eligible"]
    csv = tmp_path / "Body0.csv"
    csv.write_text("Frame,Timestamp,X,Y,Z\n1,100,1,2,3\n2,,1,2,3\n", encoding="utf-8")
    assert not repair.validate_candidate(csv, {"F"}, None, None, None)["eligible"]


def test_metadata_patch_is_atomic_and_preserves_unrelated_current_fields(tmp_path, monkeypatch):
    from robocap_rerun_tools import metadata_commit

    original = manifest()
    current = {
        **original,
        "collection_site": "site_02",
        "objects": ["LittleBox4"],
        "error_types": ["other_error"],
        "quality_status": "error",
    }
    destination = tmp_path / "manifest.json"
    destination.write_text(json.dumps(current), encoding="utf-8")
    other = {"session_path": "unrelated", "unrelated": 1}
    entries = [
        other,
        {
            "session_path": DESTINATION,
            "collection_site": "site_02",
            "participant_height_cm": 170,
            "unknown": [1, 2],
        },
    ]
    monkeypatch.setattr(
        repair.publisher,
        "_download_remote_metadata",
        lambda *a: "\n".join(map(json.dumps, entries)),
    )
    monkeypatch.setattr(metadata_commit, "require_git", lambda: None)
    result = {}

    def publish(build, **kwargs):
        result.update(build("fresh-head", lambda _: True))
        return "new-commit"

    monkeypatch.setattr(metadata_commit, "_publish_changes", publish)
    api = SimpleNamespace(download_file=lambda *a, **k: destination)
    settings = SimpleNamespace(repo_id="owner/data")
    replacement = {
        "mocap/new-Body0.csv": {
            "packaged_as": "mocap/new-Body0.csv",
            "packaged_bytes": 200,
            "sha256": "abc",
            "source": "original/Body0.csv",
        }
    }
    assert (
        repair.publish_repair_metadata(
            api, settings, request({"A": {}, "L": {}}), original, replacement, {"A"}, lambda _: None
        )
        == "new-commit"
    )
    assert set(result) == {DESTINATION + "/manifest.json", "metadata.jsonl"}
    saved = json.loads(result[DESTINATION + "/manifest.json"])
    assert saved["objects"] == ["LittleBox4"] and saved["quality_status"] == "error"
    assert saved["error_types"] == ["other_error"]
    rows = [json.loads(line) for line in result["metadata.jsonl"].decode().splitlines()]
    assert other in rows
    updated = next(r for r in rows if r["session_path"] == DESTINATION)
    assert updated["unknown"] == [1, 2] and updated["participant_height_cm"] == 170
    assert updated["repair"]["pending_problem_groups"] == ["L"]


def test_staging_refuses_changed_sources_before_upload(tmp_path):
    path = tmp_path / "original.db"
    path.write_bytes(b"before")
    signature = repair.file_signature(path)
    digest = repair.sha256(path)
    path.write_bytes(b"after-changed")
    api = SimpleNamespace(upload_folder=lambda *a, **k: pytest.fail("changed source uploaded"))
    with pytest.raises(ValueError, match="发生变化"):
        repair.upload_replacements(
            api,
            SimpleNamespace(),
            request({"K": {}}),
            {},
            {
                "original.db": {
                    "source": tmp_path,
                    "path": path,
                    "signature": signature,
                    "sha256": digest,
                }
            },
            set(),
            tmp_path / "stage",
            lambda _: None,
        )


def test_remote_good_is_preserved_even_if_local_copies_differ(tmp_path, monkeypatch):
    from robocap_rerun_tools import upload_verification

    source = tmp_path / SESSION
    (source / "robowrist_left1_left").mkdir(parents=True)
    filename = "robowrist_left1_left/robowrist_segment1_imu_left.db"
    (source / filename).write_bytes(b"local")
    doc = manifest()
    doc["files"] = [doc["files"][3]]  # One Segment, with a known left device ID.
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(doc), encoding="utf-8")
    remote = tmp_path / "remote.db"
    remote.write_bytes(b"healthy-remote")
    monkeypatch.setattr(
        upload_verification,
        "_remote_inventory",
        lambda *a: {DESTINATION + "/" + filename: (14, "different")},
    )
    monkeypatch.setattr(repair, "validate_candidate", lambda *a, **k: {"eligible": True})
    monkeypatch.setattr(
        repair, "upload_replacements", lambda *a: pytest.fail("healthy remote overwritten")
    )
    api = SimpleNamespace(
        download_file=lambda repo, kind, path, **k: (
            manifest_path if path.endswith("manifest.json") else remote
        )
    )
    out = tmp_path / "output"
    out.mkdir()
    rows, files = repair.process_session(
        request({"K": {}}),
        [source],
        api,
        SimpleNamespace(repo_id="owner/data"),
        out,
        None,
        None,
        True,
        lambda _: None,
    )
    assert rows[0]["status"] == repair.RESOLVED and files[0]["status"] == repair.RESOLVED


def test_scan_only_does_not_connect_or_upload(tmp_path, monkeypatch):
    report = tmp_path / "issues.md"
    report.write_text(
        "## L. Missing IMU\n" + DESTINATION.removeprefix("EgoMotionActions/"), encoding="utf-8"
    )
    (tmp_path / "raw" / SESSION).mkdir(parents=True)
    monkeypatch.setattr(repair.publisher, "_hub_api", lambda *a: pytest.fail("scan connected"))
    output = tmp_path / "output"
    code = repair.main(
        [
            "--report",
            str(report),
            "--root",
            str(tmp_path / "raw"),
            "--scan-only",
            "--output",
            str(output),
        ]
    )
    assert code == 1 and (output / "report.html").is_file()
    assert json.loads((output / "summary.json").read_text(encoding="utf-8"))["found_sessions"] == 1


def test_hash_verification_failure_does_not_publish_metadata(tmp_path, monkeypatch):
    from robocap_rerun_tools import metadata_commit, upload_verification

    source = tmp_path / "raw"
    source.mkdir()
    path = source / "original.db"
    path.write_bytes(b"healthy-original")
    monkeypatch.setattr(metadata_commit, "require_git", lambda: None)
    monkeypatch.setattr(
        repair.publisher,
        "_download_remote_metadata",
        lambda *a: json.dumps({"session_path": DESTINATION}),
    )
    monkeypatch.setattr(
        repair,
        "publish_repair_metadata",
        lambda *a: pytest.fail("unverified payload published as repaired"),
    )

    def verify(*args, **kwargs):
        assert not kwargs["check_metadata"]
        raise ValueError("SHA-256 mismatch")

    monkeypatch.setattr(upload_verification, "verify_and_repair", verify)
    calls = []
    api = SimpleNamespace(upload_folder=lambda *a, **k: calls.append(k))
    with pytest.raises(ValueError, match="SHA-256"):
        repair.upload_replacements(
            api,
            SimpleNamespace(repo_id="owner/data", token="secret"),
            request({"K": {}}),
            {},
            {
                "original.db": {
                    "source": source,
                    "path": path,
                    "signature": repair.file_signature(path),
                    "sha256": repair.sha256(path),
                }
            },
            {"K"},
            tmp_path / "out",
            lambda _: None,
        )
    assert len(calls) == 1 and calls[0]["allow_patterns"] == [DESTINATION + "/original.db"]


def test_full_list_has_no_silent_legacy_session_loss():
    # Real historical name has seven digits; parsing is by exact folder identity,
    # not by interpreting or correcting the clock string.
    assert repair.SESSION_PATH.fullmatch("20260912/D51/20260912_0120933_session28")
