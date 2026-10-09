import csv
import hashlib
import json
import shutil

import pytest

from robocap_rerun_tools import obj_inventory as inventory


def pose_text(names=("Segment1",)):
    return (
        "[Head]\nNumFrames,NumSegments,DataFrameRate\n"
        f"120,{len(names)},120\n\n[SegmentNames&Hierarchy]\nSegment,Parent\n"
        + "\n".join(f"{name}," for name in names)
        + "\n\n[SegmentData]\n,Timestamp,XToGlobal1,YToGlobal1,ZToGlobal1\n"
        "1,1000,1,2,3\n"
    )


def capture_dir(root, name="mocap-LM01-S02-wang-yang-01p11"):
    path = root / "20261008_010203_session7" / name
    path.mkdir(parents=True)
    return path


def test_suffix_keeps_instance_and_hyphenated_name_and_participant(tmp_path):
    path = tmp_path / "mocap-LM01-S02-wang-yang-01p11-Open box-left.csv"
    suffix, capture = inventory.filename_parts(path)
    assert suffix == "Open box-left"
    assert capture.participant == "wang-yang"
    assert capture.repetition_count == 1
    assert capture.collection_session_index == 2
    assert capture.source_directory.endswith("01p11")


def test_legacy_directory_prefix_does_not_make_generic_tracks_into_props(tmp_path):
    directory = capture_dir(tmp_path, "mocap-P01-St-wangyang1")
    expected = {
        "Body0": "body",
        "Tracker0": "tracker",
        "Unnamed": "unnamed",
        "Open box-left": "object_candidate",
    }
    for label, category in expected.items():
        path = directory / f"{directory.name}-{label}.csv"
        path.write_text(pose_text(), encoding="utf-8")
        row = inventory.describe_file(path, tmp_path, 65536)
        assert row["object_name"] == label and row["category"] == category
        assert "unparsed_capture_identity" in row["issues"]
    generic = tmp_path / "mocap" / "old-export-Tracker-start.csv"
    assert inventory.filename_parts(generic) == ("Tracker-start", None)
    generic = tmp_path / "mocap" / "mocap-P01-St-user-Tracker0.csv"
    assert inventory.filename_parts(generic) == ("Tracker0", None)
    directory = tmp_path / "mocap-LM01-S01-person-01p1"
    tricky_suffix = directory / f"{directory.name}-Tool-02p1-left.csv"
    suffix, capture = inventory.filename_parts(tricky_suffix)
    assert suffix == "Tool-02p1-left" and capture.participant == "person"


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("Body2", "body"),
        ("Body0_Left", "body"),
        ("TRACKER0", "tracker"),
        ("Tracker-start", "tracker"),
        ("Tracker-end", "tracker"),
        ("Unnamed", "unnamed"),
        ("unnamed_5", "unnamed"),
        ("BigBox0", "object_candidate"),
        ("PlatformTrolly", "object_candidate"),
        ("FutureUnknownProp", "object_candidate"),
    ],
)
def test_generic_tracks_and_open_ended_candidates(name, category):
    assert inventory.name_category(name) == category


def test_renamed_skeleton_is_not_counted_as_object(tmp_path):
    directory = capture_dir(tmp_path)
    path = directory / "Chair.csv"
    path.write_text(pose_text(("Hips", "Spine", "Head", "LeftHand")), encoding="utf-8")
    row = inventory.describe_file(path, tmp_path, 65536)
    assert row["category"] == "body"
    assert row["content_type"] == "human_skeleton"
    assert "name_contains_human_skeleton" in row["issues"]


def test_metadata_conflict_is_exposed_without_guessing(tmp_path):
    directory = capture_dir(tmp_path)
    path = directory / "mocap-EC02-S01-another-02p1-Ramp.csv"
    path.write_text(pose_text(), encoding="utf-8")
    row = inventory.describe_file(path, tmp_path, 65536)
    assert "filename_directory_identity_conflict" in row["issues"]
    assert row["action_id"] == "EC02"


def test_header_formats_empty_pointer_and_non_utf8(tmp_path):
    path = tmp_path / "box.csv"
    path.write_text(pose_text(("箱子",)), encoding="gb18030")
    header = inventory.inspect_header(path)
    assert header["header_status"] == "header_ok"
    assert header["header_names"] == ["箱子"]
    assert header["encoding"] == "gb18030"
    assert header["declared_frames"] == 120 and header["declared_rate_hz"] == 120
    path.write_bytes(b"")
    assert inventory.inspect_header(path)["header_status"] == "empty"
    path.write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 9999\n",
        encoding="utf-8",
    )
    assert inventory.inspect_header(path)["header_status"] == "git_lfs_pointer"
    trc = tmp_path / "box.trc"
    trc.write_text(
        "PathFileType\t4\t(X/Y/Z)\tbox.trc\nDataRate\tCameraRate\tNumFrames\tNumMarkers\n"
        "120\t120\t10\t1\nFrame#\tTime\tTimestamp\tBoxMarker\t\t\n",
        encoding="utf-8-sig",
    )
    assert inventory.inspect_header(trc)["header_names"] == ["BoxMarker"]
    bvh = tmp_path / "box.bvh"
    bvh.write_text(
        "HIERARCHY\nROOT Box\n{\n}\nMOTION\nFrames: 10\nFrame Time: 0.01\n",
        encoding="utf-8",
    )
    assert inventory.inspect_header(bvh)["declared_rate_hz"] == 100
    xrs = tmp_path / "box.xrs"
    xrs.write_text(
        "[Head]\nNumFrames NumSegments DataFrameRate\n10 1 120\n"
        "[SegmentNames&Hierarchy]\nSegment\t\tParent\nBox\t\t\n[SegmentData]\n",
        encoding="utf-8",
    )
    assert inventory.inspect_header(xrs)["header_names"] == ["Box"]


def test_header_limit_is_not_reported_as_valid(tmp_path):
    path = tmp_path / "box.csv"
    path.write_text("#" * 5000 + "\n" + pose_text(), encoding="utf-8")
    assert inventory.inspect_header(path, 1024)["header_status"] == "header_limit_reached"


def test_bounded_read_does_not_corrupt_trailing_utf8_character(tmp_path):
    path = tmp_path / "box.csv"
    content = pose_text() + "中" * 2000
    path.write_text(content, encoding="utf-8")
    header = inventory.inspect_header(path, 1024)
    assert header["header_status"] == "header_ok"
    assert header["encoding"] == "utf-8-sig"


def test_scan_deduplicates_overlapping_roots_and_counts_formats_once(tmp_path):
    root = tmp_path / "original"
    directory = capture_dir(root)
    for name in ("BigBox0.csv", "BigBox0.trc", "Body0.csv", "Tracker0.csv", "Unnamed.trc"):
        (directory / name).write_text(pose_text(), encoding="utf-8")
    skipped = root / "_modelscope_dataset" / directory.name
    skipped.mkdir(parents=True)
    (skipped / "NotOriginal.csv").write_text(pose_text(), encoding="utf-8")
    (root / "accounting.csv").write_text("name,amount\n", encoding="utf-8")
    rows, errors = inventory.scan([root, directory, root], tmp_path / "out", log=lambda _: None)
    assert not errors and len(rows) == 5
    objects, sessions = inventory.summarize(rows)
    assert len(objects) == 1 and objects[0]["object_name"] == "BigBox0"
    assert objects[0]["file_count"] == 2 and objects[0]["recording_count"] == 1
    assert sessions[0]["object_count"] == 1 and sessions[0]["body_files"] == 1
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    rows, _ = inventory.scan([root, copy], tmp_path / "out", log=lambda _: None)
    objects, sessions = inventory.summarize(rows)
    assert objects[0]["file_count"] == 4 and objects[0]["recording_count"] == 1
    assert objects[0]["source_copy_count"] == 2 and len(sessions) == 1


def test_compact_files_outside_mocap_directory_are_discovered(tmp_path):
    raw = tmp_path / "raw_exports"
    raw.mkdir()
    path = raw / "mocap-LM02-S03-person-01p1-BigBox5.CSV"
    path.write_text(pose_text(), encoding="utf-8")
    rows, _ = inventory.scan([raw], tmp_path / "out", log=lambda _: None)
    assert len(rows) == 1 and rows[0]["object_name"] == "BigBox5"
    assert rows[0]["action_id"] == "LM02"


def test_cli_writes_utf8_reports_and_keeps_sources_unchanged(tmp_path):
    directory = capture_dir(tmp_path / "source")
    path = directory / "BigBox0.csv"
    path.write_text(pose_text(), encoding="utf-8")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    output = tmp_path / "report"
    assert inventory.main(["--root", str(directory), "--output", str(output)]) == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not (output / "objects.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    with (output / "objects.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["file_count"] == "1" and rows[0]["object_name"] == "BigBox0"
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["scan_complete"] and summary["object_names"] == 1
    assert (output / "report.html").exists() and (output / "run.log").exists()
    with pytest.raises(SystemExit) as exc:
        inventory.main(["--root", str(directory), "--output", str(output)])
    assert exc.value.code == 2


def test_missing_root_is_incomplete_and_not_zero_success(tmp_path):
    output = tmp_path / "report"
    assert inventory.main(["--root", str(tmp_path / "missing"), "--output", str(output)]) == 1
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert not summary["scan_complete"] and summary["scan_errors"] == 1


def test_report_escapes_names_in_html(tmp_path):
    directory = capture_dir(tmp_path / "source")
    path = directory / "box.csv"
    path.write_text(pose_text(), encoding="utf-8")
    row = inventory.describe_file(path, directory, 65536)
    row["object_name"] = "<script>alert(1)</script>"
    output = tmp_path / "report"
    output.mkdir()
    inventory.write_reports(output, [directory], [row], [])
    document = (output / "report.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in document
    assert "<script>alert(1)</script>" not in document
