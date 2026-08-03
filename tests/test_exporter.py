from pathlib import Path

from robocap_rerun_tools.exporter import discover_gt_file_sets, parse_nokov_csv, parse_trc, parse_xrs


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


def test_parse_nokov_csv_expands_segment_pose_axes(tmp_path: Path) -> None:
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
    assert marker_names == ("Segment1", "Segment1_x_axis", "Segment1_y_axis", "Segment1_z_axis")
    assert positions.shape == (1, 4, 3)
    assert parents == (-1, 0, 0, 0)


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
    assert marker_names == (
        "RigidA",
        "RigidA_x_axis",
        "RigidA_y_axis",
        "RigidA_z_axis",
        "RigidB",
        "RigidB_x_axis",
        "RigidB_y_axis",
        "RigidB_z_axis",
    )
    assert positions.shape == (1, 8, 3)
    assert parents == (-1, 0, 0, 0, 0, 4, 4, 4)


def test_discover_gt_file_sets_uses_all_supported_files(tmp_path: Path) -> None:
    for name in ("Tracker0.xrs", "Tracker1.xrs", "LeftHand.bvh", "LeftHand.trc", "extra.csv"):
        (tmp_path / name).write_text("", encoding="utf-8")
    file_sets = discover_gt_file_sets(tmp_path)
    assert [file_set.label for file_set in file_sets] == ["LeftHand", "LeftHand_2", "extra", "Tracker0", "Tracker1"]
    assert sum(1 for file_set in file_sets if file_set.xrs is not None) == 2
