import json
import struct
import wave
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_session_integrity import full_session
from test_upload_verification import RemoteApi

from robocap_rerun_tools import (
    batch_completeness,
    fault_publishing,
    file_integrity,
    participant_metadata,
    session_integrity,
    web_app,
)
from robocap_rerun_tools import modelscope_publisher as publisher
from robocap_rerun_tools.data_packager import discover_package_files
from robocap_rerun_tools.session_quality import assess_quality


def write_wav(path, frames=160, *, channels=1, width=2):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((channels, width, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0" * frames * channels * width)


@pytest.mark.parametrize("channels,width", [(1, 1), (2, 2), (2, 3), (1, 4)])
def test_audio_reads_actual_samples_and_accepts_silence(tmp_path, channels, width):
    path = tmp_path / "audio_segment1.wav"
    write_wav(path, channels=channels, width=width)
    checked = file_integrity.inspect_file(path)
    assert checked["state"] == "ok"
    assert checked["sample_frames"] == 160
    assert checked["duration_s"] == 0.01
    assert checked["channels"] == channels


@pytest.mark.parametrize("kind,expected", [
    ("truncated", "invalid_format"), ("header_only", "empty_data"),
    ("zero", "zero_byte"), ("not_wav", "invalid_format"),
])
def test_audio_defects_are_not_passing(tmp_path, kind, expected):
    path = tmp_path / "audio_segment1.wav"
    write_wav(path, frames=0 if kind == "header_only" else 160)
    if kind == "truncated":
        path.write_bytes(path.read_bytes()[:-8])
    elif kind == "zero":
        path.write_bytes(b"")
    elif kind == "not_wav":
        path.write_bytes(b"not an audio file")
    assert file_integrity.inspect_file(path)["state"] == expected


@pytest.mark.parametrize("sample,expected", [(0.5, "ok"), (float("nan"), "invalid_data")])
def test_float_wav_samples(tmp_path, sample, expected):
    path = tmp_path / "float.wav"
    fmt = struct.pack("<HHIIHH", 3, 1, 16000, 64000, 4, 32)
    body = b"WAVEfmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<If", 4, sample)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    assert file_integrity.inspect_file(path)["state"] == expected


def test_rf64_and_non_pcm_do_not_silently_pass(tmp_path):
    path = tmp_path / "audio.wav"
    write_wav(path)
    original = path.read_bytes()
    ds64 = b"ds64" + struct.pack("<IQQQI", 28, len(original) + 28, 320, 160, 0)
    path.write_bytes(b"RF64\xff\xff\xff\xffWAVE" + ds64 + original[12:36]
                     + b"data\xff\xff\xff\xff" + original[44:])
    assert file_integrity.inspect_file(path)["state"] == "ok"
    changed = bytearray(original)
    struct.pack_into("<H", changed, 20, 6)
    path.write_bytes(changed)
    assert file_integrity.inspect_file(path)["state"] == "not_checked"


def test_each_segment_requires_its_own_audio_and_package_selection_is_exact(tmp_path, monkeypatch):
    source = full_session(tmp_path, monkeypatch, segments=(1, 10))
    (source / "audio_segment1.wav").unlink()
    report = session_integrity.audit_session(source, progress=lambda _: None)
    missing = [r["path"] for r in report["checks"] if r["state"] == "missing"]
    assert missing == ["audio_segment1.wav"]
    selected = discover_package_files(source, "segment1", False, False)
    assert not any("segment10" in path.name for path in selected)
    assert batch_completeness.blocks_normal_upload(report)


def current_report(source, integrity, *, mocap_frames=80):
    path = source / "_artifacts/segment1/inspection/timestamp_anomaly_detail_table.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session": str(source), "ratio": 8, "referenceFrames": 9,
        "mocapFrames": mocap_frames, "thirdFrames": 10, "integrity": integrity,
    }
    path.write_text("<script>const report=" + json.dumps(payload)
                    + "; const eventTypes=[];</script>", encoding="utf-8")


def test_legacy_audio_unaware_reports_cannot_authorize_clean_upload(tmp_path, monkeypatch):
    source = full_session(tmp_path, monkeypatch)
    report = session_integrity.audit_session(source, progress=lambda _: None)
    current_report(source, {**report, "version": 2})
    quality = assess_quality(source, report, ratio=8)
    assert quality["quality_status"] == "unchecked"
    assert quality["inspection_issues"]


def test_source_and_frame_error_metadata_both_retained(tmp_path, monkeypatch):
    source = full_session(tmp_path, monkeypatch)
    (source / "audio_segment1.wav").write_bytes(b"")
    report = session_integrity.audit_session(source, progress=lambda _: None)
    current_report(source, report, mocap_frames=84)
    quality = fault_publishing.collect_fault_quality(source, integrity_report=report, ratio=8)
    assert quality["error_types"] == ["local_zero_byte", "mocap_extra"]
    assert quality["quality_details"]["segment1"]["mocap_frame_difference"] == 4
    assert str(source) not in json.dumps(quality)
    fault_publishing.validate_quality_record(quality)
    quality["error_types"] = ["mocap_extra"]
    with pytest.raises(ValueError, match="classification"):
        fault_publishing.validate_quality_record(quality)


def fake_repositories(tmp_path, monkeypatch):
    normal = publisher.ModelScopeSettings("test", "https://modelscope.cn", tmp_path / ".env", "test", "owner/normal")
    fault = replace(normal, repo_id="owner/fault")
    apis = {settings.repo_id: RemoteApi(tmp_path / label) for label, settings in (("normal", normal), ("fault", fault))}
    for api in apis.values():
        api.files["metadata.jsonl"] = b""
        api.files["README.md"] = b"Dataset card"
        api.repo_exists = lambda *args: True
        api.whoami = lambda: SimpleNamespace(username="owner")
    monkeypatch.setattr(publisher, "load_modelscope_settings", lambda *args: normal)
    monkeypatch.setattr(fault_publishing, "fault_settings", lambda *args: fault)
    monkeypatch.setattr(publisher, "_hub_api", lambda settings: apis[settings.repo_id])
    monkeypatch.setattr(participant_metadata, "load_catalog", lambda *args, **kwargs: {})
    monkeypatch.setattr(publisher, "measure_session_duration_s", lambda *args: 0.3)
    monkeypatch.setattr(publisher, "discover_device_ids", lambda *args: {"main": None, "left": None, "right": None})
    return apis


def test_batch_upload_uses_frame_reports_without_automatic_content_routing(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    sources = []
    for index in range(1, 4):
        source = full_session(root / f"case{index}", monkeypatch)
        source = source.rename(source.with_name(f"20261010_01000{index}_session{index}"))
        if index == 2:
            (source / "audio_segment1.wav").unlink()
        report = session_integrity.audit_session(source, progress=lambda _: None)
        current_report(source, report, mocap_frames=72 if index == 3 else 80)
        sources.append(source)
    apis = fake_repositories(tmp_path, monkeypatch)
    monkeypatch.setattr(batch_completeness, "inspect_local_batch", lambda *a: pytest.fail("No full upload audit"))
    output = list(web_app.bulk_upload_clean_modelscope_sessions(
        str(root), 8, False, upload_date="20261010", batch_size=2,
    ))[-1]
    normal_rows = [json.loads(line) for line in apis["owner/normal"].files["metadata.jsonl"].splitlines()]
    assert len(normal_rows) == 2, output
    assert not apis["owner/fault"].uploads
    assert {row["session_id"] for row in normal_rows} == {s.name for s in sources[:2]}
    list(web_app.bulk_upload_clean_modelscope_sessions(
        str(root), 8, False, upload_date="20261010", quality_target="fault",
    ))
    fault_rows = [json.loads(line) for line in apis["owner/fault"].files["metadata.jsonl"].splitlines()]
    assert len(fault_rows) == 1
    assert fault_rows[0]["error_types"] == ["mocap_missing"]
    for repo, rows in (("owner/normal", normal_rows), ("owner/fault", fault_rows)):
        api = apis[repo]
        for row in rows:
            manifest = json.loads(api.files[row["manifest"]])
            assert manifest.get("quality_status") == row.get("quality_status")
            assert manifest.get("error_types") == row.get("error_types")
            assert all("_reports" not in item["packaged_as"] for item in manifest["files"])
        assert api.uploads
    before = {repo: len(api.uploads) for repo, api in apis.items()}
    list(web_app.bulk_upload_clean_modelscope_sessions(str(root), 8, False, upload_date="20261010"))
    assert {repo: len(api.uploads) for repo, api in apis.items()} == before


def test_fault_without_mocap_or_video_is_archived_with_unknown_duration(tmp_path, monkeypatch):
    source = tmp_path / "raw/P01/20261010_010000_session1"
    source.mkdir(parents=True)
    write_wav(source / "audio_segment1.wav")
    apis = fake_repositories(tmp_path, monkeypatch)

    def unavailable(*args):
        raise publisher.ModelScopePublisherError("No reference video")

    monkeypatch.setattr(publisher, "measure_session_duration_s", unavailable)
    integrity = session_integrity.audit_session(source, progress=lambda _: None)
    staged = publisher.stage_session(source, "P01", quality_target="fault", integrity_report=integrity, progress=None)
    settings = fault_publishing.fault_settings()
    publisher.upload_staged_dataset(publisher.load_staged_dataset(staged.dataset_root), None,
                                    settings=settings, api=apis["owner/fault"], progress=None)
    api = apis["owner/fault"]
    assert api.files["metadata.jsonl"]
    row = json.loads(api.files["metadata.jsonl"])
    assert row["duration_s"] is None
    assert row["duration_status"] == "unavailable"
    assert row["error_types"] == ["local_missing"]
    assert row["quality_details"] == {}
    assert row["inspection_issues"]
    assert api.files[row["session_path"] + "/audio_segment1.wav"] == (source / "audio_segment1.wav").read_bytes()
    assert not apis["owner/normal"].uploads


def test_active_source_and_inconclusive_inspection_never_upload(tmp_path, monkeypatch):
    source = full_session(tmp_path, monkeypatch)
    (source / "robocap_segment1_imu_left.db-wal").write_bytes(b"unmerged changes")
    from robocap_rerun_tools.session_quality import unstable_source
    report = session_integrity.audit_session(source, progress=lambda _: None)
    assert unstable_source(report)
    (source / "robocap_segment1_imu_left.db-wal").unlink()
    report = session_integrity.audit_session(source, progress=lambda _: None)
    assert assess_quality(source, report)["quality_status"] == "unchecked"


def test_ambiguous_mocap_directories_preserve_both_exports_and_rigid_names(tmp_path, monkeypatch):
    source = full_session(tmp_path / "raw", monkeypatch)
    duplicate = source / "mocap-second"
    duplicate.mkdir()
    (duplicate / "Body0.csv").write_bytes(b"second capture")
    (duplicate / "BigBox0.csv").write_bytes(b"object capture")
    fake_repositories(tmp_path, monkeypatch)
    integrity = session_integrity.audit_session(source, progress=lambda _: None)
    staged = publisher.stage_session(source, "P01", quality_target="fault", integrity_report=integrity, progress=None)
    row = json.loads(staged.metadata_path.read_text(encoding="utf-8"))
    manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    assert "local_ambiguous" in row["error_types"]
    assert "BigBox0" in row["mocap_rigid_names"]
    paths = [item["packaged_as"] for item in manifest["files"]]
    assert "mocap-second/Body0.csv" in paths
    assert "mocap-P01-S01-person-1p/Body0.csv" in paths
    assert (staged.session_dir / "mocap-second/Body0.csv").read_bytes() == b"second capture"


def test_partial_reference_video_coverage_does_not_undercount_session_duration(tmp_path, monkeypatch):
    source = full_session(tmp_path / "raw", monkeypatch, segments=(1, 2))
    for path in source.glob("robocap_segment2_video_*.mp4"):
        path.unlink()
    fake_repositories(tmp_path, monkeypatch)
    integrity = session_integrity.audit_session(source, progress=lambda _: None)
    staged = publisher.stage_session(source, "P01", quality_target="fault", integrity_report=integrity, progress=None)
    row = json.loads(staged.metadata_path.read_text(encoding="utf-8"))
    assert row["duration_s"] is None  # Not the measurable segment1-only duration.
    assert "whole-Session duration is unknown" in row["duration_error"]


def test_stale_frame_report_does_not_add_a_fictitious_frame_error(tmp_path, monkeypatch):
    source = full_session(tmp_path / "raw", monkeypatch)
    report = session_integrity.audit_session(source, progress=lambda _: None)
    current_report(source, report, mocap_frames=84)
    (source / "audio_segment1.wav").write_bytes(b"")
    changed = session_integrity.audit_session(source, progress=lambda _: None)
    quality = assess_quality(source, changed, ratio=8)
    assert quality["error_types"] == ["local_zero_byte"]
    assert quality["quality_details"] == {}
    assert quality["inspection_issues"]
