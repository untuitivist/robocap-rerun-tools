import fnmatch
import hashlib
import json
from types import SimpleNamespace

import pytest

from robocap_rerun_tools import modelscope_publisher as publisher
from robocap_rerun_tools import upload_verification as verification


class RemoteApi:
    def __init__(self, root):
        self.root = root
        self.files = {}
        self.uploads = []
        self.downloads = []
        self.break_path = None
        self.hashes = True

    def upload_folder(self, repo, kind, root, **kwargs):
        self.uploads.append(kwargs)
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatchcase(relative, pattern) for pattern in kwargs["allow_patterns"]):
                self.files[relative] = path.read_bytes()
        if self.break_path:
            self.files.pop(self.break_path, None)

    def list_repo_files(self, *args, **kwargs):
        return [
            SimpleNamespace(
                path=path,
                size=len(data),
                type="blob",
                sha256=hashlib.sha256(data).hexdigest() if self.hashes else None,
            )
            for path, data in self.files.items()
        ]

    def download_file(self, repo, kind, path, **kwargs):
        assert kwargs["force"] is True
        self.downloads.append(path)
        target = self.root / "download" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.files[path])
        return target


@pytest.fixture
def upload(tmp_path):
    root = tmp_path / "stage"
    relative = "EgoMotionActions/20260925/Walk [v2]/session1"
    directory = root / relative
    directory.mkdir(parents=True)
    (directory / "video.mp4").write_bytes(b"original-video")
    (directory / "manifest.json").write_text(
        json.dumps({"files": [{"packaged_as": "video.mp4", "packaged_bytes": 14}]}),
        encoding="utf-8",
    )
    (root / "README.md").write_text("dataset", encoding="utf-8")
    entries = [
        {
            "primitive_id": "Walk [v2]",
            "session_id": "session1",
            "session_path": relative,
            "upload_batch_id": "20260925",
            "duration_s": 30,
        }
    ]
    publisher._write_metadata_entries(root, entries)
    staged = SimpleNamespace(
        dataset_root=root, session_paths=(relative,), readme_path=root / "README.md"
    )
    inventory = verification.prepare_inventory(staged, None)
    api = RemoteApi(tmp_path)
    api.files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    api.files["unrelated.bin"] = b"leave unchanged"
    return staged, entries, inventory, api


def run(upload):
    staged, entries, inventory, api = upload
    verification.verify_and_repair(
        staged,
        entries,
        inventory,
        api=api,
        repository="owner/data",
        revision="master",
        max_workers=2,
        token="secret",
        progress=None,
    )
    return json.loads((staged.dataset_root / verification.REPORT_NAME).read_text(encoding="utf-8"))


@pytest.mark.parametrize("failure", ["missing", "size", "hash"])
def test_repair_only_bad_file_and_reverify(upload, failure):
    staged, _, _, api = upload
    path = f"{staged.session_paths[0]}/video.mp4"
    if failure == "missing":
        del api.files[path]
    elif failure == "size":
        api.files[path] = b"short"
    else:
        api.files[path] = b"x" * len(api.files[path])
    report = run(upload)
    assert report["status"] == "verified"
    assert len(report["rounds"]) == 2
    assert len(api.uploads) == 1
    assert api.uploads[0]["allow_patterns"] == [
        "EgoMotionActions/20260925/Walk [[]v2]/session1/video.mp4"
    ]
    assert api.uploads[0]["use_cache"] is False
    assert api.files[path] == b"original-video"
    assert api.files["unrelated.bin"] == b"leave unchanged"


def test_missing_remote_hash_downloads_bytes_for_verification(upload):
    api = upload[3]
    api.hashes = False
    report = run(upload)
    assert report["status"] == "verified"
    assert set(upload[2]).issubset(api.downloads)
    assert not api.uploads


def test_repair_exhaustion_never_reports_success(upload):
    staged, _, _, api = upload
    path = f"{staged.session_paths[0]}/video.mp4"
    api.break_path = path
    del api.files[path]
    with pytest.raises(verification.UploadVerificationError, match="after 3 repair attempts"):
        run(upload)
    assert len(api.uploads) == 3
    report = json.loads(
        (staged.dataset_root / verification.REPORT_NAME).read_text(encoding="utf-8")
    )
    assert report["status"] == "failed"
    assert len(report["rounds"]) == 4


def test_metadata_repair_preserves_other_sessions(upload):
    api = upload[3]
    other = {"primitive_id": "P01", "session_id": "other", "duration_s": 123}
    api.files["metadata.jsonl"] = (json.dumps(other) + "\n").encode()
    assert run(upload)["status"] == "verified"
    rows = [json.loads(line) for line in api.files["metadata.jsonl"].decode().splitlines()]
    assert other in rows
    assert upload[1][0] in rows
    assert api.uploads[0]["allow_patterns"] == ["metadata.jsonl"]


def test_changed_source_is_not_uploaded_as_repair(upload):
    staged, _, _, api = upload
    path = f"{staged.session_paths[0]}/video.mp4"
    del api.files[path]
    (staged.dataset_root / path).write_bytes(b"changed-source")
    with pytest.raises(verification.UploadVerificationError, match="source changed"):
        run(upload)
    assert not api.uploads


def test_missing_staged_file_rejected_before_upload(upload):
    staged = upload[0]
    manifest = staged.dataset_root / staged.session_paths[0] / "manifest.json"
    manifest.write_text(
        '{"files":[{"packaged_as":"missing.csv","packaged_bytes":10}]}', encoding="utf-8"
    )
    with pytest.raises(verification.UploadVerificationError, match="missing or size changed"):
        verification.prepare_inventory(staged, None)


def test_listing_failure_does_not_blindly_reupload(upload, monkeypatch):
    monkeypatch.setattr(publisher, "_remote_read_with_retries", lambda op: op())

    def fail(*a, **kw):
        raise OSError("listing unavailable")

    monkeypatch.setattr(upload[3], "list_repo_files", fail)
    with pytest.raises(verification.UploadVerificationError, match="could not complete"):
        run(upload)
    assert not upload[3].uploads
