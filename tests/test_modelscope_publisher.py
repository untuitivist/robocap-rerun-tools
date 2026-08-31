from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from robocap_rerun_tools import modelscope_publisher as publisher
from robocap_rerun_tools.dataset_intersection import (
    AlignedIntersectionPlan,
    FileFrameSlice,
    FrameSlice,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("p01", "P01"),
        ("Walk Sit v2", "Walk Sit v2"),
        ("左转测试", "左转测试"),
        ("custom-动作_01", "custom-动作_01"),
    ],
)
def test_validate_primitive_id_accepts_standard_and_custom_names(
    value: str, expected: str
) -> None:
    assert publisher.validate_primitive_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "action/child",
        r"action\child",
        "action:name",
        "action\nname",
        "CON",
        "con.txt",
        "action.",
        "x" * 256,
    ],
)
def test_validate_primitive_id_rejects_unsafe_directory_names(value: str) -> None:
    with pytest.raises(ValueError):
        publisher.validate_primitive_id(value)


def test_upload_batch_id_uses_local_date_and_accepts_legacy_ids() -> None:
    moment = datetime(2026, 8, 28, 17, 32, 45, tzinfo=UTC)

    assert publisher.upload_batch_id(moment) == "20260828"
    assert publisher.validate_upload_batch_id("20260828") == "20260828"
    assert publisher.validate_upload_batch_id("20260828_173245") == "20260828_173245"
    with pytest.raises(ValueError, match="valid local date"):
        publisher.validate_upload_batch_id("20260230")


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
    mocap_dir = session / "Mocap-NOKOV"
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


def test_stage_session_uses_prepared_hierarchy_and_portable_report(
    tmp_path: Path,
) -> None:
    staged = stage_fixture(tmp_path)

    expected = (
        tmp_path
        / "dataset"
        / publisher.PREPARED_DIR_NAME
        / "P01"
        / "20260803_081935_session39"
    )
    assert staged.session_dir == expected.resolve()
    assert (expected / "mocap" / "motion.trc").is_file()
    assert not (expected / "Mocap-NOKOV").exists()
    assert (expected / "robocap_segment1_video_left.mp4").read_bytes() == b"video"
    assert not (expected / "_artifacts").exists()
    report_text = staged.inspection_html.read_text(encoding="utf-8")
    assert str(tmp_path.resolve()) not in report_text
    assert '"sessionPath":"20260803_081935_session39"' in report_text
    manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    motion_record = next(item for item in manifest["files"] if item["kind"] == "data")
    assert motion_record["source"] == "Mocap-NOKOV/motion.trc"
    assert motion_record["packaged_as"] == "mocap/motion.trc"

    metadata = [
        json.loads(line) for line in staged.metadata_path.read_text(encoding="utf-8").splitlines()
    ]
    assert metadata == [
        {
            "primitive_id": "P01",
            "session_id": "20260803_081935_session39",
            "upload_batch_id": None,
            "session_path": "_prepared/P01/20260803_081935_session39",
            "manifest": "_prepared/P01/20260803_081935_session39/manifest.json",
            "inspection_html": (
                "_prepared/P01/20260803_081935_session39/"
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
    assert "EgoMotionActions/<YYYYMMDD>/<primitive_id>/<session_id>/" in dataset_readme
    assert "EgoMotionActions/Demo/<primitive_id>/<session_id>/" in dataset_readme
    assert "raw_calibration/<device_id>/" in dataset_readme
    assert "pending sessions in one upload operation share that date" in normalized_readme
    assert "Legacy `YYYYMMDD_HHMMSS` paths remain readable" in dataset_readme
    assert "rerun/<segment>/inspection/*.rrd" in dataset_readme
    assert "at least one motion-capture format must be present" in normalized_readme
    assert "under `mocap/`" in dataset_readme
    assert "Optional artifact: RRD files" in dataset_readme
    assert "Optional: third-person" not in dataset_readme
    assert "required third-person video" in dataset_readme
    assert "robowrist_<device_id>_left/" in dataset_readme
    assert "robowrist_<device_id>_right/" in dataset_readme


def test_stage_session_records_applied_aligned_intersection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    mocap_dir = session / "mocap"
    mocap_dir.mkdir(parents=True)
    video = session / "robocap_segment1_video_left.mp4"
    motion = mocap_dir / "motion.trc"
    video.write_bytes(b"video")
    motion.write_text("motion", encoding="utf-8")
    write_inspection_report(session)
    plan = AlignedIntersectionPlan(
        ratio=8,
        video_frame_offset=1,
        gt_frame_offset=8,
        reference_video=video.name,
        robocap_frames=FrameSlice(0, 2, 3),
        mocap_frames=FrameSlice(8, 24, 24),
        third_person_frames=None,
        capture_start_ns=100,
        capture_end_ns_exclusive=300,
        video_slices=(
            FileFrameSlice(video.name, "robocap_reference", FrameSlice(0, 2, 3)),
        ),
        motion_slices=(
            FileFrameSlice("mocap/motion.trc", "mocap_trc", FrameSlice(8, 24, 24)),
        ),
    )
    monkeypatch.setattr(publisher, "build_aligned_intersection_plan", lambda *args, **kwargs: plan)

    def fake_stage_aligned_file(path: Path, source: Path, target: Path, *_args, **_kwargs):
        packaged = publisher.copy_or_compress_file(
            path,
            source,
            target,
            True,
            "ffmpeg",
            540,
            28,
            "1400k",
        )
        return packaged, {"source": packaged.source, "role": "test_aligned"}

    monkeypatch.setattr(publisher, "stage_aligned_file", fake_stage_aligned_file)

    staged = publisher.stage_session(
        session,
        "P01",
        dataset_root=tmp_path / "dataset",
        segment="segment1",
        raw_video=True,
        aligned_intersection=True,
        frame_ratio=8,
        video_frame_offset=1,
        progress=None,
    )

    manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    metadata = json.loads(staged.metadata_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["options"]["aligned_intersection"] is True
    assert manifest["alignment"]["video_frame_offset"] == 1
    assert manifest["alignment"]["gt_frame_offset"] == 8
    assert manifest["alignment"]["staged_video_frame_offset"] == 0
    assert manifest["alignment"]["staged_gt_frame_offset"] == 0
    assert all("aligned_selection" in item for item in manifest["files"][:-1])
    assert metadata["alignment"]["video_frame_offset"] == 1
    assert metadata["alignment"]["staged_video_frame_offset"] == 0
    assert manifest["alignment"] == staged.alignment
    assert publisher.load_staged_session(
        staged.dataset_root, staged.primitive_id, staged.session_id
    ).alignment == staged.alignment


def test_stage_session_reports_aligned_file_failure_without_leaking_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "session"
    mocap_dir = session / "mocap"
    mocap_dir.mkdir(parents=True)
    video = session / "robocap_segment1_video_left.mp4"
    motion = mocap_dir / "motion.trc"
    video.write_bytes(b"video")
    motion.write_text("motion", encoding="utf-8")
    write_inspection_report(session)
    plan = AlignedIntersectionPlan(
        ratio=8,
        video_frame_offset=0,
        gt_frame_offset=0,
        reference_video=video.name,
        robocap_frames=FrameSlice(0, 1, 1),
        mocap_frames=FrameSlice(0, 8, 8),
        third_person_frames=None,
        capture_start_ns=100,
        capture_end_ns_exclusive=200,
        video_slices=(FileFrameSlice(video.name, "robocap_reference", FrameSlice(0, 1, 1)),),
        motion_slices=(FileFrameSlice("mocap/motion.trc", "mocap_trc", FrameSlice(0, 8, 8)),),
    )
    monkeypatch.setattr(publisher, "build_aligned_intersection_plan", lambda *args, **kwargs: plan)

    def fail_staging(*_args, **_kwargs):
        raise publisher.DatasetIntersectionError("frame crop mismatch")

    monkeypatch.setattr(publisher, "stage_aligned_file", fail_staging)

    with pytest.raises(
        publisher.ModelScopePublisherError,
        match=r"motion\.trc: frame crop mismatch",
    ):
        publisher.stage_session(
            session,
            "P01",
            dataset_root=tmp_path / "dataset",
            aligned_intersection=True,
            frame_ratio=8,
            raw_video=True,
            progress=None,
        )


def test_dataset_readme_contains_complete_action_task_catalog() -> None:
    dataset_readme = publisher._dataset_readme()

    assert "license: Apache License 2.0" in dataset_readme
    assert "## Download" in dataset_readme
    assert ':modelscope-code[]{type="sdk"}' in dataset_readme
    assert ':modelscope-code[]{type="git"}' in dataset_readme
    assert "### Update a local copy" in dataset_readme
    assert "git pull --ff-only" in dataset_readme
    assert "git lfs pull" in dataset_readme
    assert "running the SDK download command again" in dataset_readme
    assert "uv venv" not in dataset_readme
    assert "uv pip" not in dataset_readme
    assert "modelscope login" not in dataset_readme
    assert 'record["primitive_id"]' in dataset_readme
    assert "## Global collection rules" in dataset_readme
    assert "## Action task catalog (P01-P29)" in dataset_readme
    assert "Episodes / participant` is not yet" in dataset_readme
    for group in (
        "Speed & stopping",
        "Turning & trajectories",
        "Non-forward motion",
        "Composed & long sequences",
        "Full-body transitions",
        "Terrain & navigation",
    ):
        assert f"### {group}\n" in dataset_readme
    for number in range(1, 30):
        assert dataset_readme.count(f"| P{number:02d} |") == 1
    for definition in (
        "Start still; walk straight at normal comfortable speed; stop at endpoint.",
        "Walk through 3–5 turns in sequence; keep moving; stop at end.",
        "Walk backward slowly; stop.",
        "Walk continuously around room; naturally change direction/speed; no manipulation.",
        "Walk to chair; sit ~2 s; stand; walk away.",
        "Start at marker; visit targets in order; choose natural paths; avoid obstacles; stop at final target.",
    ):
        assert definition in dataset_readme


def test_write_dataset_readme_refreshes_existing_generated_card(tmp_path: Path) -> None:
    readme_path = tmp_path / publisher.DATASET_README_NAME
    readme_path.write_text("stale generated card\n", encoding="utf-8")

    written = publisher._write_dataset_readme(tmp_path)

    assert written == readme_path
    assert readme_path.read_text(encoding="utf-8") == publisher._dataset_readme()


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


def test_stage_session_rejects_legacy_nokov_directory(tmp_path: Path) -> None:
    session = tmp_path / "session"
    legacy_dir = session / "nokov"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "motion.bvh").write_text("HIERARCHY\n", encoding="utf-8")
    write_inspection_report(session)

    with pytest.raises(publisher.ModelScopePublisherError, match=r"must start with mocap"):
        publisher.stage_session(
            session,
            "P03",
            dataset_root=tmp_path / "dataset",
            raw_video=True,
            progress=None,
        )


def test_stage_session_rejects_multiple_mocap_prefix_directories(tmp_path: Path) -> None:
    session = tmp_path / "session"
    for name in ("mocap_take01", "Mocap-Backup"):
        mocap_dir = session / name
        mocap_dir.mkdir(parents=True)
        (mocap_dir / "motion.bvh").write_text("HIERARCHY\n", encoding="utf-8")
    write_inspection_report(session)

    with pytest.raises(publisher.ModelScopePublisherError, match=r"multiple mocap\* directories"):
        publisher.stage_session(
            session,
            "P03",
            dataset_root=tmp_path / "dataset",
            raw_video=True,
            progress=None,
        )


def test_find_mocap_files_discovers_only_packageable_files_in_prefixed_directory(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session"
    mocap_dir = session / "Mocap-NOKOV"
    nested = mocap_dir / "take01"
    nested.mkdir(parents=True)
    motion = nested / "motion.trc"
    rigid_body = mocap_dir / "rigid-body.csv"
    motion.write_text("Frame#\tTime\n", encoding="utf-8")
    rigid_body.write_text("frame,x,y,z\n", encoding="utf-8")
    (mocap_dir / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (mocap_dir / "preview.rrd").write_bytes(b"rrd")
    (session / "outside.txt").write_text("outside\n", encoding="utf-8")

    assert publisher.find_mocap_files(session) == [rigid_body, motion]


def test_resolve_mocap_files_accepts_relative_and_absolute_paths_and_deduplicates(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session"
    mocap_dir = session / "mocap_take01"
    mocap_dir.mkdir(parents=True)
    motion = mocap_dir / "motion.trc"
    rigid_body = mocap_dir / "rigid-body.csv"
    motion.write_text("Frame#\tTime\n", encoding="utf-8")
    rigid_body.write_text("frame,x,y,z\n", encoding="utf-8")

    resolved = publisher.resolve_mocap_files(
        session,
        [motion.relative_to(session), rigid_body, motion.resolve()],
    )

    assert resolved == [motion, rigid_body]


def test_resolve_mocap_files_rejects_empty_or_unavailable_selection(tmp_path: Path) -> None:
    session = tmp_path / "session"
    mocap_dir = session / "mocap"
    mocap_dir.mkdir(parents=True)
    (mocap_dir / "motion.trc").write_text("Frame#\tTime\n", encoding="utf-8")
    outside = session / "outside.csv"
    outside.write_text("frame,x,y,z\n", encoding="utf-8")

    with pytest.raises(ValueError, match="at least one Mocap file"):
        publisher.resolve_mocap_files(session, [])
    with pytest.raises(ValueError, match=r"not available under the Session mocap\*"):
        publisher.resolve_mocap_files(session, [outside])


def test_stage_session_synchronizes_explicit_mocap_selection(tmp_path: Path) -> None:
    session = tmp_path / "session"
    mocap_dir = session / "Mocap-NOKOV"
    mocap_dir.mkdir(parents=True)
    selected = mocap_dir / "motion.trc"
    omitted = mocap_dir / "rigid-body.csv"
    selected.write_text("Frame#\tTime\n", encoding="utf-8")
    omitted.write_text("frame,x,y,z\n", encoding="utf-8")
    write_inspection_report(session)

    initial = publisher.stage_session(
        session,
        "P05",
        dataset_root=tmp_path / "dataset",
        raw_video=True,
        progress=None,
    )
    assert (initial.session_dir / "mocap" / "motion.trc").is_file()
    assert (initial.session_dir / "mocap" / "rigid-body.csv").is_file()

    staged = publisher.stage_session(
        session,
        "P05",
        dataset_root=tmp_path / "dataset",
        mocap_files=[selected.relative_to(session)],
        raw_video=True,
        progress=None,
    )

    assert (staged.session_dir / "mocap" / "motion.trc").is_file()
    assert not (staged.session_dir / "mocap" / "rigid-body.csv").exists()
    manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    assert manifest["options"]["mocap_selection"] == "explicit"
    assert manifest["options"]["mocap_files"] == ["Mocap-NOKOV/motion.trc"]
    assert not any(item["source"] == "Mocap-NOKOV/rigid-body.csv" for item in manifest["files"])


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
        "Walk [v2]",
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

        def upload_file(self, *args, **kwargs):
            calls.append(("upload_file", args, kwargs))

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
        upload_time=datetime(2026, 8, 28, 17, 32, 45, tzinfo=UTC),
        progress=None,
    )

    assert result.repo_url == "https://modelscope.cn/datasets/owner/egomocap"
    assert result.session_count == 2
    assert result.batch_id == "20260828"
    assert calls[0] == ("repo_exists", ("owner/egomocap", "dataset"), {})
    assert calls[1][0] == "create_repo"
    assert calls[2][0] == "upload_folder"
    assert calls[2][1][:3] == ("owner/egomocap", "dataset", staged.dataset_root)
    assert calls[2][2]["allow_patterns"] == [
        "EgoMotionActions/20260828/P01/20260803_081935_session39/**",
        "EgoMotionActions/20260828/Walk [[]v2]/second_session/**",
        "raw_calibration/**",
        "README.md",
    ]
    assert calls[2][2]["disable_tqdm"] is False
    assert calls[2][2]["commit_message"] == (
        "Upload batch 20260828 with 2 indexed session(s)"
    )
    assert calls[3][0] == "upload_file"
    assert calls[3][1][:2] == ("owner/egomocap", "dataset")
    assert calls[3][1][3] == publisher.METADATA_NAME
    uploaded_metadata = [
        json.loads(line) for line in calls[3][1][2].decode("utf-8").splitlines()
    ]
    assert len(uploaded_metadata) == 2
    final_session = (
        staged.dataset_root
        / publisher.ACTIONS_DIR_NAME
        / "20260828"
        / "P01"
        / staged.session_id
    )
    assert final_session.is_dir()
    assert not staged.session_dir.exists()
    metadata = [
        json.loads(line) for line in staged.metadata_path.read_text(encoding="utf-8").splitlines()
    ]
    assert {entry["upload_batch_id"] for entry in metadata} == {"20260828"}
    assert all(
        entry["session_path"].startswith("EgoMotionActions/20260828/")
        for entry in metadata
    )


def test_upload_retry_reuses_existing_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staged = stage_fixture(tmp_path)
    upload_calls: list[dict[str, object]] = []
    remote_metadata = [b""]
    downloaded_metadata = tmp_path / "remote_metadata.jsonl"

    class FakeApi:
        def whoami(self):
            return SimpleNamespace(username="dataset-owner")

        def repo_exists(self, *args, **kwargs):
            return True

        def download_file(self, *args, **kwargs):
            downloaded_metadata.write_bytes(remote_metadata[0])
            return downloaded_metadata

        def upload_folder(self, *args, **kwargs):
            upload_calls.append(kwargs)

        def upload_file(self, *args, **kwargs):
            remote_metadata[0] = args[2]

    monkeypatch.setattr(publisher, "_hub_api", lambda _settings: FakeApi())
    settings = publisher.ModelScopeSettings(
        "secret", "https://modelscope.cn", tmp_path / ".env", ".env", "owner/egomocap"
    )
    first = publisher.upload_staged_dataset(
        publisher.load_staged_dataset(staged.dataset_root),
        None,
        settings=settings,
        upload_time=datetime(2026, 8, 28, 17, 32, 45, tzinfo=UTC),
        progress=None,
    )
    second = publisher.upload_staged_dataset(
        publisher.load_staged_dataset(staged.dataset_root),
        None,
        settings=settings,
        upload_time=datetime(2026, 8, 29, 9, 0, 0, tzinfo=UTC),
        progress=None,
    )

    assert first.batch_id == second.batch_id == "20260828"
    assert upload_calls[0]["allow_patterns"] == upload_calls[1]["allow_patterns"]


def test_failed_file_transfer_retry_reuses_finalized_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = stage_fixture(tmp_path)
    remote_metadata = tmp_path / "remote_metadata.jsonl"
    remote_metadata.write_text("", encoding="utf-8")
    folder_attempts = 0

    class FakeApi:
        def whoami(self):
            return SimpleNamespace(username="dataset-owner")

        def repo_exists(self, *args, **kwargs):
            return True

        def download_file(self, *args, **kwargs):
            return remote_metadata

        def upload_folder(self, *args, **kwargs):
            nonlocal folder_attempts
            folder_attempts += 1
            if folder_attempts == 1:
                raise OSError("simulated transfer interruption")

        def upload_file(self, *args, **kwargs):
            return None

    monkeypatch.setattr(publisher, "_hub_api", lambda _settings: FakeApi())
    settings = publisher.ModelScopeSettings(
        "secret", "https://modelscope.cn", tmp_path / ".env", ".env", "owner/egomocap"
    )

    with pytest.raises(publisher.ModelScopePublisherError, match="transfer interruption"):
        publisher.upload_staged_dataset(
            publisher.load_staged_dataset(staged.dataset_root),
            None,
            settings=settings,
            upload_time=datetime(2026, 8, 28, 17, 32, 45, tzinfo=UTC),
            progress=None,
        )

    retry = publisher.upload_staged_dataset(
        publisher.load_staged_dataset(staged.dataset_root),
        None,
        settings=settings,
        upload_time=datetime(2026, 8, 29, 9, 0, 0, tzinfo=UTC),
        progress=None,
    )

    assert retry.batch_id == "20260828"
    assert folder_attempts == 2


def test_merge_metadata_retains_demo_and_overrides_matching_remote_session() -> None:
    remote = [
        {
            "primitive_id": "P01",
            "session_id": "legacy",
            "upload_batch_id": "Demo",
            "session_path": "EgoMotionActions/Demo/P01/legacy",
        },
        {
            "primitive_id": "P02",
            "session_id": "repeat",
            "upload_batch_id": "Demo",
            "session_path": "EgoMotionActions/Demo/P02/repeat",
        },
    ]
    local = [
        {
            "primitive_id": "P02",
            "session_id": "repeat",
            "upload_batch_id": "20260828_120000",
            "session_path": "EgoMotionActions/20260828_120000/P02/repeat",
        }
    ]

    merged = publisher.merge_metadata_entries(remote, local)

    assert len(merged) == 2
    assert next(item for item in merged if item["session_id"] == "legacy") == remote[0]
    assert next(item for item in merged if item["session_id"] == "repeat") == local[0]


def test_load_staged_dataset_accepts_demo_metadata(tmp_path: Path) -> None:
    staged = stage_fixture(tmp_path)
    demo_path = (
        staged.dataset_root
        / publisher.ACTIONS_DIR_NAME
        / publisher.DEMO_DIR_NAME
        / staged.primitive_id
        / staged.session_id
    )
    demo_path.parent.mkdir(parents=True)
    staged.session_dir.replace(demo_path)
    metadata = json.loads(staged.metadata_path.read_text(encoding="utf-8"))
    relative = publisher.demo_session_path(staged.primitive_id, staged.session_id)
    metadata["upload_batch_id"] = publisher.DEMO_DIR_NAME
    metadata["session_path"] = relative
    metadata["manifest"] = f"{relative}/manifest.json"
    metadata["inspection_html"] = f"{relative}/{publisher.REPORT_NAME}"
    staged.metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")

    dataset = publisher.load_staged_dataset(staged.dataset_root)

    assert dataset.session_paths == (relative,)
    assert dataset.pending_session_paths == ()
    assert dataset.batch_ids == ()


def test_finalize_upload_batch_migrates_legacy_local_stage(tmp_path: Path) -> None:
    staged = stage_fixture(tmp_path)
    legacy_path = (
        staged.dataset_root
        / publisher.ACTIONS_DIR_NAME
        / staged.primitive_id
        / staged.session_id
    )
    legacy_path.parent.mkdir(parents=True)
    staged.session_dir.replace(legacy_path)
    metadata = json.loads(staged.metadata_path.read_text(encoding="utf-8"))
    legacy_relative = publisher.legacy_session_path(staged.primitive_id, staged.session_id)
    metadata["session_path"] = legacy_relative
    metadata["manifest"] = f"{legacy_relative}/manifest.json"
    metadata["inspection_html"] = f"{legacy_relative}/{publisher.REPORT_NAME}"
    staged.metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")

    finalized, batch_id = publisher.finalize_upload_batch(
        publisher.load_staged_dataset(staged.dataset_root),
        upload_time=datetime(2026, 8, 28, 18, 0, 1, tzinfo=UTC),
        progress=None,
    )

    assert batch_id == "20260828"
    assert finalized.session_paths == (
        "EgoMotionActions/20260828/P01/20260803_081935_session39",
    )
    assert finalized.pending_session_paths == ()
    assert not legacy_path.exists()


def test_finalize_upload_batch_replaces_same_session_on_same_date(tmp_path: Path) -> None:
    staged = stage_fixture(tmp_path)
    finalized, first_batch = publisher.finalize_upload_batch(
        publisher.load_staged_dataset(staged.dataset_root),
        upload_time=datetime(2026, 8, 28, 18, 0, 1, tzinfo=UTC),
        progress=None,
    )
    published = staged.dataset_root / Path(finalized.session_paths[0])
    (published / "stale.txt").write_text("old", encoding="utf-8")

    source = tmp_path / staged.session_id
    (source / "Mocap-NOKOV" / "motion.trc").write_text("updated\n", encoding="utf-8")
    restaged = publisher.stage_session(
        source,
        staged.primitive_id,
        dataset_root=staged.dataset_root,
        segment="segment1",
        raw_video=True,
        progress=None,
    )
    finalized, second_batch = publisher.finalize_upload_batch(
        publisher.load_staged_dataset(restaged.dataset_root),
        upload_time=datetime(2026, 8, 28, 20, 30, 0, tzinfo=UTC),
        progress=None,
    )

    assert first_batch == second_batch == "20260828"
    assert finalized.session_paths == (
        "EgoMotionActions/20260828/P01/20260803_081935_session39",
    )
    assert not (published / "stale.txt").exists()
    assert (published / "mocap" / "motion.trc").read_text(encoding="utf-8") == "updated\n"
    manifest = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["upload_batch_created_at"] == "2026-08-28T20:30:00+00:00"


def test_upload_requires_configured_token(tmp_path: Path) -> None:
    staged = stage_fixture(tmp_path)
    settings = publisher.ModelScopeSettings(
        None, "https://modelscope.cn", tmp_path / ".env", "missing"
    )

    with pytest.raises(publisher.ModelScopePublisherError, match="not configured"):
        publisher.upload_staged_session(staged, "owner/egomocap", settings=settings)


def test_fetch_remote_session_keys_uses_metadata_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = tmp_path / "metadata.jsonl"
    metadata.write_text(
        "\n".join(
            [
                json.dumps({"primitive_id": "p01", "session_id": "session-one"}),
                json.dumps({"primitive_id": "Custom Walk", "session_id": "session-two"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[tuple[object, ...]] = []

    class FakeApi:
        def repo_exists(self, *args):
            calls.append(("repo_exists", *args))
            return True

        def download_file(self, *args, **kwargs):
            calls.append(("download_file", *args, kwargs))
            return metadata

    monkeypatch.setattr(publisher, "_hub_api", lambda _settings: FakeApi())
    settings = publisher.ModelScopeSettings(
        "secret", "https://modelscope.cn", tmp_path / ".env", ".env", "owner/egomocap"
    )

    keys = publisher.fetch_remote_session_keys(settings=settings)

    assert keys == frozenset({("P01", "session-one"), ("Custom Walk", "session-two")})
    assert calls[0] == ("repo_exists", "owner/egomocap", "dataset")
    assert calls[1][0:4] == (
        "download_file",
        "owner/egomocap",
        "dataset",
        publisher.METADATA_NAME,
    )
    assert calls[1][4] == {"revision": "master", "force": True}


def test_fetch_remote_session_keys_rejects_missing_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = SimpleNamespace(repo_exists=lambda *_args: False)
    monkeypatch.setattr(publisher, "_hub_api", lambda _settings: api)
    settings = publisher.ModelScopeSettings(
        "secret", "https://modelscope.cn", tmp_path / ".env", ".env", "owner/missing"
    )

    with pytest.raises(publisher.ModelScopePublisherError, match="does not exist"):
        publisher.fetch_remote_session_keys(settings=settings)
