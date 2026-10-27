"""Publish metadata through the SDK; operators serialize dataset uploads."""

from __future__ import annotations

import glob
import json
import tempfile
from pathlib import Path


def _publish_changes(build, *, api, repository, revision, settings, progress):
    from modelscope_hub.errors import NotExistError

    def exists(path):
        try:
            api.download_file(repository, "dataset", path, revision=revision, force=True)
        except NotExistError:
            return False
        return True

    changes = build(revision, exists)
    if not changes:
        return revision
    if progress:
        progress(f"[metadata SDK] Uploading {len(changes)} metadata file(s).")
    with tempfile.TemporaryDirectory(prefix="robocap-metadata-") as directory:
        root = Path(directory)
        for relative, content in changes.items():
            target = (root / relative).resolve()
            if not target.is_relative_to(root.resolve()):
                raise ValueError(f"Unsafe metadata path: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        api.upload_folder(
            repository, "dataset", root, revision=revision,
            commit_message="Update Session metadata",
            allow_patterns=[glob.escape(path) for path in changes],
            use_cache=False, disable_tqdm=False,
        )
    return revision


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
    """Patch only requested capture fields using the ModelScope SDK."""
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

    return _publish_changes(
        build,
        api=api,
        repository=repository,
        revision=revision,
        settings=settings,
        progress=progress,
    )
