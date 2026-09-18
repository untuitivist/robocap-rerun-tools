import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from robocap_rerun_tools import fault_publishing as fault
from robocap_rerun_tools import modelscope_publisher as publisher


def recording(root, segment="segment1", **counts):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"robocap_{segment}_video_left.mp4").write_bytes(b"original-video")
    mocap = root / "mocap-L01-S7-user-05p"
    mocap.mkdir(exist_ok=True)
    (mocap / "motion.trc").write_text("Frame#\tTime\n", encoding="utf-8")
    report = root / "_artifacts" / segment / "inspection" / "timestamp_anomaly_detail_table.html"
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "referenceFrames": 9,
        "ratio": 8,
        "mocapFrames": 72,
        "thirdFrames": 12,
        "session": str(root),
        "segment": segment,
        "files": [],
        "events": [],
    }
    payload.update(counts)
    report.write_text(
        "<script>const report=" + json.dumps(payload) + "; const eventTypes=[];</script>",
        encoding="utf-8",
    )
    return report


def test_quality_retains_multiple_errors_and_segments(tmp_path):
    recording(tmp_path)
    recording(tmp_path, "segment2", mocapFrames=80, thirdFrames=10)
    quality = fault.collect_fault_quality(tmp_path)
    assert quality["error_types"] == ["mocap_missing", "third_person_extra"]
    assert quality["quality_details"]["segment1"]["mocap_frame_difference"] == -8
    assert quality["quality_details"]["segment2"]["error_types"] == []
    fault.validate_quality_record(quality)
    quality["quality_details"]["segment1"]["expected_mocap_frames"] = 79
    with pytest.raises(ValueError, match="inconsistent"):
        fault.validate_quality_record(quality)


@pytest.mark.parametrize(
    "counts", [{"mocapFrames": 80, "thirdFrames": 10}, {"ratio": 3}, {"mocapFrames": None}]
)
def test_reject_clean_or_invalid_reports(tmp_path, counts):
    recording(tmp_path, **counts)
    with pytest.raises(ValueError):
        fault.collect_fault_quality(tmp_path)


def test_unchecked_segment_excludes_entire_session(tmp_path):
    recording(tmp_path)
    (tmp_path / "robocap_segment2_video_left.mp4").write_bytes(b"video")
    with pytest.raises((ValueError, OSError)):
        fault.collect_fault_quality(tmp_path)


def test_fault_stage_preserves_original_and_metadata(tmp_path, monkeypatch):
    source = tmp_path / "20260918_080000_session1"
    recording(source)
    monkeypatch.setattr(
        publisher, "discover_device_ids", lambda *args: {"main": None, "left": None, "right": None}
    )
    monkeypatch.setattr(publisher, "measure_session_duration_s", lambda *args: 0.3)
    staged = publisher.stage_session(source, "L01", quality_target="fault", progress=None)
    assert "/fault/" in staged.dataset_root.as_posix()
    manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    record = json.loads(staged.metadata_path.read_text(encoding="utf-8"))
    assert record["quality_details"] == manifest["quality_details"]
    assert record["duration_s"] == 0.3
    videos = list(staged.session_dir.rglob("*.mp4"))
    assert [video.read_bytes() for video in videos] == [b"original-video"]
    assert fault.validate_fault_staging(staged.dataset_root)
    with pytest.raises(ValueError, match="different publishing target"):
        fault.bind_staging_target(staged.dataset_root, "normal")
    with pytest.raises(ValueError, match="cropping"):
        publisher.stage_session(source, "L01", quality_target="fault", aligned_intersection=True)
    settings = publisher.ModelScopeSettings(
        token="test",
        endpoint="https://modelscope.cn",
        env_path=tmp_path / ".env",
        token_source="test",
        repo_id="owner/normal",
    )
    with pytest.raises(ValueError, match="configured fault"):
        publisher.upload_staged_dataset(
            publisher.load_staged_dataset(staged.dataset_root), "owner/normal", settings=settings
        )
    manifest["error_types"] = ["mocap_extra"]
    staged.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        fault.validate_fault_staging(staged.dataset_root)


def test_fault_repo_configuration_is_independent(tmp_path, monkeypatch):
    monkeypatch.delenv(fault.FAULT_REPO_KEY, raising=False)
    monkeypatch.delenv("MODELSCOPE_REPO_ID", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "MODELSCOPE_REPO_ID=owner/normal\nMODELSCOPE_API_TOKEN=secret\n", encoding="utf-8"
    )
    fault.save_fault_repository("owner/fault", env)
    assert fault.fault_settings(env).repo_id == "owner/fault"
    assert publisher.load_modelscope_settings(env).repo_id == "owner/normal"
    with pytest.raises(ValueError, match="different repositories"):
        fault.save_fault_repository("owner/normal", env)


def test_fault_card_preserves_yaml_and_updates_download_repository():
    card = fault.fault_readme(
        "---\nlicense: apache-2.0\n---\n# Original\nuntuitivist/EgoMotionActions"
    )
    assert card.startswith("---\nlicense:")
    assert "untuitivist/EgoMotionActions-fault" in card
    assert "quality_details" in card


def test_fault_web_upload_uses_utc8_date_and_own_repo(tmp_path, monkeypatch):
    from robocap_rerun_tools import web_app

    source = tmp_path / "20260918_180000_session1"
    source.mkdir()
    calls = []
    monkeypatch.setattr(
        web_app, "stream_cli_command", lambda args: calls.append(args) or iter(["done"])
    )
    list(web_app.upload_fault_modelscope_data(str(source), "master", True, 4, "20260101", True))
    args = calls[0]
    assert args[args.index("--upload-date") + 1] == "20260919"
    assert args[args.index("--repo-id") + 1] == fault.fault_settings().repo_id
    assert "/fault/" in Path(args[1]).as_posix()


def test_fault_batch_filters_clean_and_invalid_and_retries_tail(tmp_path, monkeypatch):
    from robocap_rerun_tools import cli, dataset_statistics, web_app

    for name in ("fault1", "fault2", "fault3"):
        recording(tmp_path / name)
    recording(tmp_path / "clean", mocapFrames=80, thirdFrames=10)
    recording(tmp_path / "invalid", mocapFrames=None)
    monkeypatch.setattr(web_app, "STREAM_REFRESH_SECONDS", 0.01)
    monkeypatch.setattr(cli, "resolve_ffprobe", lambda *args: "ffprobe")
    monkeypatch.setattr(dataset_statistics, "probe_video_duration", lambda *args: (0.3, None))
    monkeypatch.setattr(
        publisher, "discover_device_ids", lambda *args: {"main": None, "left": None, "right": None}
    )
    monkeypatch.setattr(publisher, "measure_session_duration_s", lambda *args: 0.3)
    connection = SimpleNamespace(settings=fault.fault_settings(), api=object(), username="test")
    monkeypatch.setattr(publisher, "connect_modelscope", lambda settings: connection)
    monkeypatch.setattr(
        publisher,
        "audit_remote_sessions",
        lambda *args, **kwargs: publisher.RemoteSessionIntegrityReport(0, ()),
    )
    calls = []

    def upload(staged, repo_id, **kwargs):
        assert kwargs["settings"].repo_id == fault.fault_settings().repo_id
        assert "/fault/batches/" in staged.dataset_root.as_posix()
        entries = [
            json.loads(line)
            for line in staged.metadata_path.read_text(encoding="utf-8").splitlines()
        ]
        names = tuple(entry["session_id"] for entry in entries)
        calls.append(names)
        if len(names) == 1 and calls.count(names) == 1:
            raise publisher.ModelScopePublisherError("transient failure")

    monkeypatch.setattr(publisher, "upload_staged_dataset", upload)
    output = list(
        web_app.bulk_upload_fault_modelscope_sessions(
            str(tmp_path),
            8,
            False,
            "English",
            "20260918",
            True,
            False,
            2,
            "auto",
            False,
        )
    )[-1]
    assert [len(item) for item in calls] == [2, 1, 1], output
    assert {name for batch in calls for name in batch} == {"fault1", "fault2", "fault3"}


def test_remote_fault_quality_change_invalidates_skip(tmp_path, monkeypatch):
    recording(tmp_path)
    quality = fault.collect_fault_quality(tmp_path)
    entry = {
        "primitive_id": "L01",
        "session_id": "session1",
        "upload_batch_id": "20260918",
        "session_path": "EgoMotionActions/20260918/L01/session1",
        **quality,
    }
    monkeypatch.setattr(publisher, "_download_remote_metadata", lambda *args: json.dumps(entry))
    api = SimpleNamespace(repo_exists=lambda *args: True)
    monkeypatch.setattr(publisher, "_hub_api", lambda settings: api)
    monkeypatch.setattr(
        publisher,
        "_audit_remote_session_entry",
        lambda *args: publisher.RemoteSessionIntegrity(
            "L01",
            "session1",
            entry["session_path"],
            "20260918",
            True,
        ),
    )
    settings = publisher.ModelScopeSettings(
        token="test",
        endpoint="https://modelscope.cn",
        env_path=tmp_path / ".env",
        token_source="test",
        repo_id="owner/fault",
    )
    expected = {("L01", "session1"): quality}
    result = publisher.audit_remote_sessions(
        expected, settings=settings, api=api, expected_quality=expected
    )
    assert result.sessions[0].complete
    expected[("L01", "session1")] = {**quality, "error_types": ["mocap_extra"]}
    result = publisher.audit_remote_sessions(
        expected, settings=settings, api=api, expected_quality=expected
    )
    assert not result.sessions[0].complete
    assert "quality metadata changed" in result.sessions[0].issues[-1]
