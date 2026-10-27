"""Publish shared metadata with Git's non-fast-forward protection."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from filelock import FileLock

UPLOAD_QUIET_SECONDS = 600

def require_git():
    if not shutil.which("git"):
        raise RuntimeError("Dataset uploads require Git for concurrent-safe metadata commits.")
    result = subprocess.run(["git", "lfs", "version"], capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("Dataset uploads require Git LFS. Install Git LFS and retry.")


class MetadataCommit:
    def __init__(self, directory, url, branch, token="", progress=None):
        self.directory = Path(directory).resolve()
        self.branch = branch
        self.progress = progress
        self.env = dict(
            os.environ,
            GIT_TERMINAL_PROMPT="0",
            GCM_INTERACTIVE="Never",
            GIT_LFS_SKIP_SMUDGE="1",
            GIT_AUTHOR_NAME="Robocap uploader",
            GIT_AUTHOR_EMAIL="robocap-uploader@localhost",
            GIT_COMMITTER_NAME="Robocap uploader",
            GIT_COMMITTER_EMAIL="robocap-uploader@localhost",
        )
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        ):
            self.env.pop(key, None)
        # Credentials live only in subprocess environment, never URLs or Git config files.
        config = [("credential.helper", ""), ("core.hooksPath", str(self.directory / "no-hooks"))]
        if token:
            auth = base64.b64encode(f"oauth2:{token}".encode()).decode()
            config.append(("http.extraHeader", f"Authorization: Basic {auth}"))
        self.env["GIT_CONFIG_COUNT"] = str(len(config))
        for i, (key, value) in enumerate(config):
            self.env[f"GIT_CONFIG_KEY_{i}"] = key
            self.env[f"GIT_CONFIG_VALUE_{i}"] = value
        self.directory.mkdir(parents=True, exist_ok=True)
        self.git("init", "--bare")
        if os.name == "nt":
            self.git("config", "core.longpaths", "true")
        self.git("check-ref-format", f"refs/heads/{branch}")
        remote = self.git("remote", "get-url", "origin", check=False)
        if remote.returncode:
            self.git("remote", "add", "origin", url)
        elif remote.stdout.decode().strip() != url:
            raise RuntimeError("Metadata cache belongs to a different remote repository.")
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "remote.origin.partialclonefilter", "blob:none")

    def git(self, *args, data=None, check=True):
        result = subprocess.run(
            ["git", *args],
            cwd=self.directory,
            env=self.env,
            input=data,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if check and result.returncode:
            # Do not surface Git's raw auth/config diagnostics.
            raise RuntimeError(
                f"Metadata Git operation {args[0]} failed (exit {result.returncode}). "
                "Check network, repository permissions and Git/LFS installation."
            )
        return result

    def head(self):
        # Keep a ref so later fetches can negotiate with objects from earlier batches.
        self.git(
            "fetch", "--no-tags", "--depth=1", "--filter=blob:none", "origin",
            f"+refs/heads/{self.branch}:refs/remotes/origin/{self.branch}",
        )
        return self.git("rev-parse", "FETCH_HEAD").stdout.decode().strip()

    def publish(self, build, attempts=8):
        """Rebuild changed small files against each fresh head, then push without force."""
        for attempt in range(attempts):
            if self.progress:
                self.progress(
                    f"[metadata {attempt + 1}/{attempts}] Fetching latest revision and merging index."
                )
            parent = self.head()
            changes = build(parent)
            if not changes:
                return parent
            self.git("read-tree", parent)
            for path, content in changes.items():
                if path.startswith("/") or ".." in Path(path).parts or "\n" in path:
                    raise ValueError(f"Unsafe metadata path: {path}")
                # The root index may be LFS-managed. Store/upload its pointer without checking out videos.
                if path == "metadata.jsonl":
                    content = self.git("lfs", "clean", "--", path, data=content).stdout
                    oid = next(
                        line.split(b":", 1)[1]
                        for line in content.splitlines()
                        if line.startswith(b"oid sha256:")
                    )
                    self.git("lfs", "push", "--object-id", "origin", oid.decode())
                blob = (
                    self.git("hash-object", "-w", "--stdin", data=content).stdout.decode().strip()
                )
                self.git("update-index", "--add", "--cacheinfo", "100644", blob, path)
            tree = self.git("write-tree").stdout.decode().strip()
            commit = (
                self.git("commit-tree", tree, "-p", parent, "-m", "Merge uploaded Session metadata")
                .stdout.decode()
                .strip()
            )
            result = self.git(
                "push", "--porcelain", "origin", f"{commit}:refs/heads/{self.branch}", check=False
            )
            if not result.returncode:
                return commit
            latest = self.head()
            if latest == commit:  # Server accepted the push but the response was lost.
                return commit
            if latest == parent:
                raise RuntimeError(
                    "Metadata push failed without a remote version change; check network/permissions."
                )
            if self.progress:
                self.progress(
                    f"Remote changed: rebuilding metadata against latest revision ({attempt + 1}/{attempts})."
                )
            if attempt + 1 < attempts:
                self.wait_until_quiet()
        raise RuntimeError(
            "Remote kept changing; metadata was not published. Retry upload; existing files can be reused."
        )

    def wait_until_quiet(self, own_revision=None):
        """Observe commits, not in-flight transfers; retain non-force push protection."""
        observed = None
        committed_at = None
        while True:
            refs = self.git("ls-remote", "--heads", "origin", f"refs/heads/{self.branch}").stdout.split()
            if not refs:
                return  # A new, empty repository has no upload history.
            head = refs[0].decode()
            if head == own_revision:
                if self.progress:
                    self.progress("[upload wait] Latest commit is this run's previous batch; continuing without delay.")
                return
            if head != observed:
                head = self.head()
                committed_at = int(self.git("show", "-s", "--format=%ct", head).stdout.strip())
                observed = head
            if time.time() - committed_at >= UPLOAD_QUIET_SECONDS:
                if self.progress:
                    self.progress("[upload wait] No remote commit in the last 10 minutes; starting upload.")
                return
            if self.progress:
                next_check = datetime.fromtimestamp(time.time() + UPLOAD_QUIET_SECONDS).astimezone().isoformat(timespec="seconds")
                self.progress(
                    "[upload wait] Remote committed within the last 10 minutes. "
                    f"Waiting 10 minutes; next check at {next_check}; "
                    "no file scanning during this wait."
                )
            time.sleep(UPLOAD_QUIET_SECONDS)


def wait_for_upload_turn(settings, repository, revision, progress=None, own_revision=None):
    directory = _metadata_cache_directory(settings, repository, revision)
    directory.parent.mkdir(parents=True, exist_ok=True)
    # A separate observation cache lets local metadata publication proceed while waiting.
    directory = directory.with_name(directory.name + "-activity")
    with FileLock(str(directory) + ".lock", timeout=-1):
        transaction = MetadataCommit(
            directory, f"{settings.endpoint.rstrip('/')}/datasets/{repository}.git",
            revision, settings.token, progress,
        )
        transaction.wait_until_quiet(own_revision)


def _metadata_cache_directory(settings, repository, revision):
    root = os.environ.get("LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME")
    cache = Path(root) if root else Path.home() / ".cache"
    identity = f"{settings.endpoint.rstrip('/')}\n{repository}\n{revision}"
    key = hashlib.sha256(identity.encode()).hexdigest()
    return cache / "robocap-rerun-tools" / "metadata-git" / key


def _publish_changes(build, *, api, repository, revision, settings, progress):
    directory = _metadata_cache_directory(settings, repository, revision)
    directory.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("[metadata cache] Reusing local Git objects; waiting for cache lock.")
    # Different processes share objects, but must never modify the same Git index together.
    # This lock only protects the local cache; remote conflicts still use fresh-head retries.
    with FileLock(str(directory) + ".lock", timeout=600):
        transaction = MetadataCommit(
            directory,
            f"{settings.endpoint.rstrip('/')}/datasets/{repository}.git",
            revision,
            settings.token,
            progress,
        )
        return transaction.publish(
            lambda parent: build(
                parent,
                lambda path: bool(
                    transaction.git("ls-tree", "--name-only", parent, "--", path).stdout.strip()
                ),
            )
        )


def publish_session_metadata(
    entries, *, api, repository, revision, settings, progress=None, initial_readme=None
):
    from . import modelscope_publisher as publisher

    def build(parent, exists):
        original = publisher._download_remote_metadata(api, repository, parent)
        merged = publisher._metadata_document(
            publisher.merge_metadata_entries(
                publisher._read_metadata_document(original, "remote metadata"), entries
            )
        )
        changes = {} if merged == original else {publisher.METADATA_NAME: merged.encode("utf-8")}
        if initial_readme is not None and not exists("README.md"):
            changes["README.md"] = initial_readme.read_bytes()
        return changes

    return _publish_changes(
        build,
        api=api,
        repository=repository,
        revision=revision,
        settings=settings,
        progress=progress,
    )


def publish_capture_metadata(updates, *, api, repository, revision, settings, progress=None):
    """Patch only requested capture fields, rebuilding manifests on each conflict."""
    from . import modelscope_publisher as publisher

    def build(parent, exists):
        original = publisher._download_remote_metadata(api, repository, parent)
        entries = publisher._read_metadata_document(original, "remote metadata")
        changes = {}
        found = set()
        for entry in entries:
            key = (entry.get("primitive_id"), entry.get("session_id"))
            if key not in updates:
                continue
            found.add(key)
            record = updates[key].as_record()
            entry["mocap_capture"] = record
            remote = f"{entry['session_path']}/manifest.json"
            local = api.download_file(repository, "dataset", remote, revision=parent, force=True)
            manifest = json.loads(Path(local).read_text(encoding="utf-8"))
            if (manifest.get("primitive_id"), manifest.get("session_id")) != key:
                raise ValueError(f"Manifest identity mismatch: {remote}")
            if manifest.get("mocap_capture") != record:
                manifest["mocap_capture"] = record
                changes[remote] = (
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
                ).encode()
        if found != set(updates):
            raise ValueError(
                "A selected Session disappeared from remote metadata; refresh before retrying."
            )
        merged = publisher._metadata_document(entries)
        if merged != original:
            changes[publisher.METADATA_NAME] = merged.encode()
        return changes

    require_git()
    return _publish_changes(
        build,
        api=api,
        repository=repository,
        revision=revision,
        settings=settings,
        progress=progress,
    )
