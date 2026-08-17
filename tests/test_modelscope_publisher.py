from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from robocap_rerun_tools import modelscope_publisher as publisher


def write_inspection_report(session_dir: Path, segment: str = "segment1") -> Path:
    report = session_dir / "_artifacts" / segment / "inspection" / publisher.REPORT_NAME
    report.parent.mkdir(parents=True)
    payload = {
        "session": session_dir.name,
        "sessionPath": str(session_dir.resolve()),
        "segment": segment,
        "files": [],
        "events": [],
    }
    report.write_text(
        "<!doctype html><script>const report="
        + json.dumps(payload)
        + "; const eventTypes=[];</script>",
        encoding="utf-8",
    )
    return report


def stage_fixture(tmp_path: Path) -> publisher.StageResult:
    session = tmp_path / "20260803_081935_session39"
    session.mkdir()
    mocap_dir = session / "mocap"
    mocap_dir.mkdir()
    (mocap_dir / "motion.trc").write_text("Frame#\tTime\n", encoding="utf-8")
    (session / "robocap_segment1_video_left.mp4").write_bytes(b"video")
    write_inspection_report(session)
    return publisher.stage_session(
        session,
        "p01",
        dataset_root=tmp_path / "dataset",
        segment="segment1",
        raw_video=True,
        progress=None,
    )


def test_save_modelscope_settings_uses_utf8_env_without_exposing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(publisher.TOKEN_KEY, raising=False)
    monkeypatch.delenv(publisher.ENDPOINT_KEY, raising=False)
    monkeypatch.delenv(publisher.REPO_ID_KEY, raising=False)
    env_path = tmp_path / ".env"

    settings = publisher.save_modelscope_settings(
        "ms-secret-value",
        "https://modelscope.cn",
        env_path,
        repo_id="owner/egomocap",
    )

    assert settings.token == "ms-secret-value"
    assert settings.env_path == env_path.resolve()
    assert settings.repo_id == "owner/egomocap"
    assert "MODELSCOPE_API_TOKEN='ms-secret-value'" in env_path.read_text(encoding="utf-8")
    assert "MODELSCOPE_REPO_ID='owner/egomocap'" in env_path.read_text(encoding="utf-8")
    assert not env_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "ms-secret-value" not in publisher.token_status(settings)


def test_ensure_env_file_adds_repo_key_to_existing_configuration(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MODELSCOPE_API_TOKEN=secret\nMODELSCOPE_ENDPOINT=https://modelscope.cn\n",
        encoding="utf-8",
        newline="\n",
    )

    publisher.ensure_env_file(env_path)

    env_text = env_path.read_text(encoding="utf-8")
    assert publisher.REPO_ID_KEY in env_text
    assert not env_path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_stage_session_uses_primitive_session_hierarchy_and_portable_report(
    tmp_path: Path,
) -> None:
    staged = stage_fixture(tmp_path)

    expected = (
        tmp_path
        / "dataset"
        / publisher.ACTIONS_DIR_NAME
        / "P01"
        / "20260803_081935_session39"
    )
    assert staged.session_dir == expected.resolve()
    assert (expected / "mocap" / "motion.trc").is_file()
    assert (expected / "robocap_segment1_video_left.mp4").read_bytes() == b"video"
    assert not (expected / "_artifacts").exists()
    report_text = staged.inspection_html.read_text(encoding="utf-8")
    assert str(tmp_path.resolve()) not in report_text
    assert '"sessionPath":"20260803_081935_session39"' in report_text

    metadata = [
        json.loads(line) for line in staged.metadata_path.read_text(encoding="utf-8").splitlines()
    ]
    assert metadata == [
        {
            "primitive_id": "P01",
            "session_id": "20260803_081935_session39",
            "session_path": "EgoMotionActions/P01/20260803_081935_session39",
            "manifest": "EgoMotionActions/P01/20260803_081935_session39/manifest.json",
            "inspection_html": (
                "EgoMotionActions/P01/20260803_081935_session39/"
                "timestamp_anomaly_detail_table.html"
            ),
            "segment": "segment1",
            "device_ids": {"main": None, "left": None, "right": None},
            "file_count": 3,
            "packaged_bytes": staged.total_bytes,
        }
    ]
    dataset_readme = staged.readme_path.read_text(encoding="utf-8")
    normalized_readme = " ".join(dataset_readme.split())
    assert "domain:\n- multi-modal\n- cv\n" in dataset_readme
    assert "tasks:\n- action-recognition\n- body-3d-keypoints\n" in dataset_readme
    for tag in (
        "egocentric-video",
        "human-motion",
        "motion-capture",
        "synchronized-multimodal",
        "wearable-sensors",
        "imu",
        "magnetometer",
    ):
        assert f"- {tag}\n" in dataset_readme
    assert "Required capture streams" in dataset_readme
    assert "Complete dataset structure" in dataset_readme
    assert "Only the concrete NOKOV motion-capture export format selection" in normalized_readme
    assert "EgoMotionActions/PXX/<session_id>/" in dataset_readme
    assert "raw_calibration/<device_id>/" in dataset_readme
    assert "EgoMotionActions/` is generated by session staging" in normalized_readme
    assert "rerun/<segment>/inspection/*.rrd" in dataset_readme
    assert "at least one motion-capture format must be present" in normalized_readme
    assert "under `mocap/`" in dataset_readme
    assert "Optional artifact: RRD files" in dataset_readme
    assert "Optional: third-person" not in dataset_readme
    assert "required third-person video" in dataset_readme
    assert "robowrist_<device_id>_left/" in dataset_readme
    assert "robowrist_<device_id>_right/" in dataset_readme


def test_staging_same_session_updates_one_metadata_row(tmp_path: Path) -> None:
    first = stage_fixture(tmp_path)
    second = publisher.stage_session(
        tmp_path / "20260803_081935_session39",
        "P01",
        dataset_root=tmp_path / "dataset",
        segment="segment1",
        raw_video=True,
        progress=None,
    )

    assert first.metadata_path == second.metadata_path
    assert len(second.metadata_path.read_text(encoding="utf-8").splitlines()) == 1
    assert (
        publisher.load_staged_session(second.dataset_root, "P01", second.session_id).file_count == 3
    )


def test_stage_session_never_packages_dotenv_files(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / ".env").write_text("MODELSCOPE_API_TOKEN=secret\n", encoding="utf-8")
    (session / "mocap").mkdir()
    (session / "mocap" / "motion.bvh").write_text("HIERARCHY\n", encoding="utf-8")
    write_inspection_report(session)

    staged = publisher.stage_session(
        session,
        "P02",
        dataset_root=tmp_path / "dataset",
        raw_video=True,
        progress=None,
    )

    assert not (staged.session_dir / ".env").exists()
    assert "secret" not in staged.manifest_path.read_text(encoding="utf-8")


def test_stage_session_requires_canonical_mocap_directory(tmp_path: Path) -> None:
    session = tmp_path / "session"
    legacy_dir = session / "nokov"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "motion.bvh").write_text("HIERARCHY\n", encoding="utf-8")
    write_inspection_report(session)

    with pytest.raises(publisher.ModelScopePublisherError, match=r"named mocap/"):
        publisher.stage_session(
            session,
            "P03",
            dataset_root=tmp_path / "dataset",
            raw_video=True,
            progress=None,
        )


def test_stage_records_device_ids_without_packaging_calibration(tmp_path: Path) -> None:
    session = tmp_path / "session"
    calibration = session / "raw_calibration"
    calibration.mkdir(parents=True)
    (session / "robowrist_wrist-left-01_left").mkdir()
    (session / "robowrist_wrist-right-01_right").mkdir()
    (session / "mocap").mkdir()
    (session / "mocap" / "motion.bvh").write_text("HIERARCHY\n", encoding="utf-8")
    (calibration / "device_ids.json").write_text(
        json.dumps(
            {
                "main_device_id": "main-device-01",
                "related_device_ids": ["ambiguous-device-a", "ambiguous-device-b"],
                "source": "Z:/private/calibration/source",
            }
        ),
        encoding="utf-8",
    )
    (calibration / "api_response.json").write_text(
        '{"signed_url":"https://private.example/signed-secret"}', encoding="utf-8"
    )
    write_inspection_report(session)

    staged = publisher.stage_session(
        session,
        "P04",
        dataset_root=tmp_path / "dataset",
        raw_video=True,
        progress=None,
    )

    assert not (staged.session_dir / "raw_calibration").exists()
    assert staged.main_device_id == "main-device-01"
    assert staged.left_device_id == "wrist-left-01"
    assert staged.right_device_id == "wrist-right-01"
    manifest_text = staged.manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["device_ids"] == {
        "main": "main-device-01",
        "left": "wrist-left-01",
        "right": "wrist-right-01",
    }
    assert "related" not in manifest["device_ids"]
    assert "raw_calibration" not in manifest_text
    assert "signed-secret" not in manifest_text
    metadata = json.loads(staged.metadata_path.read_text(encoding="utf-8"))
    assert metadata["device_ids"] == manifest["device_ids"]


def test_device_ids_use_video_tags_and_robowrist_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    session.mkdir()
    main_video = session / "robocap_segment1_video_left.mp4"
    main_video.write_bytes(b"video")
    (session / "robowrist_wrist-left-02_left").mkdir()
    (session / "robowrist_wrist-right-02_right").mkdir()
    monkeypatch.setattr(
        publisher,
        "_ffprobe_format_tags",
        lambda path, ffprobe: {
            "deviceid": "main-device-02",
            "subdevices": "ambiguous-device-a,ambiguous-device-b",
        },
    )

    assert publisher.discover_device_ids(session) == {
        "main": "main-device-02",
        "left": "wrist-left-02",
        "right": "wrist-right-02",
    }


def test_device_ids_use_explicit_wrist_position_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    streams = session / "streams"
    streams.mkdir(parents=True)
    main_video = session / "robocap_segment1_video_left.mp4"
    left_video = streams / "robowrist_segment1_video_left_down.mp4"
    right_video = streams / "robowrist_segment1_video_right_down.mp4"
    for path in (main_video, left_video, right_video):
        path.write_bytes(b"video")

    def tags(path: Path, _ffprobe: str) -> dict[str, str]:
        if path == main_video:
            return {"deviceid": "main-device-03", "subdevices": "unordered-a,unordered-b"}
        if path == left_video:
            return {"deviceid": "wrist-left-03", "position": "left", "host": "main-device-03"}
        return {"deviceid": "wrist-right-03", "position": "right", "host": "main-device-03"}

    monkeypatch.setattr(publisher, "_ffprobe_format_tags", tags)

    assert publisher.discover_device_ids(session) == {
        "main": "main-device-03",
        "left": "wrist-left-03",
        "right": "wrist-right-03",
    }


def test_stage_includes_selected_rrd_outside_artifacts_tree(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "mocap").mkdir()
    (session / "mocap" / "motion.trc").write_text("Frame#\tTime\n", encoding="utf-8")
    report = write_inspection_report(session)
    source_rrd = report.parent / "aligned.rrd"
    source_rrd.write_bytes(b"rrd")

    staged = publisher.stage_session(
        session,
        "P05",
        dataset_root=tmp_path / "dataset",
        include_rrd=True,
        raw_video=True,
        progress=None,
    )

    packaged_rrd = staged.session_dir / "rerun" / "segment1" / "inspection" / "aligned.rrd"
    assert packaged_rrd.read_bytes() == b"rrd"
    manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    rerun_records = [item for item in manifest["files"] if item["kind"] == "rerun"]
    assert rerun_records[0]["packaged_as"] == "rerun/segment1/inspection/aligned.rrd"


def test_stage_includes_only_explicitly_selected_rrd_files(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "mocap").mkdir()
    (session / "mocap" / "motion.trc").write_text("Frame#\tTime\n", encoding="utf-8")
    report = write_inspection_report(session)
    selected = report.parent / "frame.rrd"
    omitted = report.parent / "time.rrd"
    selected.write_bytes(b"frame")
    omitted.write_bytes(b"time")

    initial = publisher.stage_session(
        session,
        "P05",
        dataset_root=tmp_path / "dataset",
        segment="segment1",
        include_rrd=True,
        raw_video=True,
        progress=None,
    )
    initial_rerun_dir = initial.session_dir / "rerun" / "segment1" / "inspection"
    assert (initial_rerun_dir / "frame.rrd").is_file()
    assert (initial_rerun_dir / "time.rrd").is_file()

    staged = publisher.stage_session(
        session,
        "P05",
        dataset_root=tmp_path / "dataset",
        segment="segment1",
        rrd_files=[selected.relative_to(session)],
        raw_video=True,
        progress=None,
    )

    rerun_dir = staged.session_dir / "rerun" / "segment1" / "inspection"
    assert (rerun_dir / "frame.rrd").read_bytes() == b"frame"
    assert not (rerun_dir / "time.rrd").exists()
    manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    assert manifest["options"]["include_rrd"] is True
    assert manifest["options"]["rrd_files"] == [
        "_artifacts/segment1/inspection/frame.rrd"
    ]


def test_stage_rejects_selected_rrd_outside_current_segment(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "mocap").mkdir()
    (session / "mocap" / "motion.trc").write_text("Frame#\tTime\n", encoding="utf-8")
    write_inspection_report(session, "segment1")
    other_report = write_inspection_report(session, "segment2")
    other_rrd = other_report.parent / "other.rrd"
    other_rrd.write_bytes(b"other")

    with pytest.raises(ValueError, match="not available under segment 'segment1'"):
        publisher.stage_session(
            session,
            "P05",
            dataset_root=tmp_path / "dataset",
            segment="segment1",
            rrd_files=[other_rrd],
            raw_video=True,
            progress=None,
        )


def test_upload_staged_session_uploads_every_indexed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_fixture(tmp_path)
    second_session = tmp_path / "second_session"
    second_session.mkdir()
    (second_session / "mocap").mkdir()
    (second_session / "mocap" / "motion.bvh").write_text("HIERARCHY\n", encoding="utf-8")
    write_inspection_report(second_session)
    publisher.stage_session(
        second_session,
        "P02",
        dataset_root=staged.dataset_root,
        raw_video=True,
        progress=None,
    )
    calibration = staged.dataset_root / publisher.CALIBRATION_DIR_NAME / "main-device-01"
    calibration.mkdir(parents=True)
    (calibration / "camera.json").write_text("{}\n", encoding="utf-8")
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class FakeApi:
        def whoami(self):
            return SimpleNamespace(username="dataset-owner")

        def repo_exists(self, *args, **kwargs):
            calls.append(("repo_exists", args, kwargs))
            return False

        def create_repo(self, *args, **kwargs):
            calls.append(("create_repo", args, kwargs))

        def upload_folder(self, *args, **kwargs):
            calls.append(("upload_folder", args, kwargs))

    monkeypatch.setattr(publisher, "_hub_api", lambda _settings: FakeApi())
    settings = publisher.ModelScopeSettings(
        "secret", "https://modelscope.cn", tmp_path / ".env", ".env", "owner/egomocap"
    )

    result = publisher.upload_staged_session(
        staged,
        None,
        create_if_missing=True,
        visibility="private",
        settings=settings,
    )

    assert result.repo_url == "https://modelscope.cn/datasets/owner/egomocap"
    assert result.session_count == 2
    assert calls[0] == ("repo_exists", ("owner/egomocap", "dataset"), {})
    assert calls[1][0] == "create_repo"
    assert calls[2][0] == "upload_folder"
    assert calls[2][1][:3] == ("owner/egomocap", "dataset", staged.dataset_root)
    assert calls[2][2]["allow_patterns"] == [
        "EgoMotionActions/P01/20260803_081935_session39/**",
        "EgoMotionActions/P02/second_session/**",
        "raw_calibration/**",
        "metadata.jsonl",
        "README.md",
    ]
    assert calls[2][2]["disable_tqdm"] is False


def test_upload_requires_configured_token(tmp_path: Path) -> None:
    staged = stage_fixture(tmp_path)
    settings = publisher.ModelScopeSettings(
        None, "https://modelscope.cn", tmp_path / ".env", "missing"
    )

    with pytest.raises(publisher.ModelScopePublisherError, match="not configured"):
        publisher.upload_staged_session(staged, "owner/egomocap", settings=settings)
