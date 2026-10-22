import csv
import json
from pathlib import Path, PureWindowsPath

import pytest

from robocap_rerun_tools import mocap_reexport as tool

SESSION = "20261008_054213_session72"
RECORDING = "mocap-LM43-S03-gaoziyue-01p1"


def target():
    return {
        "session": SESSION,
        "session_path": "EgoMotionActions/20261008/LM43/" + SESSION,
        "recording": RECORDING,
        "groups": {"D": "zero bytes"},
        "expected_exports": [],
    }


def exports(folder, *, extra=False):
    folder.mkdir(parents=True, exist_ok=True)
    pose = (
        "NumFrames,NumSegments,DataFrameRate\n2,1,120\n"
        ",Timestamp,XToGlobal1,YToGlobal1,ZToGlobal1,\n1,100,1,2,3,\n2,108,1,2,3,\n"
    )
    trc = (
        "DataRate\tCameraRate\tNumFrames\tNumMarkers\n120\t120\t2\t1\n"
        "Frame#\tTime\tTimestamp\tMarker1\t\t\n\t\t\tX1\tY1\tZ1\n\n"
        "1\t0\t100\t1\t2\t3\n2\t0.008333\t108\t1\t2\t3\n"
    )
    bvh = (
        "HIERARCHY\nROOT Hips\n{\nOFFSET 0 0 0\nCHANNELS 3 Xposition Yposition Zposition\n}\n"
        "MOTION\nFrames: 2\nFrame Time: 0.008333\n1 2 3\n1 2 3\n"
    )
    for label in ("Body0", "Tracker0", *(("Box1", "Body1") if extra else ())):
        formats = [("csv", pose), ("trc", trc)]
        if label.startswith("Body"):
            formats.append(("bvh", bvh))
        for ext, text in formats:
            (folder / f"{RECORDING}-{label}.{ext}").write_text(text, encoding="utf-8")
    return folder


def plan_for(tmp_path, *, drive="D"):
    root = tmp_path / drive
    source = exports(root / "nokov" / "morning" / RECORDING)
    (source.parent / f"{RECORDING}.cap").write_bytes(b"original")
    session = root / "raw" / SESSION
    old = session / RECORDING
    old.mkdir(parents=True)
    (old / "old.txt").write_text("preserve", encoding="utf-8")
    dirs, names, errors = tool.scan_directories([root], lambda _: None)
    assert not errors
    return tool.make_plan([target()], dirs, names)[0], source, session


def test_bundled_mapping_covers_exactly_previous_mocap_issues():
    rows = tool.read_targets()
    assert len(rows) == len({r["session"] for r in rows}) == 74
    assert all(set(r["groups"]) <= set("ABCDEFG") for r in rows)


def test_inventory_windows_paths_dedup_wrappers_and_list_only_affected_batches(tmp_path):
    source = PureWindowsPath("E:/different/nokov/morning") / RECORDING
    session = PureWindowsPath("E:/raw") / SESSION
    csv_path = tmp_path / "inventory.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["生成由 WizTree"])
        writer.writerow(["文件名称", "大小"])
        for path in (
            source,
            source / RECORDING,
            session,
            session / RECORDING,
            session / RECORDING / RECORDING,
            source.parent / "mocap-unrelated",
        ):
            writer.writerow([str(path) + "\\", 0])
        writer.writerow([str(source) + ".cap", 123])
    dirs, names, errors = tool.inventory_directories(csv_path)
    row = tool.make_plan([target()], dirs, names)[0]
    assert not errors
    assert row["sources"] == [str(source)] and row["destinations"] == [str(session)]
    assert row["batches"] == [str(source.parent)]
    assert row["state"] == "匹配唯一"
    output = tmp_path / "report"
    output.mkdir()
    assert tool.write_report(output, [row], [], inventory=csv_path) == 1
    assert (output / "batch_paths.txt").read_text(encoding="utf-8").strip() == str(source.parent)
    page = (output / "report.html").read_text(encoding="utf-8")
    assert "复制全部批次路径" in page and "WizTree 目录快照" in page


def test_duplicate_source_or_session_is_never_silently_selected(tmp_path):
    _row, source, session = plan_for(tmp_path)
    copy = tmp_path / "E" / "nokov" / "other" / RECORDING
    copy.mkdir(parents=True)
    dirs, names, _ = tool.scan_directories([tmp_path], lambda _: None)
    duplicate = tool.make_plan([target()], dirs, names)[0]
    assert len(duplicate["sources"]) == 2 and "重复" in duplicate["state"]
    with pytest.raises(ValueError, match="重复"):
        tool.copy_one(duplicate, tmp_path)
    assert (session / RECORDING / "old.txt").is_file() and source.is_dir()
    # Removing a candidate from the in-memory inventory requires no filesystem mutation.
    dirs.discard(copy)
    other = tmp_path / "backup" / SESSION
    (other / RECORDING).mkdir(parents=True)
    dirs.update((other, other / RECORDING))
    duplicate = tool.make_plan([target()], dirs, names)[0]
    assert "副本" in duplicate["state"]


def test_packaged_cache_is_not_an_original_session_or_source(tmp_path):
    _row, source, session = plan_for(tmp_path)
    cached = tmp_path / ".cache" / "nokov" / "batch" / RECORDING
    cached.mkdir(parents=True)
    packaged = tmp_path / "packaged" / SESSION
    packaged.mkdir(parents=True)
    (packaged / "manifest.json").write_text("{}", encoding="utf-8")
    (packaged / "robocap_segment1_imu_right.db").write_bytes(b"payload")
    dirs, names, _ = tool.scan_directories([tmp_path], lambda _: None)
    found = tool.make_plan([target()], dirs, names)[0]
    assert found["sources"] == [str(source)]
    assert found["destinations"] == [str(session)]


def test_copy_checks_real_exports_keeps_source_and_old_files_and_flattens(tmp_path):
    row, source, session = plan_for(tmp_path)
    exports(source, extra=True)
    # Duplicated wrappers with identical content must not create another nesting level.
    exports(source / RECORDING)
    source_hashes = {
        str(p.relative_to(source)): tool.sha256(p) for p in source.rglob("*") if p.is_file()
    }
    outcome = tool.copy_one(row, tmp_path)
    assert outcome["state"] == "已复制并校验" and outcome["files"] == 10
    journal = json.loads(Path(outcome["journal"]).read_text(encoding="utf-8"))
    assert Path(journal["backups"][0]["backup"]).joinpath("old.txt").read_text() == "preserve"
    assert not (session / RECORDING / RECORDING).exists()
    assert (session / RECORDING / f"{RECORDING}-Box1.trc").is_file()
    assert source_hashes == {
        str(p.relative_to(source)): tool.sha256(p) for p in source.rglob("*") if p.is_file()
    }
    dirs, names, _ = tool.scan_directories([tmp_path], lambda _: None)
    assert tool.make_plan([target()], dirs, names)[0]["state"] == "匹配唯一"


@pytest.mark.parametrize("failure", ["missing", "zero", "bad", "timestamps", "lfs", "prop"])
def test_incomplete_or_invalid_reexport_leaves_target_untouched(tmp_path, failure):
    row, source, session = plan_for(tmp_path)
    if failure == "missing":
        row["expected_exports"] = [RECORDING + "-Box9.csv"]
    elif failure == "prop":
        (source / f"{RECORDING}-Box9.csv").write_text("payload", encoding="utf-8")
    else:
        file = source / f"{RECORDING}-Body0.csv"
        payload = {
            "zero": "",
            "bad": "bad",
            "lfs": "version https://git-lfs.github.com/spec/v1\n",
            "timestamps": file.read_text().replace(",100,", ",0,").replace(",108,", ",0,"),
        }
        file.write_text(payload[failure], encoding="utf-8")
    with pytest.raises(ValueError):
        tool.copy_one(row, tmp_path)
    assert (session / RECORDING / "old.txt").read_text() == "preserve"
    assert not (session.parent / "_artifacts").exists()


def test_conflicting_nested_exports_are_blocked(tmp_path):
    row, source, session = plan_for(tmp_path)
    nested = source / RECORDING
    nested.mkdir()
    (nested / f"{RECORDING}-Body0.csv").write_text("different", encoding="utf-8")
    with pytest.raises(ValueError, match="冲突"):
        tool.copy_one(row, tmp_path)
    assert (session / RECORDING / "old.txt").is_file()


def test_install_failure_restores_old_directory_and_preserves_staged_data(tmp_path, monkeypatch):
    row, source, session = plan_for(tmp_path)
    original = Path.rename

    def fail_install(path, destination):
        if path.parent.name == "new":
            raise OSError("simulated interrupted install")
        return original(path, destination)

    monkeypatch.setattr(Path, "rename", fail_install)
    with pytest.raises(OSError, match="interrupted"):
        tool.copy_one(row, tmp_path)
    assert (session / RECORDING / "old.txt").is_file()
    journals = list(session.parent.glob("_artifacts/mocap_reexport_backups/*/*/journal.json"))
    assert len(journals) == 1
    doc = json.loads(journals[0].read_text(encoding="utf-8"))
    assert doc["state"] == "failed" and Path(doc["stage"]).is_dir()
    assert (source / f"{RECORDING}-Body0.csv").is_file()


def test_snapshot_cannot_be_used_for_copying():
    with pytest.raises(SystemExit) as error:
        tool.main(["copy", "--inventory", "unused.csv"])
    assert error.value.code == 2


def test_original_cap_without_export_still_lists_batch_but_cannot_copy(tmp_path):
    batch = PureWindowsPath("U:/original/capture-batch")
    dirs = {batch}
    names = {batch: {RECORDING.casefold() + ".cap"}}
    row = tool.make_plan([target()], dirs, names)[0]
    assert row["batches"] == [str(batch)]
    assert row["state"] == "找到原始 CAP，尚无导出目录"
    with pytest.raises(ValueError, match="CAP"):
        tool.copy_one(row, tmp_path)
