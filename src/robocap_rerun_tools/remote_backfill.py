"""Merge validated local exports into an existing remote Session, without deleting data."""

from __future__ import annotations

import json
from pathlib import Path

from . import modelscope_publisher as publisher
from .mocap_inventory import manifest_mocap_names


def reconcile_staged_sessions(staged, remote_entries, *, api, repository, revision, progress=None):
    from .upload_verification import file_digest

    local_entries = publisher._read_metadata(staged.metadata_path)
    by_key = {}
    for entry in remote_entries:
        key = (entry.get("primitive_id"), entry.get("session_id"))
        if key in by_key:
            raise publisher.ModelScopePublisherError(f"Duplicate remote Session: {key}")
        by_key[key] = entry
    retained = {}
    for index, entry in enumerate(local_entries):
        previous = by_key.get((entry["primitive_id"], entry["session_id"]))
        if previous is None:
            continue
        _, _, remote_path, _ = publisher._metadata_session_location(previous)
        if remote_path != entry["session_path"]:
            raise publisher.ModelScopePublisherError(
                f"Remote Session moved since staging; restage before upload: {remote_path}"
            )
        directory = staged.dataset_root / remote_path
        manifest_path = directory / "manifest.json"
        local = json.loads(manifest_path.read_text(encoding="utf-8"))
        remote_hashes = {}
        remote_files = publisher._list_remote_session_files(
            api, repository, revision, remote_path, digests=remote_hashes,
        )
        manifest_key = f"{remote_path}/manifest.json"
        remote = {}
        if remote_files.get(manifest_key, 0):
            downloaded = publisher._remote_read_with_retries(
                lambda manifest_key=manifest_key: api.download_file(
                    repository, "dataset", manifest_key, revision=revision, force=True
                )
            )
            remote = json.loads(Path(downloaded).read_text(encoding="utf-8"))
            if not isinstance(remote, dict) or (
                remote.get("primitive_id") != entry["primitive_id"]
                or remote.get("session_id") != entry["session_id"]
            ):
                raise publisher.ModelScopePublisherError(
                    f"Remote manifest identity mismatch: {manifest_key}"
                )
        if remote.get("alignment") != local.get("alignment"):
            raise publisher.ModelScopePublisherError(
                f"Cannot mix different alignment windows when backfilling: {remote_path}"
            )
        if remote.get("segment", local["segment"]) != local["segment"]:
            raise publisher.ModelScopePublisherError(
                f"Cannot change Segment scope during additive backfill: {remote_path}"
            )
        records = {}
        for record in remote.get("files", []):
            relative = publisher._manifest_relative_path(record.get("packaged_as"), "packaged_as")
            if relative == "manifest.json" or relative in records:
                raise publisher.ModelScopePublisherError(
                    f"Invalid duplicate/self manifest path: {relative}"
                )
            records[relative] = dict(record)
        # Also retain actual remote files that an older manifest forgot to list.
        for path, size in remote_files.items():
            relative = publisher._manifest_relative_path(
                path[len(remote_path) + 1 :], "remote path"
            )
            if relative != "manifest.json":
                records.setdefault(
                    relative,
                    {
                        "source": relative,
                        "packaged_as": relative,
                        "original_bytes": size,
                        "packaged_bytes": size,
                        "kind": "data",
                        "compressed_video": False,
                    },
                )
        local_records = {}
        for record in local["files"]:
            relative = publisher._manifest_relative_path(record["packaged_as"], "packaged_as")
            if (directory / relative).is_file():
                local_records[relative] = record
            elif not record.get("retained_remote"):
                raise publisher.ModelScopePublisherError(f"Staged source disappeared: {relative}")
        for relative, record in records.items():
            if relative in local_records:
                continue
            path = f"{remote_path}/{relative}"
            expected = publisher._json_nonnegative_int(
                record.get("packaged_bytes"), "packaged_bytes"
            )
            if remote_files.get(path) != expected:
                raise publisher.ModelScopePublisherError(
                    f"Remote-only file is missing or damaged, no local replacement: {path}"
                )
            # Establish exact retained bytes before uploading, even for old manifests
            # without hashes. Only files absent locally require this download.
            digest = remote_hashes.get(path)
            if digest is None:
                downloaded = publisher._remote_read_with_retries(
                    lambda path=path: api.download_file(
                        repository, "dataset", path, revision=revision, force=True
                    )
                )
                downloaded = Path(downloaded)
                if downloaded.stat().st_size != expected:
                    raise publisher.ModelScopePublisherError(f"Remote-only file size mismatch: {path}")
                digest = file_digest(downloaded)
            if record.get("sha256") and record["sha256"] != digest:
                raise publisher.ModelScopePublisherError(f"Remote-only file hash mismatch: {path}")
            record["sha256"] = digest
            record["retained_remote"] = True
            retained[path] = {"size": expected, "sha256": digest, "remote_only": True}
        records.update(local_records)
        files = [records[path] for path in sorted(records)]
        merged = {
            **remote,
            **local,
            "files": files,
            "mocap_rigid_names": manifest_mocap_names(files),
        }
        publisher._validate_manifest_video_policy(merged, manifest_path)
        manifest_path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        local_entries[index] = {
            **previous,
            **entry,
            "file_count": len(files),
            "packaged_bytes": sum(r["packaged_bytes"] for r in files),
            "mocap_rigid_names": merged["mocap_rigid_names"],
        }
        if progress:
            progress(
                f"[backfill] {remote_path}: {len(local_records)} local files, "
                f"{len(files) - len(local_records)} remote-only files retained"
            )
    publisher._write_metadata_entries(staged.dataset_root, local_entries)
    return retained
