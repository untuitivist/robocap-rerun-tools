"""Verify uploaded bytes against staging, repairing only failed remote paths."""

from __future__ import annotations

import glob
import hashlib
import json
import re
from pathlib import Path

from modelscope_hub.errors import HubError

from . import modelscope_publisher as publisher

REPAIR_RETRIES = 3
REPORT_NAME = "upload_verification.json"


class UploadVerificationError(publisher.ModelScopePublisherError):
    """Post-upload verification exhausted its own repair attempts."""


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_inventory(staged, progress, *, retained=None):
    retained = retained or {}
    paths = set()
    declared_hashes = {}
    manifests = {}
    for session_path in staged.session_paths:
        directory = staged.dataset_root / session_path
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        manifests[directory / "manifest.json"] = manifest
        for record in manifest["files"]:
            relative = publisher._manifest_relative_path(record["packaged_as"], "packaged_as")
            path = directory / relative
            if not path.is_file() and f"{session_path}/{relative}" in retained:
                if retained[f"{session_path}/{relative}"]["size"] != record["packaged_bytes"]:
                    raise UploadVerificationError(f"Retained size mismatch: {path}")
                continue
            if not path.is_file() or path.stat().st_size != record["packaged_bytes"]:
                raise UploadVerificationError(f"Staged file missing or size changed: {path}")
            if record.get("sha256"):
                declared_hashes[path] = record["sha256"]
        paths.update(path for path in directory.rglob("*") if path.is_file())
    calibration = staged.dataset_root / publisher.CALIBRATION_DIR_NAME
    paths.update(path for path in calibration.rglob("*") if path.is_file())
    inventory = dict(retained)
    for number, path in enumerate(sorted(paths), 1):
        relative = path.relative_to(staged.dataset_root).as_posix()
        if progress:
            progress(f"[upload hash {number}/{len(paths)}] {relative}")
        before = path.stat()
        digest = file_digest(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise UploadVerificationError(f"Staged file changed while hashing: {path}")
        if path in declared_hashes and declared_hashes[path] != digest:
            raise UploadVerificationError(f"Staged hash differs from manifest: {path}")
        inventory[relative] = {"size": before.st_size, "sha256": digest}
    for path, manifest in manifests.items():
        changed = False
        for record in manifest["files"]:
            relative = (path.parent / record["packaged_as"]).relative_to(staged.dataset_root).as_posix()
            digest = inventory[relative]["sha256"]
            if record.get("sha256") != digest:
                record["sha256"] = digest
                changed = True
        if changed:
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            inventory[path.relative_to(staged.dataset_root).as_posix()] = {
                "size": path.stat().st_size, "sha256": file_digest(path),
            }
    return inventory


def _remote_inventory(api, repository, revision, staged):
    legacy_list = getattr(getattr(api, "legacy", None), "list_repo_files", None)
    if callable(legacy_list):
        roots = [*staged.session_paths]
        if (staged.dataset_root / publisher.CALIBRATION_DIR_NAME).is_dir():
            roots.append(publisher.CALIBRATION_DIR_NAME)
        items = []
        for root in ["", *roots]:
            items.extend(
                publisher._remote_read_with_retries(
                    lambda root=root: legacy_list(
                        repo_id=repository,
                        repo_type="dataset",
                        revision=revision,
                        root=root,
                        recursive=bool(root),
                    )
                )
            )
    else:
        items = publisher._remote_read_with_retries(
            lambda: api.list_repo_files(repository, "dataset", revision=revision, recursive=True)
        )
    result = {}
    for item in items:
        path, kind, size = publisher._remote_file_fields(item)
        if kind == "tree":
            continue
        digest = (
            (item.get("Sha256") or item.get("sha256"))
            if isinstance(item, dict)
            else getattr(item, "sha256", None)
        )
        digest = str(digest or "").lower()
        result[path] = (size, digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None)
    return result


def verify_and_repair(
    staged, entries, inventory, *, api, repository, revision, max_workers, token, progress,
    check_metadata=True, publish_metadata=None,
):
    report_path = staged.dataset_root / REPORT_NAME
    report = {
        "repository": repository,
        "revision": revision,
        "status": "checking",
        "session_paths": list(staged.session_paths),
        "files": inventory,
        "rounds": [],
    }

    def log(message):
        if progress:
            progress(message)

    def save():
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    save()
    pending_upload_error = None
    for attempt in range(REPAIR_RETRIES + 1):
        issues = {}
        log(
            f"[upload verification {attempt + 1}/{REPAIR_RETRIES + 1}] Checking {len(inventory)} files"
            + (" and metadata" if check_metadata else " before metadata publication")
        )
        try:
            remote = _remote_inventory(api, repository, revision, staged)
            for number, (path, expected) in enumerate(inventory.items(), 1):
                actual = remote.get(path)
                if actual is None:
                    issues[path] = "remote file missing"
                elif actual[0] != expected["size"]:
                    issues[path] = f"size mismatch: expected {expected['size']}, remote {actual[0]}"
                else:
                    digest = actual[1]
                    if digest is None:
                        log(
                            f"[upload verification] No remote SHA-256; downloading to verify: {path}"
                        )
                        try:
                            downloaded = publisher._remote_read_with_retries(
                                lambda path=path: api.download_file(
                                    repository, "dataset", path, revision=revision, force=True
                                )
                            )
                            digest = file_digest(Path(downloaded))
                        except (HubError, OSError, ValueError, RuntimeError) as exc:
                            issues[path] = publisher._redacted_error(exc, token)
                    if path not in issues and digest != expected["sha256"]:
                        issues[path] = "SHA-256 mismatch"
                log(
                    f"[upload verification {number}/{len(inventory)}] {path}: {issues.get(path, 'OK')}"
                )
            remote_entries = []
            if check_metadata:
                document = publisher._download_remote_metadata(api, repository, revision)
                remote_entries = publisher._read_metadata_document(document, "remote metadata")
            for entry in entries if check_metadata else ():
                matches = [
                    row
                    for row in remote_entries
                    if row.get("session_path") == entry["session_path"]
                ]
                if len(matches) != 1 or any(
                    matches[0].get(key) != value for key, value in entry.items()
                ):
                    issues[publisher.METADATA_NAME] = (
                        "uploaded Session metadata missing, duplicated or different"
                    )
                    break
        except Exception as exc:
            # A failed audit is not evidence that any particular file needs overwriting.
            report["status"] = "verification_failed"
            report["error"] = publisher._redacted_error(exc, token)
            save()
            raise UploadVerificationError(
                f"Upload verification could not complete: {report['error']}; report: {report_path}"
            ) from exc

        record = {"attempt": attempt, "issues": issues}
        if pending_upload_error:
            record["repair_error"] = pending_upload_error
            pending_upload_error = None
        report["rounds"].append(record)
        report["status"] = ("verified" if check_metadata else "files_verified") if not issues else "repair_required"
        save()
        if not issues:
            label = "Upload verified" if check_metadata else "Files verified; metadata publication pending"
            log(
                f"{label}: {len(entries)} Session(s), {len(inventory)} files; report: {report_path}"
            )
            return
        unrepairable = [path for path in issues if inventory.get(path, {}).get("remote_only")]
        if unrepairable:
            report["status"] = "remote_only_changed"
            save()
            raise UploadVerificationError(
                "Retained remote-only files changed; no local copy to repair: "
                + ", ".join(unrepairable) + f"; report: {report_path}"
            )
        if attempt == REPAIR_RETRIES:
            break
        failed_paths = sorted(issues)
        log(
            f"[upload repair {attempt + 1}/{REPAIR_RETRIES}] Reuploading {len(failed_paths)} failed file(s): "
            + ", ".join(failed_paths)
        )
        for path in failed_paths:
            if (
                path in inventory
                and file_digest(staged.dataset_root / path) != inventory[path]["sha256"]
            ):
                report["status"] = "source_changed"
                save()
                raise UploadVerificationError(
                    f"Staged source changed; refusing repair: {path}; report: {report_path}"
                )
        try:
            if publisher.METADATA_NAME in issues:
                if publish_metadata is None:
                    raise RuntimeError("Metadata repair requires a conflict-safe publisher.")
                publish_metadata()
                failed_paths.remove(publisher.METADATA_NAME)
            if not failed_paths:
                continue
            api.upload_folder(
                repository,
                "dataset",
                staged.dataset_root,
                path_in_repo="",
                revision=revision,
                commit_message=f"Repair uploaded files ({attempt + 1}/{REPAIR_RETRIES})",
                allow_patterns=[glob.escape(path) for path in failed_paths],
                max_workers=max_workers,
                use_cache=False,
                disable_tqdm=False,
            )
        except (HubError, OSError, ValueError, RuntimeError) as exc:
            pending_upload_error = publisher._redacted_error(exc, token)
            log(f"[upload repair] {pending_upload_error}")
    report["status"] = "failed"
    save()
    raise UploadVerificationError(
        f"Upload verification failed after {REPAIR_RETRIES} repair attempts: "
        + "; ".join(f"{path}: {reason}" for path, reason in issues.items())
        + f"; report: {report_path}"
    )
