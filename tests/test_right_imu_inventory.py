import hashlib
import json
import shutil
import sqlite3
from contextlib import closing

import pytest

from robocap_rerun_tools import right_imu_inventory as inventory


def database(path, acc=2, gyro=3, tables=("acc_data", "gyro_data")):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        for table, count in (("acc_data", acc), ("gyro_data", gyro)):
            if table not in tables:
                continue
            connection.execute(f"CREATE TABLE {table}(timestamp INTEGER,x REAL,y REAL,z REAL)")
            connection.executemany(
                f"INSERT INTO {table} VALUES (?,?,?,?)",
                [(1_000_000_000 + i * 10_000_000, 1.0, 2.0, 3.0) for i in range(count)],
            )
        connection.commit()
    return path


def session(root, name="20261008_170000_session7", segments=(1,)):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "mocap-LM01-S02-wang-yang-01p11").mkdir()
    for segment in segments:
        (directory / f"robocap_segment{segment}_video_left.mp4").write_bytes(b"video")
    return directory


def wrist(directory, device="device_a", segment=1, **kwargs):
    return database(
        directory / f"robowrist_{device}_right" / f"robowrist_segment{segment}_imu_right.db",
        **kwargs,
    )


@pytest.mark.parametrize(
    ("acc", "gyro", "state"),
    [
        (2, 3, "has_data"),
        (0, 0, "empty"),
        (0, 3, "partial"),
        (2, 0, "partial"),
    ],
)
def test_real_database_counts_and_read_only(tmp_path, acc, gyro, state):
    path = database(tmp_path / "数据 #1.db", acc, gyro)
    before = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
    row = inventory.inspect_database(path)
    assert row["state"] == state
    assert row["acc_rows"] == acc and row["gyro_rows"] == gyro
    assert row["gyro_span_s"] == (pytest.approx(0.02) if gyro else None)
    assert before == (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    ("payload", "state"),
    [
        (b"", "zero_byte"),
        (b"not a sqlite database" * 100, "sqlite_error"),
        (b"version https://git-lfs.github.com/spec/v1\noid sha256:123\n", "lfs_pointer"),
    ],
)
def test_non_database_files_are_distinct(tmp_path, payload, state):
    path = tmp_path / "imu.db"
    path.write_bytes(payload)
    row = inventory.inspect_database(path)
    assert row["state"] == state and row["acc_rows"] is None


def test_missing_table_and_columns_are_not_empty_streams(tmp_path):
    path = database(tmp_path / "imu.db", acc=0, tables=("acc_data",))
    row = inventory.inspect_database(path)
    assert row["state"] == "schema_error" and "gyro_data" in row["detail"]
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE gyro_data(timestamp INTEGER)")
    row = inventory.inspect_database(path)
    assert row["state"] == "schema_error" and "columns x, y, z" in row["detail"]


def test_live_wal_is_not_misreported_as_empty(tmp_path):
    path = database(tmp_path / "imu.db", acc=0, gyro=0)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO acc_data VALUES(100,1,2,3)")
        connection.commit()
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        row = inventory.inspect_database(path)
        assert row["state"] == "active_database" and row["acc_rows"] is None
        assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert inventory.inspect_database(path)["state"] == "partial"


def test_changes_during_read_invalidate_counts(tmp_path, monkeypatch):
    path = database(tmp_path / "imu.db")
    original = inventory._database_contents

    def change_file(target):
        result = original(target)
        with target.open("ab") as stream:
            stream.write(b"modified")
        return result

    monkeypatch.setattr(inventory, "_database_contents", change_file)
    row = inventory.inspect_database(path)
    assert row["state"] == "changed" and row["acc_rows"] is None


def test_scan_excludes_head_imu_and_handles_missing_segment(tmp_path):
    directory = session(tmp_path / "raw", segments=(1, 2))
    wrist(directory)
    database(directory / "robocap_segment2_imu_right.db")
    database(directory / "robowrist_left_left" / "robowrist_segment2_imu_left.db")
    rows, errors = inventory.scan([directory], tmp_path / "out", log=lambda _: None)
    assert not errors and len(rows) == 2
    assert [(r["segment"], r["state"]) for r in rows] == [(1, "has_data"), (2, "missing_file")]
    assert rows[1]["device_id"] == "device_a"
    assert rows[1]["device_source"] == "unique_session_directory"
    assert rows[0]["date"] == "2026-10-09"  # Session name is UTC; reporting uses UTC+08.
    assert rows[0]["participant"] == "wang-yang" and rows[0]["action_id"] == "LM01"


def test_different_devices_per_segment_do_not_create_fake_missing_files(tmp_path):
    directory = session(tmp_path / "raw", segments=(1, 2, 3))
    wrist(directory, "first", 1)
    wrist(directory, "second", 2, acc=0, gyro=0)
    rows, errors = inventory.scan([directory], tmp_path / "out", log=lambda _: None)
    assert not errors
    assert [(r["segment"], r["device_id"], r["state"]) for r in rows] == [
        (1, "first", "has_data"),
        (2, "second", "empty"),
        (3, "unknown", "missing_file"),
    ]


def test_other_wrist_stream_identifies_device_of_missing_imu(tmp_path):
    directory = session(tmp_path / "raw", segments=(1, 2))
    wrist(directory, "first", 1)
    other = directory / "robowrist_second_right"
    other.mkdir()
    (other / "robowrist_segment2_video_right_down.mp4").write_bytes(b"video")
    rows, errors = inventory.scan([directory], tmp_path / "out", log=lambda _: None)
    assert not errors and len(rows) == 2
    assert rows[1]["device_id"] == "second" and rows[1]["state"] == "missing_file"


@pytest.mark.parametrize("declared", [True, False])
def test_missing_wrist_uses_manifest_or_keeps_unknown(tmp_path, declared):
    directory = session(tmp_path / "raw")
    manifest = {"device_ids": {"right": "right_device"}} if declared else {}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    rows, errors = inventory.scan([directory], tmp_path / "out", log=lambda _: None)
    assert not errors and len(rows) == 1 and rows[0]["state"] == "missing_file"
    assert rows[0]["device_id"] == ("right_device" if declared else "unknown")


def test_packaged_identity_from_manifest_and_direct_wrist_file(tmp_path):
    directory = tmp_path / "20261001_010000_session1"
    directory.mkdir()
    database(directory / "robowrist_segment2_imu_right.db")
    manifest = {
        "mocap_capture": {
            "participant": "xiaobo",
            "action_id": "P01",
            "source_directory": "mocap-P01-S01-xiaobo-1p",
        }
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    rows, errors = inventory.scan([directory], tmp_path / "out", log=lambda _: None)
    assert not errors and len(rows) == 1
    assert rows[0]["participant"] == "xiaobo" and rows[0]["device_id"] == "unknown"
    assert rows[0]["segment"] == 2 and rows[0]["state"] == "has_data"


def test_invalid_manifest_does_not_hide_the_database_result(tmp_path):
    directory = session(tmp_path / "source")
    wrist(directory)
    (directory / "manifest.json").write_text("[]", encoding="utf-8")
    rows, errors = inventory.scan([directory], tmp_path / "out", log=lambda _: None)
    assert errors and "JSON object" in errors[0]["error"]
    assert len(rows) == 1 and rows[0]["state"] == "has_data"
    assert rows[0]["participant"] == "wang-yang"


def test_duplicate_roots_skipped_but_separate_copies_are_counted(tmp_path):
    root = tmp_path / "raw"
    directory = session(root)
    wrist(directory)
    staged = session(root / "_modelscope_dataset")
    wrist(staged, "staged")
    rows, errors = inventory.scan([root, directory, root], tmp_path / "out", log=lambda _: None)
    assert not errors and len(rows) == 1
    copied = tmp_path / "copy"
    shutil.copytree(root, copied)
    rows, _ = inventory.scan([root, copied], tmp_path / "out", log=lambda _: None)
    devices = inventory.summarize(rows, "device_id")
    assert len(devices) == 1 and devices[0]["checks"] == 2
    assert devices[0]["session_directories"] == 2


def test_empty_session_is_visible_and_ambiguous_identity_not_guessed(tmp_path):
    directory = session(tmp_path / "raw", segments=())
    rows, errors = inventory.scan([directory], tmp_path / "out", log=lambda _: None)
    assert not errors and rows[0]["state"] == "no_segment"
    (directory / "mocap-P02-S01-someone-1p").mkdir()
    rows, errors = inventory.scan([directory], tmp_path / "out", log=lambda _: None)
    assert errors and rows[0]["participant"] == ""


def test_cli_reports_preserve_sources_and_escape_html(tmp_path):
    root = tmp_path / "source"
    directory = session(root)
    path = wrist(directory)
    before = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()
    }
    output = tmp_path / "out"
    assert inventory.main(["--root", str(root), "--output", str(output)]) == 0
    assert before == {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()
    }
    for name in (
        "report.html",
        "devices.csv",
        "dates.csv",
        "participants.csv",
        "files.csv",
        "scan_errors.csv",
        "summary.json",
        "run.log",
    ):
        payload = (output / name).read_bytes()
        assert not payload.startswith(b"\xef\xbb\xbf")
        payload.decode("utf-8")
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["scan_complete"] and summary["states"] == {"has_data": 1}
    rows, _ = inventory.scan([root], output, log=lambda _: None)
    rows[0]["participant"] = "<script>alert(1)</script>"
    inventory.write_reports(output, [root], rows, [])
    report = (output / "report.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report
    assert "<script>alert(1)</script>" not in report
    assert str(path) in report
    with pytest.raises(SystemExit) as exc:
        inventory.main(["--root", str(root), "--output", str(output)])
    assert exc.value.code == 2


def test_missing_root_and_unfinished_database_mark_scan_incomplete(tmp_path):
    output = tmp_path / "missing_report"
    assert inventory.main(["--root", str(tmp_path / "missing"), "--output", str(output)]) == 1
    directory = session(tmp_path / "source")
    path = wrist(directory)
    path.with_name(path.name + "-journal").write_bytes(b"pending transaction")
    rows, errors = inventory.scan([directory], tmp_path / "out", log=lambda _: None)
    assert errors and rows[0]["state"] == "active_database"


def test_permission_error_not_reported_as_missing(tmp_path, monkeypatch):
    directory = session(tmp_path / "source")
    path = wrist(directory)
    original = type(path).iterdir

    def blocked(current):
        if current == path.parent:
            raise PermissionError("blocked")
        return original(current)

    monkeypatch.setattr(type(path), "iterdir", blocked)
    rows, errors = inventory.scan([directory], tmp_path / "out", log=lambda _: None)
    assert errors and [r["state"] for r in rows] == ["read_error"]
