import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_modelscope_publisher import write_inspection_report
from test_upload_verification import RemoteApi

from robocap_rerun_tools import modelscope_publisher as publisher
from robocap_rerun_tools.batch_completeness import (
    blocks_normal_upload,
    local_upload_inventory,
    write_batch_report,
)
from robocap_rerun_tools.mocap_inventory import mocap_names
from robocap_rerun_tools.session_integrity import (
    INTEGRITY_VERSION,
    integrity_summary,
    save_integrity,
    source_signature,
)


def test_rigid_names_preserve_labels_and_deduplicate_export_formats():
    base = Path("session/mocap-P01-St-person1")
    assert mocap_names(
        [
            base / name
            for name in (
                "mocap-P01-St-person1-Body0.trc",
                "mocap-P01-St-person1-Body0.csv",
                "mocap-P01-St-person1-Tracker0.trc",
                "mocap-P01-St-person1-Box-2.csv",
                "mocap-P01-St-person1-box-2.trc",
                "Unnamed1.csv",
                "third.mp4",
            )
        ]
    ) == ["Body0", "Box-2", "Tracker0"]


@pytest.fixture
def backfill(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "measure_session_duration_s", lambda *a: 12.5)
    source = tmp_path / "session1"
    mocap = source / "mocap-HH01-St-person1"
    mocap.mkdir(parents=True)
    for name in ("Body0.trc", "Body0.bvh", "Body0.csv", "Tracker0.trc", "Tracker0.csv"):
        (mocap / name).write_bytes(b"original motion export")
    (source / "robocap_segment1_video_left.mp4").write_bytes(b"original video")
    write_inspection_report(source)
    api = RemoteApi(tmp_path)
    api.whoami = lambda: SimpleNamespace(username="owner")
    api.repo_exists = lambda *a: True
    api.files["metadata.jsonl"] = b""
    monkeypatch.setattr(publisher, "_hub_api", lambda *a: api)
    settings = publisher.ModelScopeSettings(
        "test", "https://modelscope.cn", tmp_path / ".env", ".env", "owner/data"
    )

    def publish(date="20260904"):
        publisher.stage_session(source, "HH01", dataset_root=tmp_path / "stage", progress=None)
        return publisher.upload_staged_dataset(
            publisher.load_staged_dataset(tmp_path / "stage"),
            None,
            api=api,
            settings=settings,
            upload_date=date,
            progress=None,
        )

    publish()
    return source, mocap, api, settings, publish


def test_omitted_local_rigid_is_backfilled_at_original_date_preserving_remote_only(backfill):
    source, mocap, api, settings, publish = backfill
    original = json.loads(api.files["metadata.jsonl"])
    remote_path = original["session_path"]
    for suffix in ("trc", "csv"):
        (mocap / f"BigBox0.{suffix}").write_bytes(b"new local rigid")
        api.files[f"{remote_path}/mocap/OnlyRemote0.{suffix}"] = b"retain remote rigid"
    original["human_review"] = "keep me"
    api.files["metadata.jsonl"] = json.dumps(original).encode()
    old_manifest = json.loads(api.files[original["manifest"]])
    old_manifest["human_review"] = "keep manifest note"
    api.files[original["manifest"]] = json.dumps(old_manifest).encode()
    local_files = local_upload_inventory(source, publisher.find_mocap_files(source))
    audit = publisher.audit_remote_sessions(
        {("HH01", "session1")},
        local_files={("HH01", "session1"): local_files},
        settings=settings,
        api=api,
        progress=None,
    )
    assert not audit.complete_keys
    assert any("BigBox0" in issue for issue in audit.sessions[0].issues)
    publish("20261010")
    current = json.loads(api.files["metadata.jsonl"])
    manifest = json.loads(api.files[current["manifest"]])
    assert current["session_path"] == remote_path
    assert current["upload_batch_id"] == "20260904"
    assert current["human_review"] == "keep me"
    assert manifest["human_review"] == "keep manifest note"
    assert current["mocap_rigid_names"] == ["BigBox0", "Body0", "OnlyRemote0", "Tracker0"]
    assert manifest["mocap_rigid_names"] == current["mocap_rigid_names"]
    assert current["file_count"] == len(manifest["files"])
    assert current["packaged_bytes"] == sum(r["packaged_bytes"] for r in manifest["files"])
    for suffix in ("trc", "csv"):
        assert api.files[f"{remote_path}/mocap/BigBox0.{suffix}"] == b"new local rigid"
        assert api.files[f"{remote_path}/mocap/OnlyRemote0.{suffix}"] == b"retain remote rigid"
    assert not any("20261010" in key for key in api.files)
    audit = publisher.audit_remote_sessions(
        {("HH01", "session1")},
        local_files={("HH01", "session1"): local_files},
        settings=settings,
        api=api,
        progress=None,
    )
    assert audit.complete_keys == {("HH01", "session1")}
    # Same-sized edits must not be mistaken for unchanged files.
    (mocap / "BigBox0.trc").write_bytes(b"bad local rigid")
    local_files = local_upload_inventory(source, publisher.find_mocap_files(source))
    audit = publisher.audit_remote_sessions(
        {("HH01", "session1")},
        local_files={("HH01", "session1"): local_files},
        settings=settings,
        api=api,
        progress=None,
    )
    assert not audit.complete_keys


def test_backfill_cannot_forget_missing_remote_only_file(backfill):
    _, mocap, api, _, publish = backfill
    # First publish an object, then remove it only from the temporary test source.
    path = mocap / "GoneBox0.csv"
    path.write_bytes(b"remote original")
    publish()
    path.unlink()
    row = json.loads(api.files["metadata.jsonl"])
    del api.files[f"{row['session_path']}/mocap/GoneBox0.csv"]
    upload_count = len(api.uploads)
    with pytest.raises(publisher.ModelScopePublisherError, match="no local replacement"):
        publish()
    assert len(api.uploads) == upload_count


@pytest.mark.parametrize("remote_hashes", [True, False])
def test_same_size_remote_corruption_triggers_repair(backfill, remote_hashes):
    source, _, api, settings, publish = backfill
    row = json.loads(api.files["metadata.jsonl"])
    path = f"{row['session_path']}/mocap/Tracker0.csv"
    original = api.files[path]
    api.files[path] = b"x" * len(original)
    api.hashes = remote_hashes
    audit = publisher.audit_remote_sessions(
        {("HH01", "session1")},
        settings=settings,
        api=api,
        progress=None,
        local_files={
            ("HH01", "session1"): local_upload_inventory(source, publisher.find_mocap_files(source))
        },
    )
    assert not audit.complete_keys
    assert any("remote hash mismatch" in issue for issue in audit.sessions[0].issues)
    publish()
    assert api.files[path] == original


def test_backfill_retry_after_metadata_was_uploaded(backfill):
    source, mocap, api, settings, _ = backfill
    (mocap / "Box0.csv").write_bytes(b"new export")
    publisher.stage_session(source, "HH01", dataset_root=source.parent / "stage", progress=None)
    upload = api.upload_folder
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        upload(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise OSError("connection lost after commit")

    api.upload_folder = fail_once
    with pytest.raises(publisher.ModelScopePublisherError, match="connection lost"):
        publisher.upload_staged_dataset(
            publisher.load_staged_dataset(source.parent / "stage"),
            None,
            api=api,
            settings=settings,
            upload_date="20261010",
            progress=None,
        )
    publisher.upload_staged_dataset(
        publisher.load_staged_dataset(source.parent / "stage"),
        None,
        api=api,
        settings=settings,
        upload_date="20261010",
        progress=None,
    )
    rows = [json.loads(line) for line in api.files["metadata.jsonl"].splitlines()]
    assert len(rows) == 1
    assert rows[0]["upload_batch_id"] == "20260904"
    assert "Box0" in rows[0]["mocap_rigid_names"]


def test_integrity_report_becomes_stale_when_file_or_roster_changes(tmp_path):
    source = tmp_path / "session"
    source.mkdir()
    (source / "Body0.csv").write_bytes(b"motion")
    report = {
        "version": INTEGRITY_VERSION,
        "status": "passed",
        "checks": [],
        "source_signature": source_signature(source),
    }
    save_integrity(source, report)
    assert integrity_summary(source)[0] == "passed"
    (source / "Box0.csv").write_bytes(b"new export")
    assert integrity_summary(source)[0] == "stale"


def test_segment_only_failure_is_visible_in_statistics(tmp_path):
    report = {"version": INTEGRITY_VERSION, "status": "failed", "checks": [
        {"path": "mocap/Body0.csv", "state": "missing", "severity": "error"}],
        "source_signature": source_signature(tmp_path, segment="segment1")}
    save_integrity(tmp_path, report, "segment1")
    status, issues = integrity_summary(tmp_path)
    assert status == "failed"
    assert "Body0.csv" in issues[0]


def test_local_issues_are_durable_and_unknown_requirements_do_not_block(tmp_path):
    reports = {
        tmp_path / "session": {
            "status": "failed",
            "errors": 1,
            "warnings": 1,
            "checks": [
                {
                    "path": "mocap/Body0.bvh",
                    "state": "missing",
                    "severity": "error",
                    "detail": "缺少导出",
                },
                {
                    "path": "rules",
                    "state": "requirements_unknown",
                    "severity": "warning",
                    "detail": "待确认",
                },
            ],
        }
    }
    report = next(iter(reports.values()))
    assert blocks_normal_upload(report)
    write_batch_report(
        tmp_path, reports, {tmp_path / "session": {"upload_status": "local_incomplete"}}
    )
    assert "Body0.bvh" in (tmp_path / "issues.csv").read_text(encoding="utf-8")
    assert "local_incomplete" in (tmp_path / "sessions.csv").read_text(encoding="utf-8")
    assert "缺少导出" in (tmp_path / "report.html").read_text(encoding="utf-8")
    assert not blocks_normal_upload({"errors": 0, "checks": report["checks"][1:]})
    assert blocks_normal_upload({"errors": 0, "checks": [{"state": "changed"}]})


def test_independent_inspection_records_incomplete_source(tmp_path, monkeypatch):
    from test_session_integrity import full_session

    from robocap_rerun_tools.session_integrity import audit_session, save_integrity

    source = full_session(tmp_path / "raw", monkeypatch)
    body = next(source.glob("mocap*/Body0.csv"))
    body.write_bytes(b"")
    report = audit_session(source, progress=lambda _: None)
    save_integrity(source, report)
    assert any(row["path"].endswith("Body0.csv") and row["state"] == "zero_byte"
               for row in report["checks"])
    assert integrity_summary(source)[0] == "failed"
