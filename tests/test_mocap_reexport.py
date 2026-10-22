import csv
import json
import sys
import time
from pathlib import Path, PureWindowsPath

import pytest

from robocap_rerun_tools import mocap_reexport as tool
from robocap_rerun_tools.mocap_scan import DirectoryReader, list_directory

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


def test_blocking_directory_times_out_emits_progress_and_worker_restarts():
    command = [
        sys.executable,
        "-u",
        "-c",
        (
            "import sys,json,time\n"
            "for line in sys.stdin:\n"
            " path=json.loads(line)\n"
            " if path=='blocked': time.sleep(10)\n"
            " print(json.dumps({'directories':[], 'files':[path], 'errors':[]}),flush=True)\n"
        ),
    ]
    waiting = []
    with DirectoryReader(timeout=0.8, progress_seconds=0.1, command=command) as reader:
        assert reader.read("before")["files"] == ["before"]
        first_process = reader.process
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="跳过"):
            reader.read("blocked", lambda p, elapsed: waiting.append((p, elapsed)))
        assert time.monotonic() - started < 4
        assert first_process.poll() is not None
        assert waiting and all(p == "blocked" for p, _ in waiting)
        assert reader.read("after")["files"] == ["after"]
        last_process = reader.process
    assert last_process.poll() is not None


def test_bad_directory_does_not_discard_good_siblings_or_next_roots(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "blocked").mkdir()
    exports(first / "nokov" / "morning" / RECORDING)
    original = DirectoryReader.read

    def read(reader, path, waiting):
        if path.name == "blocked":
            raise TimeoutError("目录读取超时")
        return original(reader, path, waiting)

    monkeypatch.setattr(DirectoryReader, "read", read)
    checkpoints, logs = [], []
    dirs, names, errors = tool.scan_directories(
        [first, second], logs.append, checkpoint=lambda d, n, e: checkpoints.append(set(d))
    )
    assert second in dirs and any(p.name == RECORDING for p in dirs)
    assert len(errors) == 1 and errors[0]["path"].endswith("blocked")
    assert len(checkpoints) == 2 and first in checkpoints[0] and second in checkpoints[1]
    assert any("跳过" in message for message in logs)
    assert any("完成目录" in message for message in logs)
    assert tool.make_plan([target()], dirs, names)[0]["sources"]


def test_scan_cancellation_keeps_partial_report_and_never_copies(tmp_path, monkeypatch):
    output = tmp_path / "report"
    monkeypatch.setattr(
        tool,
        "scan_directories",
        lambda *a, **k: (set(), {}, [{"path": "扫描", "error": "用户中止", "cancelled": True}]),
    )

    def unexpected_copy(*_args):
        pytest.fail("Cancellation must not start copying")

    monkeypatch.setattr(tool, "copy_one", unexpected_copy)
    assert tool.main(["copy", "--root", str(tmp_path), "--output", str(output)]) == 1
    assert (output / "report.html").is_file()
    assert not (output / "copy_results.json").exists()
    assert "用户中止" in (output / "scan_errors.csv").read_text(encoding="utf-8")
    assert "未完成或跳过的搜索路径" in (output / "report.html").read_text(encoding="utf-8")


def test_directory_worker_lists_content_and_unavailable_paths(tmp_path):
    (tmp_path / "directory").mkdir()
    (tmp_path / "mixed.CAP").write_bytes(b"cap")
    result = list_directory(tmp_path)
    assert result == {"directories": ["directory"], "files": ["mixed.cap"], "errors": []}
    with DirectoryReader(timeout=5) as reader:
        assert "error" in reader.read(tmp_path / "missing")
        assert reader.read(tmp_path)["files"] == ["mixed.cap"]


def test_bat_launchers_keep_only_ascii_commands():
    root = Path(tool.__file__).resolve().parents[2]
    for name in ("01_find_batches.bat", "02_copy_reexports.bat"):
        data = (root / "scripts" / "mocap_reexport" / name).read_bytes()
        assert data.isascii(), "Chinese prompts belong in Python; CMD misparses LF UTF-8 BATs"


def test_nonpositive_scan_timeout_is_rejected():
    with pytest.raises(SystemExit) as error:
        tool.main(["report", "--scan-timeout", "0"])
    assert error.value.code == 2


def test_default_report_uses_onsite_results_without_scanning_any_disk(tmp_path, monkeypatch):
    def unexpected_scan(*_args, **_kwargs):
        pytest.fail("Default report must not rediscover Sessions or probe onsite disks")

    monkeypatch.setattr(tool, "automatic_roots", unexpected_scan)
    monkeypatch.setattr(tool, "scan_directories", unexpected_scan)
    monkeypatch.setattr(tool, "DirectoryReader", unexpected_scan)
    output = tmp_path / "report"
    assert tool.main(["report", "--output", str(output)]) == 1
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    rows = plan["rows"]
    assert len(rows) == 74
    assert "20261022_213918_248704" in plan["basis"]
    assert sum(bool(r["sources"]) for r in rows) == 25
    assert sum(bool(r["destinations"]) for r in rows) == 20
    assert sum(r["state"] == "匹配唯一" for r in rows) == 18
    assert len({p for r in rows for p in r["batches"]}) == 4
    assert sum(len(r["groups"]) for r in rows) == 84
    assert all(".cache" not in p for r in rows for p in r["destinations"])
    assert "本次未重新搜索 Session" in (output / "report.html").read_text(encoding="utf-8")


def test_report_copy_verifies_only_exact_paths_and_blocks_unavailable_paths(monkeypatch):
    visited = []

    class Reader:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, path, _waiting):
            visited.append(path)
            return {"error": "directory unavailable"} if path == "missing" else {}

    monkeypatch.setattr(tool, "DirectoryReader", Reader)
    rows = [
        {"state": "匹配唯一", "sources": ["source"], "destinations": ["missing"]},
        {"state": "未找到本地原始 Session", "sources": ["skip"], "destinations": []},
    ]
    errors = tool.verify_report_paths(rows, lambda _: None)
    assert visited == ["source", "missing"]
    assert len(errors) == 1 and rows[0]["state"] == "报告路径当前不可访问，未复制"


def test_cancel_default_copy_saves_report_without_searching_or_writing_data(tmp_path, monkeypatch):
    def cancel(*_args, **_kwargs):
        raise KeyboardInterrupt

    def unexpected_copy(*_args):
        pytest.fail("Cancelled path verification must not start copying")

    monkeypatch.setattr(tool, "verify_report_paths", cancel)
    monkeypatch.setattr(tool, "copy_one", unexpected_copy)
    output = tmp_path / "report"
    assert tool.main(["copy", "--output", str(output)]) == 1
    assert "未开始复制" in (output / "scan_errors.csv").read_text(encoding="utf-8")
    assert (output / "report.html").is_file()
    assert not (output / "copy_results.json").exists()
