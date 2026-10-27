import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from robocap_rerun_tools import metadata_commit as metadata


class Api:
    def __init__(self, root):
        self.root = root
        self.files = {
            "README.md": b"Custom card",
            "metadata.jsonl": b'{"primitive_id":"P01","session_id":"old","extra":"keep"}\n',
        }
        self.uploads = []

    def download_file(self, repository, kind, path, **kwargs):
        local = self.root / "download" / path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.files[path])
        return local

    def upload_folder(self, repository, kind, root, **kwargs):
        files = {p.relative_to(root).as_posix(): p.read_bytes()
                 for p in Path(root).rglob("*") if p.is_file()}
        self.uploads.append((files, kwargs))
        self.files.update(files)


def test_sdk_metadata_merges_rows_and_preserves_readme_without_git(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("No Git subprocess"))
    api = Api(tmp_path)
    card = tmp_path / "README.md"
    card.write_text("Generated card", encoding="utf-8")
    row = {"primitive_id": "P02", "session_id": "new", "collection_site": {"id": "site_01"}}
    metadata.publish_session_metadata(
        [row], api=api, repository="owner/data", revision="master",
        settings=SimpleNamespace(), initial_readme=card,
    )
    assert api.files["README.md"] == b"Custom card"
    rows = [json.loads(line) for line in api.files["metadata.jsonl"].splitlines()]
    assert rows[0]["extra"] == "keep"
    assert row in rows
    assert len(api.uploads) == 1
    assert set(api.uploads[0][0]) == {"metadata.jsonl"}
    assert api.uploads[0][1]["use_cache"] is False


def test_sdk_metadata_upload_failure_propagates_without_conflict_retry(tmp_path):
    api = Api(tmp_path)
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError("network failure")

    api.upload_folder = fail
    with pytest.raises(OSError, match="network failure"):
        metadata._publish_changes(
            lambda revision, exists: {"metadata.jsonl": b"new"},
            api=api, repository="owner/data", revision="master",
            settings=None, progress=None,
        )
    assert calls == [1]


def test_unchanged_metadata_does_not_upload(tmp_path):
    api = Api(tmp_path)
    metadata._publish_changes(
        lambda revision, exists: {}, api=api, repository="owner/data",
        revision="master", settings=None, progress=None,
    )
    assert api.uploads == []
