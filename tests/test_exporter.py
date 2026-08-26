import sqlite3
from dataclasses import replace
from pathlib import Path

import numpy as np

import robocap_rerun_tools.exporter as exporter
from robocap_rerun_tools.alignment import FrameAlignment
from robocap_rerun_tools.exporter import (
    discover_gt_dir,
    discover_gt_file_sets,
    discover_session,
    make_proxy_video,
    parse_nokov_csv,
    parse_trc,
    parse_xrs,
    robocap_sensors_container,
    robowrist_stream_labels,
    sensors_container,
    synthesize_frame_aligned_timestamps,
)


def test_frame_alignment_offset_uses_reference_video_frames() -> None:
    reference_timestamps = np.arange(20, dtype=np.int64) * 100
    alignment = FrameAlignment(ratio=8.0, video_frame_offset=5)

    aligned = synthesize_frame_aligned_timestamps(
        frame_count=49,
        reference_video_timestamps=reference_timestamps,
        ratio=8.0,
        frame_offset=alignment.gt_frame_offset,
    )

    assert alignment.gt_frame_offset == 40
    assert aligned[40] == reference_timestamps[0]
    assert aligned[48] == reference_timestamps[1]


def test_frame_alignment_accepts_negative_video_frame_offset() -> None:
    reference_timestamps = np.arange(20, dtype=np.int64) * 100
    alignment = FrameAlignment(ratio=8.0, video_frame_offset=-5)

    aligned = synthesize_frame_aligned_timestamps(
        frame_count=9,
        reference_video_timestamps=reference_timestamps,
        ratio=8.0,
        frame_offset=alignment.gt_frame_offset,
    )

    assert alignment.gt_frame_offset == -40
    assert aligned[0] == reference_timestamps[5]
    assert aligned[8] == reference_timestamps[6]
    summary = exporter.describe_frame_alignment(8.0, -5)
    assert "Robocap offset=-5 frames -> GT offset=-40 frames" in summary
    assert "NOKOV/GT is delayed (shifted later) relative to Robocap video" in summary
    assert "video frame 0 -> GT frame -40" in summary
    assert "GT frame 0 -> video frame 5.000000000" in summary


def test_capture_timestamps_map_to_gt_scale_frame_timeline() -> None:
    reference_timestamps = np.asarray([100, 200, 300], dtype=np.int64)
    timestamps = np.asarray([50, 100, 150, 200, 300, 350], dtype=np.int64)

    frames = exporter.capture_timestamps_to_aligned_frames(
        timestamps, reference_timestamps, ratio=8.0
    )

    assert frames.tolist() == [-4, 0, 4, 8, 16, 20]


def test_frame_timeline_aligns_video_and_gt_source_frames() -> None:
    reference_timestamps = np.arange(200, dtype=np.int64) * 100
    positive = exporter.TimelineContext(
        alignment_mode="frame",
        reference_timestamps_ns=reference_timestamps,
        frame_alignment=FrameAlignment(ratio=8.0, video_frame_offset=5),
    )
    negative = exporter.TimelineContext(
        alignment_mode="frame",
        reference_timestamps_ns=reference_timestamps,
        frame_alignment=FrameAlignment(ratio=8.0, video_frame_offset=-5),
    )

    video_frame = int(positive.frames_from_capture_time(reference_timestamps[[100]])[0])

    assert positive.primary_timeline == "frame"
    assert video_frame == 800
    assert positive.gt_frame(840) == video_frame
    assert negative.gt_frame(760) == video_frame


def test_third_person_video_start_follows_frame_offset() -> None:
    reference_timestamps = np.arange(20, dtype=np.int64) * 100
    marker_track = exporter.GTMarkerTrack(
        label="hand",
        entity="gt/tracks/trc/hand",
        source="trc",
        timestamps_ns=np.arange(81, dtype=np.int64) * 12_500_000,
        positions=np.zeros((81, 1, 3), dtype=np.float32),
        marker_names=("root",),
    )
    gt_config = exporter.GTConfig(
        skeleton=None,
        mesh=None,
        marker_tracks=(marker_track,),
        mano_mesh_tracks=(),
        third_person_video=Path("third-person.mp4"),
    )

    positive = exporter.with_frame_aligned_gt_timestamps(
        gt_config,
        reference_timestamps,
        video_rate_hz=30.0,
        frame_ratio=8.0,
        video_frame_offset=5,
    )
    negative = exporter.with_frame_aligned_gt_timestamps(
        gt_config,
        reference_timestamps,
        video_rate_hz=30.0,
        frame_ratio=8.0,
        video_frame_offset=-5,
    )

    assert exporter.reference_timestamp_at_video_frame(-5.0, reference_timestamps) == -500
    assert exporter.reference_timestamp_at_video_frame(5.0, reference_timestamps) == 500
    assert positive is not None
    assert negative is not None
    assert positive.third_person_start_ns == -500
    assert negative.third_person_start_ns == 500


def test_exporter_rounds_auto_inferred_ratio_but_preserves_explicit_ratio() -> None:
    marker_track = exporter.GTMarkerTrack(
        label="hand",
        entity="gt/tracks/trc/hand",
        source="trc",
        timestamps_ns=np.arange(81, dtype=np.int64) * 12_500_000,
        positions=np.zeros((81, 1, 3), dtype=np.float32),
        marker_names=("root",),
    )
    gt_config = exporter.GTConfig(
        skeleton=None,
        mesh=None,
        marker_tracks=(marker_track,),
        mano_mesh_tracks=(),
        third_person_video=None,
    )

    assert exporter.resolve_gt_frame_ratio(gt_config, video_rate_hz=30.0, frame_ratio=None) == 3.0
    assert exporter.resolve_gt_frame_ratio(gt_config, video_rate_hz=30.0, frame_ratio=2.5) == 2.5


def test_robocap_frame_range_is_zero_based_inclusive() -> None:
    timestamps_ns = np.asarray([100, 200, 350, 500, 800], dtype=np.int64)

    frame_range = exporter.normalize_robocap_frame_range(1, 3)
    window = exporter.robocap_frame_capture_window(timestamps_ns, frame_range)

    assert frame_range == (1, 3)
    assert window == exporter.TimeWindow(start_ns=200, end_ns=500)
    assert exporter.time_mask(timestamps_ns, window).tolist() == [False, True, True, True, False]


def test_robocap_frame_range_requires_valid_pair() -> None:
    for start_frame, end_frame in ((1, None), (None, 1), (-1, 2), (3, 2)):
        try:
            exporter.normalize_robocap_frame_range(start_frame, end_frame)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected invalid frame range: {start_frame}, {end_frame}")

    try:
        exporter.robocap_frame_capture_window(np.asarray([100, 200]), (0, 2))
    except ValueError as exc:
        assert "0..1" in str(exc)
    else:
        raise AssertionError("Expected out-of-bounds frame range to fail")


def test_requested_frame_window_intersects_common_capture_window() -> None:
    requested = exporter.TimeWindow(start_ns=100, end_ns=500)
    common = exporter.TimeWindow(start_ns=200, end_ns=700)

    assert exporter.intersect_time_windows(requested, common) == exporter.TimeWindow(200, 500)
    assert exporter.intersect_time_windows(requested, exporter.TimeWindow(501, 700)) is None


def sample_export_name_parameters() -> exporter.ExportNameParameters:
    return exporter.ExportNameParameters(
        alignment_mode="frame",
        frame_ratio=8.0,
        video_frame_offset=5,
        reference_video="left",
        frame_range=(100, 200),
        retarget_model="none",
        use_proxy=True,
        proxy_height=540,
        proxy_crf=28,
        proxy_bitrate="1400k",
        ffmpeg="auto",
        max_sensor_points=6000,
        trim_to_common_time=True,
        align_gt_to_robocap=True,
        gt_coordinate_scale=0.001,
        bvh_coordinate_scale=0.01,
        gt_time_offset_ns=0,
        gt_max_frames=None,
        interpolate_dropped_frames=True,
        include_robowrist=True,
        include_mag=False,
        include_imu=True,
        gt_dir="mocap",
        gt_input_files=("left.trc", "tracker.xrs"),
        gt_skeleton=None,
        gt_mesh=None,
        third_person_input="third-person.mp4",
        mano_model_dir="mano",
        gt_sources=("trc:left", "xrs:tracker"),
        third_person_video=True,
    )


def test_rrd_name_contains_readable_parameters_and_stable_fingerprint() -> None:
    path = Path("session_segment_frame_aligned.rrd")
    parameters = sample_export_name_parameters()

    named = exporter.with_export_parameter_suffix(path, parameters)

    assert "_r8_o5_ref-left_f100-200_interp1_rt-none_p540_" in named.name
    assert "_data-rw1-mag0-imu1-tp1_cfg-" in named.name
    assert exporter.with_export_parameter_suffix(named, parameters) == named


def test_rrd_name_fingerprint_changes_with_non_readable_export_parameter() -> None:
    path = Path("session_segment_frame_aligned.rrd")
    parameters = sample_export_name_parameters()

    original = exporter.with_export_parameter_suffix(path, parameters)
    changed = exporter.with_export_parameter_suffix(
        path, replace(parameters, gt_coordinate_scale=0.01)
    )

    assert original != changed


def test_default_rrd_names_do_not_include_legacy_dual_hands_gt_suffix(tmp_path: Path) -> None:
    session_dir = tmp_path / "session39"
    config = exporter.SessionConfig(segment_name="segment1", videos={}, signals={}, notes={})

    artifacts = exporter.build_artifact_paths(session_dir, config)

    assert artifacts.rrd_path.name == "session39_segment1.rrd"
    assert (
        exporter.default_rrd_path(artifacts, session_dir, config, "frame").name
        == "session39_segment1_frame_aligned.rrd"
    )


def test_gt_visual_style_uses_only_point_and_line_colors_for_all_formats() -> None:
    for suffix in (".bvh", ".trc", ".csv", ".xrs"):
        for stem in ("Body0_Left", "Body0_Right", "Tracker0"):
            style = exporter.gt_file_visual_style(Path(f"{stem}{suffix}"))
            assert style.point_color == exporter.GT_POINT_COLOR
            assert style.line_color == exporter.GT_LINE_COLOR

    assert exporter.GTMarkerTrack.__dataclass_fields__["point_color"].default == (
        exporter.GT_POINT_COLOR
    )
    assert exporter.GTMarkerTrack.__dataclass_fields__["line_color"].default == (
        exporter.GT_LINE_COLOR
    )
    assert {exporter.GT_POINT_COLOR, exporter.GT_LINE_COLOR} == {
        (24, 72, 255),
        (255, 45, 45),
    }


def test_gt_logging_writes_blue_points_and_red_lines(monkeypatch) -> None:
    point_calls = []
    line_calls = []

    class FakeTimeline:
        alignment_mode = "time"

        def set_time(self, timestamp_ns, frame_index=None):
            return None

    monkeypatch.setattr(
        exporter.rr,
        "Points3D",
        lambda *args, **kwargs: point_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(
        exporter.rr,
        "LineStrips3D",
        lambda *args, **kwargs: line_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(exporter.rr, "log", lambda *args, **kwargs: None)

    track = exporter.GTMarkerTrack(
        label="body",
        entity="gt/tracks/trc/body",
        source="trc",
        timestamps_ns=np.asarray([0], dtype=np.int64),
        positions=np.asarray([[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]], dtype=np.float32),
        marker_names=("root", "tip"),
        connections=((0, 1),),
    )

    exporter.log_gt_marker_track(track, None, FakeTimeline())

    assert point_calls == [{"radii": track.radius, "colors": list(exporter.GT_POINT_COLOR)}]
    assert line_calls == [{"radii": track.radius * 0.45, "colors": list(exporter.GT_LINE_COLOR)}]


def test_interpolated_marker_frame_turns_red_adds_label_then_clears(monkeypatch) -> None:
    point_calls = []
    line_calls = []
    clear_calls = []
    logged_entities = []

    class FakeTimeline:
        alignment_mode = "frame"

        def gt_frame(self, source_frame):
            return source_frame + 100

        def set_time(self, timestamp_ns, frame_index=None):
            return None

    monkeypatch.setattr(
        exporter.rr,
        "Points3D",
        lambda *args, **kwargs: point_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(
        exporter.rr,
        "LineStrips3D",
        lambda *args, **kwargs: line_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(
        exporter.rr,
        "Clear",
        lambda *args, **kwargs: clear_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(
        exporter.rr,
        "log",
        lambda entity, *args, **kwargs: logged_entities.append(entity),
    )

    track = exporter.GTMarkerTrack(
        label="body",
        entity="gt/tracks/trc/body",
        source="trc",
        timestamps_ns=np.asarray([0, 4_000_000, 8_000_000], dtype=np.int64),
        positions=np.asarray(
            [
                [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
                [[0.0, 1.0, 0.0], [1.0, 1.0, 0.0]],
                [[0.0, 2.0, 0.0], [1.0, 2.0, 0.0]],
            ],
            dtype=np.float32,
        ),
        marker_names=("root", "tip"),
        connections=((0, 1),),
        interpolated_mask=np.asarray([False, True, False]),
    )

    exporter.log_gt_marker_track(track, None, FakeTimeline())

    assert [call["colors"] for call in point_calls] == [
        list(exporter.GT_POINT_COLOR),
        list(exporter.GT_INTERPOLATED_COLOR),
        list(exporter.GT_INTERPOLATED_COLOR),
        list(exporter.GT_POINT_COLOR),
    ]
    assert [call["colors"] for call in line_calls] == [
        list(exporter.GT_LINE_COLOR),
        list(exporter.GT_INTERPOLATED_COLOR),
        list(exporter.GT_LINE_COLOR),
    ]
    assert point_calls[2]["show_labels"] is True
    assert point_calls[2]["labels"] == [
        "INTERPOLATED TRC source frame 1 (0-based) / timeline frame 101"
    ]
    assert clear_calls == [{"recursive": True}]
    assert logged_entities.count("gt/tracks/trc/body/interpolated_frame") == 2


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

    class FakeTimeline:
        alignment_mode = "time"

        def set_time(self, timestamp_ns, frame_index=None):
            capture_times.append(timestamp_ns)

    monkeypatch.setattr(
        exporter.rr,
        "log",
        lambda entity, archetype, **kwargs: logged.append((entity, archetype, kwargs)),
    )

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

    timeline = FakeTimeline()
    exporter.log_gt_marker_track(marker_track, None, timeline)
    exporter.log_gt_mano_mesh(mesh_track, None, timeline)

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


def test_gt_skeleton_formats_are_arranged_left_to_right() -> None:
    tracks = tuple(
        exporter.GTMarkerTrack(
            label=f"hand_{source}",
            entity=f"gt/tracks/{source}/hand",
            source=source,
            timestamps_ns=np.asarray([0], dtype=np.int64),
            positions=np.zeros((1, 1, 3), dtype=np.float32),
            marker_names=("root",),
        )
        for source in ("bvh", "trc", "csv", "xrs")
    )
    gt_config = exporter.GTConfig(
        skeleton=None,
        mesh=None,
        marker_tracks=tracks,
        mano_mesh_tracks=(),
        third_person_video=None,
    )

    container = exporter.gt_skeleton_row(gt_config)

    assert container is not None
    assert container.name == "GT skeleton"
    assert [view.name for view in container.contents] == ["BVH", "TRC", "CSV", "XRS"]
    assert container.column_shares == [1.0, 1.0, 1.0, 1.0]


def test_interpolate_dropped_samples_preserves_4_5ms_jitter_and_fills_8ms_gap() -> None:
    timestamps_ns = np.asarray([0, 4_000_000, 12_000_000, 17_000_000], dtype=np.int64)
    positions = np.asarray([0.0, 4.0, 12.0, 17.0], dtype=np.float32)[:, None, None]

    timestamps, values, inserted, skipped = exporter.interpolate_dropped_samples(
        timestamps_ns, positions
    )

    assert timestamps.tolist() == [0, 4_000_000, 8_000_000, 12_000_000, 17_000_000]
    assert values[:, 0, 0].tolist() == [0.0, 4.0, 8.0, 12.0, 17.0]
    assert inserted == 1
    assert skipped == 0
    masked_result = exporter.interpolate_dropped_samples_with_mask(timestamps_ns, positions)
    assert masked_result.interpolated_mask.tolist() == [False, False, True, False, False]


def test_interpolation_does_not_bridge_long_capture_discontinuity() -> None:
    timestamps_ns = np.asarray([0, 2_000_000_000], dtype=np.int64)
    positions = np.asarray([0.0, 1.0], dtype=np.float32)[:, None, None]

    timestamps, values, inserted, skipped = exporter.interpolate_dropped_samples(
        timestamps_ns, positions
    )

    assert timestamps.tolist() == timestamps_ns.tolist()
    assert values.tolist() == positions.tolist()
    assert inserted == 0
    assert skipped == 1


def test_gt_interpolation_updates_marker_and_mano_tracks_before_alignment() -> None:
    timestamps_ns = np.asarray([0, 8_000_000], dtype=np.int64)
    marker_track = exporter.GTMarkerTrack(
        label="left_hand",
        entity="gt/tracks/trc/left_hand",
        source="trc",
        timestamps_ns=timestamps_ns,
        positions=np.asarray([[[0.0, 0.0, 0.0]], [[2.0, 0.0, 0.0]]], dtype=np.float32),
        marker_names=("root",),
    )
    mano_track = exporter.GTManoMeshTrack(
        label="left_hand",
        entity="gt/mesh/trc/left_hand",
        source="trc",
        timestamps_ns=timestamps_ns,
        vertices=np.asarray([[[0.0, 0.0, 0.0]], [[2.0, 0.0, 0.0]]], dtype=np.float32),
        faces=np.empty((0, 3), dtype=np.uint32),
    )
    config = exporter.GTConfig(
        skeleton=None,
        mesh=None,
        marker_tracks=(marker_track,),
        mano_mesh_tracks=(mano_track,),
        third_person_video=None,
    )

    interpolated = exporter.interpolate_gt_dropped_frames(config)

    assert interpolated is not None
    assert interpolated.marker_tracks[0].timestamps_ns.tolist() == [0, 4_000_000, 8_000_000]
    assert interpolated.mano_mesh_tracks[0].timestamps_ns.tolist() == [0, 4_000_000, 8_000_000]
    assert interpolated.marker_tracks[0].interpolated_mask.tolist() == [False, True, False]
    assert interpolated.mano_mesh_tracks[0].interpolated_mask.tolist() == [False, True, False]
    assert "trc:left_hand +1" in (interpolated.note or "")


def test_sibling_trc_clock_replaces_invalid_csv_timestamps_before_interpolation() -> None:
    reference_timestamps_ns = np.asarray([0, 4_000_000, 12_000_000, 17_000_000], dtype=np.int64)
    csv_track = exporter.GTMarkerTrack(
        label="hand",
        entity="gt/tracks/csv/hand",
        source="csv",
        timestamps_ns=np.zeros(4, dtype=np.int64),
        positions=np.asarray([0.0, 4.0, 12.0, 17.0], dtype=np.float32)[:, None, None],
        marker_names=("root",),
    )

    synchronized = exporter.synchronize_marker_track_timestamps(
        csv_track, reference_timestamps_ns, None
    )
    timestamps, positions, inserted, skipped = exporter.interpolate_dropped_samples(
        synchronized.timestamps_ns, synchronized.positions
    )

    assert timestamps.tolist() == [0, 4_000_000, 8_000_000, 12_000_000, 17_000_000]
    assert positions[:, 0, 0].tolist() == [0.0, 4.0, 8.0, 12.0, 17.0]
    assert inserted == 1
    assert skipped == 0


def test_sibling_trc_clock_uses_matching_downsample_indexes() -> None:
    reference_timestamps_ns = np.asarray([0, 4, 8, 12], dtype=np.int64)
    track = exporter.GTMarkerTrack(
        label="hand",
        entity="gt/tracks/csv/hand",
        source="csv",
        timestamps_ns=np.asarray([0, 0], dtype=np.int64),
        positions=np.zeros((2, 1, 3), dtype=np.float32),
        marker_names=("root",),
    )

    synchronized = exporter.synchronize_marker_track_timestamps(track, reference_timestamps_ns, 2)

    assert synchronized.timestamps_ns.tolist() == [0, 12]


def test_unselected_sibling_trc_remains_available_as_csv_clock(tmp_path: Path) -> None:
    csv_path = tmp_path / "Hand.csv"
    trc_path = tmp_path / "Hand.TRC"
    csv_path.write_text("", encoding="utf-8")
    trc_path.write_text("", encoding="utf-8")
    file_set = exporter.gt_file_sets_from_paths(tmp_path, [csv_path])[0]

    assert file_set.trc is None
    assert exporter.gt_file_set_trc_clock_path(file_set) == trc_path


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


def test_discover_gt_dir_accepts_mocap_prefix_folder_and_ignores_robowrist(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    mocap_dir = session_dir / "Mocap-NOKOV"
    robowrist_dir = session_dir / "robowrist_device_left"
    mocap_dir.mkdir(parents=True)
    robowrist_dir.mkdir()
    (mocap_dir / "Tracker4.trc").write_text("", encoding="utf-8")
    (robowrist_dir / "sensor.csv").write_text("", encoding="utf-8")

    assert discover_gt_dir(session_dir, None) == mocap_dir


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


def test_discover_session_finds_nested_wrist_mag_and_reads_expected_axes(tmp_path: Path) -> None:
    (tmp_path / "robocap_segment1_video_left.mp4").write_bytes(b"")
    nested = tmp_path / "capture" / "devices" / "left-unit"
    nested.mkdir(parents=True)
    db_path = nested / "robowrist_segment1_mag_left.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE mag_data (timestamp INTEGER, mag_x REAL, mag_y REAL, mag_z REAL)"
        )
        connection.execute("INSERT INTO mag_data VALUES (100, 1.0, 2.0, 3.0)")

    config = discover_session(tmp_path, "segment1")
    spec = config.signals["left_wrist_mag"]
    timestamps_ns, axes = exporter.fetch_signal_rows(db_path, spec.table, spec.columns, 10)

    assert spec.relative_path == "capture/devices/left-unit/robowrist_segment1_mag_left.db"
    assert timestamps_ns.tolist() == [100]
    assert {axis: values.tolist() for axis, values in axes.items()} == {
        "mag_x": [1.0],
        "mag_y": [2.0],
        "mag_z": [3.0],
    }


def test_discover_session_can_exclude_all_robowrist_streams(tmp_path: Path) -> None:
    (tmp_path / "robocap_segment1_video_left.mp4").write_bytes(b"")
    left_dir = tmp_path / "robowrist_device_left"
    right_dir = tmp_path / "robowrist_device_right"
    left_dir.mkdir()
    right_dir.mkdir()
    for directory, side in ((left_dir, "left"), (right_dir, "right")):
        (directory / f"robowrist_segment1_video_{side}_down.mp4").write_bytes(b"")
        (directory / f"robowrist_segment1_mag_{side}.db").write_bytes(b"")
        (directory / f"robowrist_segment1_imu_{side}.db").write_bytes(b"")

    included = discover_session(tmp_path, "segment1", include_robowrist=True)
    excluded = discover_session(tmp_path, "segment1", include_robowrist=False)

    assert robowrist_stream_labels(included) == (
        "left_wrist_down",
        "right_wrist_down",
        "left_wrist_mag",
        "left_wrist_acc",
        "left_wrist_gyro",
        "right_wrist_mag",
        "right_wrist_acc",
        "right_wrist_gyro",
    )
    assert robowrist_stream_labels(excluded) == ()


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

    assert container.name == "Robocap sensors"
    assert container.column_shares == [1.0, 2.0]
    assert mag_view.name == "middle_mag"
    assert imu_column.name == "Robocap IMU rows"
    assert [row.name for row in imu_column.contents] == [
        "Left robocap IMU",
        "Right robocap IMU",
    ]
    assert [[view.name for view in row.contents] for row in imu_column.contents] == [
        ["left_robocap_acc", "left_robocap_gyro"],
        ["right_robocap_acc", "right_robocap_gyro"],
    ]


def test_robocap_sensor_layout_is_omitted_without_mag_or_imu(tmp_path: Path) -> None:
    (tmp_path / "robocap_segment1_video_left.mp4").write_bytes(b"")
    config = discover_session(tmp_path, "segment1")

    assert robocap_sensors_container(config) is None
    assert sensors_container(config) is None


def _write_sensor_session_files(tmp_path: Path) -> None:
    for name in (
        "robocap_segment1_video_left.mp4",
        "robocap_segment1_video_left_eye.mp4",
        "robocap_segment1_video_left_front.mp4",
        "robocap_segment1_video_right.mp4",
        "robocap_segment1_video_right_eye.mp4",
        "robocap_segment1_video_right_front.mp4",
        "robocap_segment1_mag_middle.db",
        "robocap_segment1_imu_left.db",
        "robocap_segment1_imu_right.db",
    ):
        (tmp_path / name).write_bytes(b"")
    for side in ("left", "right"):
        wrist_dir = tmp_path / f"robowrist_device_{side}"
        wrist_dir.mkdir()
        (wrist_dir / f"robowrist_segment1_video_{side}_down.mp4").write_bytes(b"")
        (wrist_dir / f"robowrist_segment1_mag_{side}.db").write_bytes(b"")
        (wrist_dir / f"robowrist_segment1_imu_{side}.db").write_bytes(b"")


def test_sensor_layout_renders_robowrist_only_when_enabled(tmp_path: Path) -> None:
    _write_sensor_session_files(tmp_path)

    included = discover_session(tmp_path, "segment1", include_robowrist=True)
    sensors = sensors_container(included)
    assert [row.name for row in sensors.contents] == [
        "Robocap sensors",
        "Left wrist sensors",
        "Right wrist sensors",
    ]
    assert sensors.grid_columns == 1
    assert sensors.row_shares == [1.6, 1.0, 1.0]
    assert [[view.name for view in row.contents] for row in sensors.contents[1:]] == [
        ["left_wrist_mag", "left_wrist_acc", "left_wrist_gyro"],
        ["right_wrist_mag", "right_wrist_acc", "right_wrist_gyro"],
    ]

    excluded = discover_session(tmp_path, "segment1", include_robowrist=False)
    sensors = sensors_container(excluded)
    assert [row.name for row in sensors.contents] == ["Robocap sensors"]
    assert sensors.grid_columns == 1
    assert sensors.row_shares == [1.6]


def test_blueprint_uses_sensor_layout(tmp_path: Path, monkeypatch) -> None:
    _write_sensor_session_files(tmp_path)
    config = discover_session(tmp_path, "segment1")
    calls = []
    original = exporter.sensors_container

    def tracked_sensors_container(session_config):
        calls.append(session_config)
        return original(session_config)

    monkeypatch.setattr(exporter, "sensors_container", tracked_sensors_container)

    exporter.build_blueprint(config)

    assert calls == [config]
