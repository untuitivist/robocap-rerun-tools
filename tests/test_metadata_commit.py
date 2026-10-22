import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from robocap_rerun_tools.metadata_commit import MetadataCommit


def git(root, *args):
    return (
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )


def seed(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    work = tmp_path / "seed"
    work.mkdir()
    git(work, "init", "-b", "master")
    git(work, "config", "user.name", "test")
    git(work, "config", "user.email", "test@localhost")
    (work / "index.json").write_text("[]", encoding="utf-8")
    (work / "unrelated.txt").write_text("keep", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "seed")
    git(work, "push", str(remote), "master")
    return remote


def test_two_uploaders_remerge_after_non_fast_forward(tmp_path):
    remote = seed(tmp_path)
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    messages = []
    a = MetadataCommit(tmp_path / "a", str(remote), "master", progress=messages.append)
    b = MetadataCommit(tmp_path / "b", str(remote), "master")
    calls = []

    def build(transaction, parent, name):
        rows = json.loads(transaction.git("show", f"{parent}:index.json").stdout)
        return {"index.json": json.dumps(rows + [name]).encode()}

    def build_a(parent):
        changes = build(a, parent, "A")
        if not calls:
            b.publish(lambda head: build(b, head, "B"))
        calls.append(parent)
        return changes

    a.publish(build_a)
    assert json.loads(git(remote, "show", "master:index.json")) == ["B", "A"]
    assert git(remote, "show", "master:unrelated.txt") == "keep"
    assert len(calls) == 2
    assert any("Remote changed" in message for message in messages)


def test_metadata_lfs_pointer_is_published(tmp_path):
    remote = seed(tmp_path)
    work = tmp_path / "upload"
    work.mkdir()
    transaction = MetadataCommit(work, str(remote), "master")
    data = b'{"session_id":"A"}\n'
    transaction.publish(lambda parent: {"metadata.jsonl": data})
    pointer = git(remote, "show", "master:metadata.jsonl")
    import hashlib

    assert f"oid sha256:{hashlib.sha256(data).hexdigest()}" in pointer


def test_conflict_exhaustion_never_force_pushes(tmp_path):
    remote = seed(tmp_path)
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    a = MetadataCommit(tmp_path / "a", str(remote), "master")
    b = MetadataCommit(tmp_path / "b", str(remote), "master")
    count = []

    def build(parent):
        count.append(1)
        b.publish(lambda head: {"index.json": json.dumps([len(count)]).encode()})
        return {"index.json": b'["A"]'}

    with pytest.raises(RuntimeError, match="Remote kept changing"):
        a.publish(build, attempts=2)
    assert json.loads(git(remote, "show", "master:index.json")) == [2]


def test_cached_publisher_reuses_objects_and_fetches_external_changes(tmp_path, monkeypatch):
    from robocap_rerun_tools import metadata_commit as module

    remote = seed(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    settings = SimpleNamespace(endpoint="https://example.invalid", token="secret-token")
    directories = []

    def transaction(directory, url, branch, token, progress):
        directories.append(directory)
        return MetadataCommit(directory, str(remote), branch, token, progress)

    monkeypatch.setattr(module, "MetadataCommit", transaction)

    def publish(name):
        def build(parent, exists):
            assert exists("unrelated.txt")
            rows = json.loads(git(remote, "show", f"{parent}:index.json"))
            return {"index.json": json.dumps(rows + [name]).encode()}

        return module._publish_changes(build, api=None, repository="owner/data",
                                       revision="master", settings=settings, progress=None)

    first = publish("A")
    other = MetadataCommit(tmp_path / "other", str(remote), "master")
    other.publish(lambda parent: {"index.json": b'["A", "B"]'})
    publish("C")
    assert json.loads(git(remote, "show", "master:index.json")) == ["A", "B", "C"]
    assert directories[0] == directories[1]
    assert git(directories[1], "cat-file", "-t", first) == "commit"
    assert "secret-token" not in (directories[1] / "config").read_text()
    assert git(remote, "show", "master:unrelated.txt") == "keep"


def test_metadata_cache_is_partitioned_by_remote_and_branch(tmp_path, monkeypatch):
    from robocap_rerun_tools.metadata_commit import _metadata_cache_directory

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    settings = SimpleNamespace(endpoint="https://one.invalid", token="one")
    first = _metadata_cache_directory(settings, "owner/normal", "master")
    assert first != _metadata_cache_directory(settings, "owner/fault", "master")
    assert first != _metadata_cache_directory(settings, "owner/normal", "other")
    settings.endpoint = "https://two.invalid"
    assert first != _metadata_cache_directory(settings, "owner/normal", "master")


def test_existing_git_cache_cannot_be_repointed_to_another_remote(tmp_path):
    remote = seed(tmp_path)
    work = tmp_path / "cache"
    MetadataCommit(work, str(remote), "master")
    with pytest.raises(RuntimeError, match="different remote"):
        MetadataCommit(work, "https://other.invalid/data.git", "master")


def test_same_cache_serializes_simultaneous_publishers(tmp_path, monkeypatch):
    from robocap_rerun_tools import metadata_commit as module

    remote = seed(tmp_path)
    settings = SimpleNamespace(endpoint="https://example.invalid", token="")
    monkeypatch.setattr(module, "_metadata_cache_directory", lambda *a: tmp_path / "cache")
    monkeypatch.setattr(module, "MetadataCommit", lambda directory, url, branch, token, progress:
                        MetadataCommit(directory, str(remote), branch, token, progress))
    ready = threading.Barrier(2)

    def publish(name):
        ready.wait(timeout=5)

        def build(parent, exists):
            rows = json.loads(git(remote, "show", f"{parent}:index.json"))
            return {"index.json": json.dumps(rows + [name]).encode()}

        return module._publish_changes(build, api=None, repository="owner/data",
                                       revision="master", settings=settings, progress=None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        commits = list(pool.map(publish, ["A", "B"]))
    assert len(set(commits)) == 2
    assert sorted(json.loads(git(remote, "show", "master:index.json"))) == ["A", "B"]
