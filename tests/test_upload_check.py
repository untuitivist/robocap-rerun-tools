from argparse import Namespace

import pytest

from robocap_rerun_tools import cli, session_integrity, timestamp_anomaly
from robocap_rerun_tools.modelscope_publisher import validate_upload_sources


def test_upload_inspection_generates_timestamp_report_without_content_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(session_integrity, "audit_session", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("Upload must not run full content inspection")))
    monkeypatch.setattr(cli, "inspection_files", lambda *a: [])
    monkeypatch.setattr(cli, "resolve_ffprobe", lambda *a: "ffprobe")
    reports = []

    def write(*args, **kwargs):
        assert "integrity" not in kwargs
        reports.append(args)
        return tmp_path / "timestamp_anomaly_detail_table.html"

    monkeypatch.setattr(timestamp_anomaly, "write_timestamp_anomaly_report", write)
    args = Namespace(session_dir=tmp_path, segment=None, output=None,
                     ffprobe="ffprobe", ffmpeg="ffmpeg", mocap_ratio=8,
                     inspection_mocap_ratio=8, upload_check=True,
                     refresh_inspection=True, dry_run=False)
    assert cli.command_inspect(args) == 0
    cli.refresh_modelscope_inspection(args)
    assert len(reports) == 2


def test_standalone_inspection_still_uses_full_check(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "inspect_session_report", lambda *a, **kw: (
        calls.append(a) or tmp_path / "report.html", {}))
    args = Namespace(session_dir=tmp_path, segment=None, output=None,
                     ffprobe="ffprobe", ffmpeg="ffmpeg", mocap_ratio=4)
    assert cli.command_inspect(args) == 0
    assert len(calls) == 1


def test_lightweight_checks_reject_missing_empty_and_active_files(tmp_path):
    path = tmp_path / "imu.db"
    with pytest.raises(FileNotFoundError, match="missing"):
        validate_upload_sources([path])
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        validate_upload_sources([path])
    validate_upload_sources([path], allow_empty=True)
    path.write_bytes(b"payload not parsed by lightweight checks")
    validate_upload_sources([path])
    path.with_name("imu.db-wal").write_bytes(b"pending")
    with pytest.raises(ValueError, match="unmerged"):
        validate_upload_sources([path], allow_empty=True)
