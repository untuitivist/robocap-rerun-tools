import csv
import json
from types import SimpleNamespace

import pytest

from robocap_rerun_tools import repair_upload as repair


def request_csv(path, bad_file="robocap_segment1_video_right_front.mp4"):
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(
            out, fieldnames=["session_path", "session", "error_type", "bad_file"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "session_path": "EgoMotionActions/20260917/D49/take1",
                "session": "take1",
                "error_type": "video_unreadable",
                "bad_file": bad_file,
            }
        )


@pytest.mark.parametrize("bad", ["../x.mp4", "C:/x.mp4", "/x.mp4", "mocap/../../x.csv"])
def test_repair_rejects_unsafe_paths(tmp_path, bad):
    path = tmp_path / "issues.csv"
    request_csv(path, bad)
    with pytest.raises(ValueError):
        repair.read_requests(path)


def test_search_roots_deduplicates_and_skips_staging(tmp_path):
    (tmp_path / "root" / "take1").mkdir(parents=True)
    (tmp_path / "root" / "_modelscope_dataset" / "take1").mkdir(parents=True)
    result = repair.search_sessions(
        [tmp_path / "root", tmp_path / "root" / "take1"], {"take1"}, lambda _: None
    )
    assert result == {"take1": [tmp_path / "root" / "take1"]}


def test_mocap_alias_resolves_and_ambiguity_is_rejected(tmp_path):
    directory = tmp_path / "mocap-D49-S1-user-2p"
    directory.mkdir()
    file = directory / "body.csv"
    file.write_text("data", encoding="utf-8")
    assert repair.locate_file(tmp_path, "mocap/body.csv") == file
    records = {"mocap-D49-S1-user-2p/body.trc": {}}
    assert repair.remote_file_path("mocap/body.csv", records) == "mocap-D49-S1-user-2p/body.csv"
    second = tmp_path / "mocap-other"
    second.mkdir()
    (second / "body.csv").write_text("data", encoding="utf-8")
    with pytest.raises(ValueError, match="found 2"):
        repair.locate_file(tmp_path, "mocap/body.csv")


def test_trc_variable_headers_and_bad_rows(tmp_path):
    path = tmp_path / "body.trc"
    header = "DataRate\tCameraRate\tNumFrames\tNumMarkers\n120\t120\t1\t1\nFrame#\tTime\tTimestamp\tmarker\t\t\n\t\t\tX1\tY1\tZ1\n"
    path.write_text(header + "1\t0.0\t0\t1\t2\t3\t\n", encoding="utf-8")
    repair.validate_trc(path)
    path.write_text(header + "1\t0.0\t0\t1\t2\t3\t4\n", encoding="utf-8")
    with pytest.raises(ValueError, match="columns"):
        repair.validate_trc(path)
    path.write_text(header.replace("\t1\t1\n", "\t2\t1\n") + "1\t0\t0\t1\t2\t3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="count mismatch"):
        repair.validate_trc(path)


def test_video_uses_full_decode_and_rejects_failure(tmp_path, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "ffprobe":
            return SimpleNamespace(stdout=b'{"streams":[{}]}')
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(repair.subprocess, "run", run)
    with pytest.raises(ValueError, match="decode failed"):
        repair.validate_file(tmp_path / "bad.mp4", "ffmpeg", "ffprobe", tmp_path / "decode.log")
    assert "-xerror" in calls[1][0] and "-t" not in calls[1][0]
    assert "timeout" not in calls[1][1]


def test_preview_never_uploads_and_different_copies_block(tmp_path, monkeypatch):
    csv_path = tmp_path / "issues.csv"
    request_csv(csv_path)
    roots = []
    for name, data in [("a", b"one"), ("b", b"two")]:
        root = tmp_path / name
        source = root / "take1"
        source.mkdir(parents=True)
        (source / "robocap_segment1_video_right_front.mp4").write_bytes(data)
        roots.append(root)
    monkeypatch.setattr(repair, "_hub_api", lambda _: pytest.fail("preview must not connect"))
    out = tmp_path / "output"
    assert (
        repair.main(
            [
                "--csv",
                str(csv_path),
                "--root",
                str(roots[0]),
                "--root",
                str(roots[1]),
                "--output",
                str(out),
            ]
        )
        == 1
    )
    result = json.loads((out / "results.json").read_text(encoding="utf-8"))[0]
    assert result["status"] == "failed" and "differ" in result["detail"][-1]


def test_repair_preserves_remote_path_and_updates_only_selected_files(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    name = "robocap_segment1_video_right_front.mp4"
    (source / name).write_bytes(b"original")
    destination = "EgoMotionActions/20260917/D49/take1"
    manifest = {
        "dataset_path": destination,
        "session_id": "take1",
        "primitive_id": "D49",
        "upload_batch_id": "20260917",
        "segment": "segment1",
        "duration_s": 1,
        "device_ids": {},
        "mocap_capture": {"participant": "alice"},
        "files": [
            {"packaged_as": name, "packaged_bytes": 0},
            {"packaged_as": "untouched.db", "packaged_bytes": 12},
        ],
    }
    remote_manifest = tmp_path / "manifest.json"
    remote_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    unrelated = {
        "primitive_id": "P02",
        "session_id": "other",
        "session_path": "EgoMotionActions/20260916/P02/other",
    }
    state = {"metadata": json.dumps(unrelated), "calls": 0}
    monkeypatch.setattr(repair, "_download_remote_metadata", lambda *args: state["metadata"])
    monkeypatch.setattr(
        repair,
        "load_catalog",
        lambda *args, **kwargs: {"alice": {"gender": "female", "height_cm": 160, "weight_kg": 50}},
    )

    class Api:
        def __init__(self):
            self.legacy = self

        def download_file(self, *args, **kwargs):
            return remote_manifest

        def upload_folder(self, repo, kind, root, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise OSError("transient failure")
            assert set(kwargs["allow_patterns"]) == {
                destination + "/" + name,
                destination + "/manifest.json",
                "metadata.jsonl",
            }
            assert not kwargs["sync_remote_repo"] and not kwargs["use_cache"]
            assert (root / destination / name).read_bytes() == b"original"
            remote_manifest.write_bytes((root / destination / "manifest.json").read_bytes())
            state["metadata"] = (root / "metadata.jsonl").read_text(encoding="utf-8")

        def list_dataset_files_paginated(self, *args, **kwargs):
            return [{"Path": destination + "/" + name, "Size": 8}]

    request = {"session_path": destination, "session": "take1"}
    result = repair.repair_one(
        Api(),
        SimpleNamespace(repo_id="owner/data", token="secret"),
        request,
        source,
        {name: source / name},
        tmp_path / "out",
        lambda _: None,
    )
    assert state["calls"] == 2 and result[name]["bytes"] == 8
    rows = [json.loads(x) for x in state["metadata"].splitlines()]
    assert unrelated in rows
    updated = next(r for r in rows if r["session_id"] == "take1")
    assert updated["packaged_bytes"] == 20 and updated["participant_height_cm"] == 160
