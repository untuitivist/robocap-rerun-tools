from pathlib import Path

import numpy as np

import robocap_rerun_tools.exporter as exporter
from robocap_rerun_tools.exporter import (
    discover_gt_dir,
    discover_gt_file_sets,
    discover_session,
    make_proxy_video,
    parse_nokov_csv,
    parse_trc,
    parse_xrs,
    robocap_sensors_container,
    synthesize_frame_aligned_timestamps,
)


def test_frame_alignment_offset_uses_reference_video_frames() -> None:
    reference_timestamps = np.arange(20, dtype=np.int64) * 100

    aligned = synthesize_frame_aligned_timestamps(
        frame_count=49,
        reference_video_timestamps=reference_timestamps,
        ratio=8.0,
        video_frame_offset=5,
    )

    assert aligned[40] == reference_timestamps[0]
    assert aligned[48] == reference_timestamps[1]


def test_parse_trc_reads_multiple_markers(tmp_path: Path) -> None:
    path = tmp_path / "Tracker0.trc"
    path.write_text(
        "\n".join(
            [
                "PathFileType\t4\t(X/Y/Z)\ttest.trc",
                "DataRate\tCameraRate\tNumFrames\tNumMarkers\tUnits\tOrigDataRate\tOrigDataStartFrame\tOrigNumFrames\tuseTimeStamp",
                "90\t90\t1\t4\tmm\t90\t1\t1\t1",
                "Frame#\tTime\tTimestamp\tMarker1\t\t\tMarker2\t\t\tMarker3\t\t\tMarker4\t\t\t",
                "\t\t\tX1\tY1\tZ1\tX2\tY2\tZ2\tX3\tY3\tZ3\tX4\tY4\tZ4",
                "",
                "1\t0.000\t1000\t1\t2\t3\t4\t5\t6\t7\t8\t9\t10\t11\t12",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    timestamps_ns, marker_names, positions = parse_trc(path, 1.0, None, marker_limit=4)
    assert timestamps_ns.tolist() == [1_000_000_000]
    assert marker_names == ("Marker1", "Marker2", "Marker3", "Marker4")
    assert positions.shape == (1, 4, 3)


def test_parse_nokov_csv_keeps_only_real_segment_nodes(tmp_path: Path) -> None:
    path = tmp_path / "Tracker0.csv"
    path.write_text(
        "\n".join(
            [
                "#Hierarchical Translation and Rotation (.csv) file",
                "[Head]",
                "NumFrames,NumSegments,DataFrameRate,EulerRotationOrder,BoneAxis,TranslationUnits",
                "1,1,90,ZYX,Y,mm",
                "",
                "[SegmentNames&Hierarchy]",
                "Segment,Parent",
                "Segment1,",
                "",
                "[SegmentData]",
                " Frame# ,,Segment1,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,",
                ",Timestamp,XToGlobal1,YToGlobal1,ZToGlobal1,VxToGlobal1,VyToGlobal1,VzToGlobal1,VrToGlobal1,AxToGlobal1,AyToGlobal1,AzToGlobal1,ArToGlobal1,QxToGlobal1,QyToGlobal1,QzToGlobal1,QwToGlobal1,ExToGlobal1,EyToGlobal1,EzToGlobal1,EVxToGlobal1,EVyToGlobal1,EVzToGlobal1,EAxToGlobal1,EAyToGlobal1,EAzToGlobal1,Segmentlength1,",
                "1,1000,1,2,3,,,,,,,,,0,0,0,1,,,,,,,,,,100,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    timestamps_ns, marker_names, positions, parents = parse_nokov_csv(path, 1.0, None)
    assert timestamps_ns.tolist() == [1_000_000_000]
    assert marker_names == ("Segment1",)
    assert positions.shape == (1, 1, 3)
    assert parents == (-1,)
    assert exporter.infer_nokov_view_up_axis([path]) == "z"


def test_parse_xrs_reads_multiple_segments_in_one_space(tmp_path: Path) -> None:
    path = tmp_path / "Tracker0.xrs"
    path.write_text(
        "\n".join(
            [
                "# Hierarchical Translation and Rotation (.xrs) file",
                "[Head]",
                "NumFrames NumSegments DataFrameRate EulerRotationOrder BoneAxis TranslationUnits",
                "1 2 90 ZYX Y mm",
                "",
                "[SegmentNames&Hierarchy]",
                "Segment Parent",
                "RigidA",
                "RigidB RigidA",
                "",
                "[SegmentData]",
                " Frame#   RigidA                         RigidB",
                "Timestamp XToGlobal1 YToGlobal1 ZToGlobal1 QxToGlobal1 QyToGlobal1 QzToGlobal1 QwToGlobal1 Segmentlength1 XToGlobal2 YToGlobal2 ZToGlobal2 QxToGlobal2 QyToGlobal2 QzToGlobal2 QwToGlobal2 Segmentlength2",
                "1 1000 1 2 3 0 0 0 1 100 4 5 6 0 0 0 1 100",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    timestamps_ns, marker_names, positions, parents = parse_xrs(path, 1.0, None)
    assert timestamps_ns.tolist() == [1_000_000_000]
    assert marker_names == ("RigidA", "RigidB")
    assert positions.shape == (1, 2, 3)
    assert parents == (-1, 0)


def test_parse_xrs_preserves_empty_tab_columns_and_fixed_axis_count(tmp_path: Path) -> None:
    path = tmp_path / "Body0_Left.xrs"
    path.write_text(
        "\n".join(
            [
                "# Hierarchical Translation and Rotation (.xrs) file",
                "[SegmentNames&Hierarchy]",
                "Segment\tParent",
                "LeftHand\t",
                "",
                "[SegmentData]",
                "\tFrame#\tLeftHand",
                "\tTimestamp\tXToGlobal1\tYToGlobal1\tZToGlobal1\tQxToGlobal1\tQyToGlobal1\tQzToGlobal1\tQwToGlobal1\tSegmentlength1",
                "",
                "1\t1000\t1\t2\t3\t\t\t\t\t100",
                "2\t1011\t2\t3\t4\t0\t0\t0\t1\t100",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    timestamps_ns, marker_names, positions, parents = parse_xrs(path, 1.0, None)
    assert timestamps_ns.tolist() == [1_000_000_000, 1_011_000_000]
    assert marker_names == ("LeftHand",)
    assert positions.shape == (2, 1, 3)
    assert positions[0, 0].tolist() == [1.0, 2.0, 3.0]
    assert parents == (-1,)


def test_parse_xrs_hierarchy_ignores_fixed_width_empty_tab_columns() -> None:
    lines = [
        "[SegmentNames&Hierarchy]",
        "Segment\t\t\tParent",
        "LeftHand\t\t\t",
        "LeftHandThumb0\t\t\tLeftHand",
        "LeftHandThumb1\t\t\tLeftHandThumb0",
        "[SegmentData]",
    ]

    names, parents = exporter.parse_nokov_segment_hierarchy(lines, "xrs")

    assert names == ("LeftHand", "LeftHandThumb0", "LeftHandThumb1")
    assert parents == (-1, 0, 1)


def test_nokov_bone_axis_z_maps_to_y_up(tmp_path: Path) -> None:
    path = tmp_path / "hand.xrs"
    path.write_text(
        "\n".join(
            [
                "[Head]",
                "NumFrames\t\tBoneAxis\t\tTranslationUnits",
                "1\t\tZ\t\tmm",
            ]
        ),
        encoding="utf-8",
    )
    assert exporter.infer_nokov_view_up_axis([path]) == "y"


def test_mano_retarget_uses_source_script_bvh_joint_mapping() -> None:
    parents = np.asarray([-1, 0, 1, 2, 0, 4, 5, 0, 7, 8, 0, 10, 11, 0, 13, 14], dtype=np.int64)
    joints = np.zeros((16, 3), dtype=np.float32)
    chain_starts = {
        1: (0.035, 0.004, 0.045),
        4: (0.012, 0.000, 0.052),
        7: (-0.038, -0.003, 0.040),
        10: (-0.014, 0.002, 0.049),
        13: (0.043, -0.012, 0.018),
    }
    for start, value in chain_starts.items():
        joints[start] = value
        joints[start + 1] = joints[start] + np.asarray([0.002, 0.003, 0.030], dtype=np.float32)
        joints[start + 2] = joints[start + 1] + np.asarray([0.001, -0.002, 0.024], dtype=np.float32)
    template = exporter.ManoTemplate(
        vertices=joints.copy(),
        faces=np.empty((0, 3), dtype=np.uint32),
        joints=joints,
        parents=parents,
        weights=np.eye(16, dtype=np.float32),
    )
    translation = np.asarray([1.0, 2.0, 3.0], dtype=np.float32)
    names = ["WristM", "WristIn", "WristOut", "HandOffset"]
    marker_positions = [
        translation,
        translation + [-0.01, 0.0, 0.0],
        translation + [0.01, 0.0, 0.0],
        translation,
    ]
    expected_targets = {0: translation}
    for finger, indexes in exporter.MANO_CHAIN_NAMES.items():
        prefix = f"LeftHand{'Pinky' if finger == 'pinky' else finger.capitalize()}"
        for marker_number, joint_index in enumerate(indexes):
            target = translation + np.asarray(
                [0.01 * joint_index, 0.02 * (marker_number + 1), 0.005 * joint_index],
                dtype=np.float32,
            )
            names.append(f"{prefix}{marker_number}")
            marker_positions.append(target)
            expected_targets[joint_index] = target
    positions = np.asarray(marker_positions, dtype=np.float32)
    track = exporter.GTMarkerTrack(
        label="left_hand",
        entity="gt/tracks/trc/left_hand",
        source="trc",
        timestamps_ns=np.asarray([0], dtype=np.int64),
        positions=positions[None, ...],
        marker_names=tuple(names),
    )

    target, root_rotation, scale = exporter.target_mano_joints_from_track(
        track, template, "left", positions
    )
    assert exporter.finger_joint_names("left", "index") == (
        ("LeftHandIndex0", "FingerIndex1"),
        ("LeftHandIndex1", "FingerIndex2"),
        ("LeftHandIndex2", "FingerIndex3"),
    )
    assert scale > 0
    assert np.isfinite(root_rotation).all()
    for joint_index, expected in expected_targets.items():
        assert np.allclose(target[joint_index], expected, atol=1e-6)

    posed = exporter.posed_vertices_from_joints(template, target, root_rotation, scale)
    assert np.allclose(posed, target, atol=1e-5)


def test_normalize_mano_template_centers_and_scales_vertices() -> None:
    vertices = np.asarray([[-2.0, 0.0, 0.0], [0.0, 1.0, 0.0], [2.0, 0.0, 0.0]], dtype=np.float32)
    template = exporter.ManoTemplate(
        vertices=vertices,
        faces=np.empty((0, 3), dtype=np.uint32),
        joints=vertices.copy(),
        parents=np.asarray([-1, 0, 1], dtype=np.int64),
        weights=np.eye(3, dtype=np.float32),
    )

    normalized = exporter.normalize_mano_template(template)

    assert np.allclose(normalized.vertices.mean(axis=0), 0.0, atol=1e-6)
    radii = np.linalg.norm(normalized.vertices, axis=1)
    assert np.isclose(np.percentile(radii, 95), 1.0)
    assert np.allclose(normalized.joints, normalized.vertices)


def test_gt_logging_writes_one_geometry_batch_per_timestamp(monkeypatch) -> None:
    logged = []
    capture_times = []
    monkeypatch.setattr(
        exporter.rr,
        "log",
        lambda entity, archetype, **kwargs: logged.append((entity, archetype, kwargs)),
    )
    monkeypatch.setattr(exporter, "set_capture_time", capture_times.append)

    timestamps_ns = np.asarray([1_000_000_000, 2_000_000_000], dtype=np.int64)
    marker_track = exporter.GTMarkerTrack(
        label="hand",
        entity="gt/tracks/trc/hand",
        source="trc",
        timestamps_ns=timestamps_ns,
        positions=np.asarray(
            [
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
                [[0.0, 1.0, 0.0], [1.0, 1.0, 0.0]],
            ],
            dtype=np.float32,
        ),
        marker_names=("root", "tip"),
        connections=((0, 1),),
    )
    mesh_track = exporter.GTManoMeshTrack(
        label="hand_mano_mesh",
        entity="gt/mesh/trc/hand_mano_mesh",
        source="trc",
        timestamps_ns=timestamps_ns,
        vertices=np.asarray(
            [
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                [[0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [0.0, 1.0, 1.0]],
            ],
            dtype=np.float32,
        ),
        faces=np.asarray([[0, 1, 2]], dtype=np.uint32),
    )

    exporter.log_gt_marker_track(marker_track, None)
    exporter.log_gt_mano_mesh(mesh_track, None)

    assert capture_times == [1_000_000_000, 2_000_000_000] * 2
    assert [entity for entity, _archetype, _kwargs in logged] == [
        "gt/tracks/trc/hand/labels",
        "gt/tracks/trc/hand/points",
        "gt/tracks/trc/hand/connections",
        "gt/tracks/trc/hand/points",
        "gt/tracks/trc/hand/connections",
        "gt/mesh/trc/hand_mano_mesh",
        "gt/mesh/trc/hand_mano_mesh",
    ]


def test_gt_overview_omits_absent_mesh_and_video_views() -> None:
    marker_track = exporter.GTMarkerTrack(
        label="hand",
        entity="gt/tracks/trc/hand",
        source="trc",
        timestamps_ns=np.asarray([0], dtype=np.int64),
        positions=np.zeros((1, 1, 3), dtype=np.float32),
        marker_names=("root",),
    )
    gt_config = exporter.GTConfig(
        skeleton=None,
        mesh=None,
        marker_tracks=(marker_track,),
        mano_mesh_tracks=(),
        third_person_video=None,
    )

    assert exporter.gt_mesh_tabs(gt_config) is None
    assert exporter.gt_third_person_video_view(gt_config) is None
    assert exporter.gt_overview_container(gt_config).name == "GT skeleton"
    assert exporter.gt_overview_container(None) is None


def test_discover_gt_file_sets_uses_all_supported_files(tmp_path: Path) -> None:
    for name in ("Tracker0.xrs", "Tracker1.xrs", "LeftHand.bvh", "LeftHand.trc", "extra.csv"):
        (tmp_path / name).write_text("", encoding="utf-8")
    file_sets = discover_gt_file_sets(tmp_path)
    assert [file_set.label for file_set in file_sets] == [
        "LeftHand",
        "extra",
        "Tracker0",
        "Tracker1",
    ]
    assert file_sets[0].bvh is not None
    assert file_sets[0].trc is not None
    assert sum(1 for file_set in file_sets if file_set.xrs is not None) == 2


def test_discover_gt_dir_accepts_nokov_folder_and_ignores_robowrist(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    nokov_dir = session_dir / "nokov"
    robowrist_dir = session_dir / "robowrist_device_left"
    nokov_dir.mkdir(parents=True)
    robowrist_dir.mkdir()
    (nokov_dir / "Tracker4.trc").write_text("", encoding="utf-8")
    (robowrist_dir / "sensor.csv").write_text("", encoding="utf-8")

    assert discover_gt_dir(session_dir, None) == nokov_dir


def test_make_proxy_video_rebuilds_nonempty_unreadable_file(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "camera.mp4"
    source.write_bytes(b"source")
    proxy_dir = tmp_path / "proxy"
    target = proxy_dir / "camera_h540_crf28.mp4"
    target.parent.mkdir()
    target.write_bytes(b"truncated")

    class FakeAssetVideo:
        def __init__(self, path):
            self.path = Path(path)

        def read_frame_timestamps_nanos(self):
            if self.path == target:
                raise RuntimeError("moov atom not found")
            return [0]

    monkeypatch.setattr(exporter.rr, "AssetVideo", FakeAssetVideo)
    monkeypatch.setattr(exporter, "ffmpeg_encoder_args", lambda *_args: [])

    def fake_run(args, check):
        assert check is True
        Path(args[-1]).write_bytes(b"complete")

    monkeypatch.setattr(exporter.subprocess, "run", fake_run)

    result = make_proxy_video(source, proxy_dir, 540, 28, "2500k", "ffmpeg")

    assert result == target
    assert target.read_bytes() == b"complete"


def test_discover_session_can_exclude_mag_and_imu_streams(tmp_path: Path) -> None:
    for name in (
        "robocap_segment1_video_left.mp4",
        "robocap_segment1_mag_middle.db",
        "robocap_segment1_imu_left.db",
        "robocap_segment1_imu_right.db",
    ):
        (tmp_path / name).write_bytes(b"")

    without_mag = discover_session(tmp_path, "segment1", include_mag=False)
    assert "middle_mag" not in without_mag.signals
    assert "left_robocap_acc" in without_mag.signals
    assert "right_robocap_gyro" in without_mag.signals

    without_imu = discover_session(tmp_path, "segment1", include_imu=False)
    assert "middle_mag" in without_imu.signals
    assert not any(label.endswith(("_acc", "_gyro")) for label in without_imu.signals)


def test_robocap_sensor_layout_spans_mag_across_two_imu_rows(tmp_path: Path) -> None:
    for name in (
        "robocap_segment1_video_left.mp4",
        "robocap_segment1_mag_middle.db",
        "robocap_segment1_imu_left.db",
        "robocap_segment1_imu_right.db",
    ):
        (tmp_path / name).write_bytes(b"")

    config = discover_session(tmp_path, "segment1")
    container = robocap_sensors_container(config)
    mag_view, imu_column = container.contents

    assert container.column_shares == [1.0, 2.0]
    assert mag_view.name == "middle_mag"
    assert [[view.name for view in row.contents] for row in imu_column.contents] == [
        ["left_robocap_acc", "left_robocap_gyro"],
        ["right_robocap_acc", "right_robocap_gyro"],
    ]


def test_robocap_sensor_layout_is_omitted_without_mag_or_imu(tmp_path: Path) -> None:
    (tmp_path / "robocap_segment1_video_left.mp4").write_bytes(b"")
    config = discover_session(tmp_path, "segment1")

    assert robocap_sensors_container(config) is None
    assert exporter.all_signals_container(config) is None
