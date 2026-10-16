import json
import subprocess

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
