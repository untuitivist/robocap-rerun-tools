"""Copy shared documentation/calibrations, never recording Sessions or their index."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath

from robocap_rerun_tools.fault_publishing import fault_readme, fault_settings
from robocap_rerun_tools.modelscope_publisher import (
    _hub_api,
    _list_remote_session_files,
    _remote_file_fields,
    load_modelscope_settings,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    def emit(message):
        print(message, flush=True)
        with (args.output / "setup.log").open("a", encoding="utf-8") as log:
            log.write(message + "\n")

    normal = load_modelscope_settings()
    fault = fault_settings()
    api = _hub_api(fault)
    source, target = normal.repo_id, fault.repo_id
    if not source or source == target:
        raise ValueError("Distinct source and destination repositories are required")
    if not api.repo_exists(target, "dataset"):
        raise ValueError(f"Destination repository does not exist: {target}")
    top = [
        _remote_file_fields(item)
        for item in api.list_repo_files(source, "dataset", recursive=False)
    ]
    emit(json.dumps({"source": source, "target": target, "root_files": top}, ensure_ascii=False))
    shared = _list_remote_session_files(api, source, "master", "raw_calibration")
    for path, kind, size in top:
        if (
            kind != "tree"
            and path != "README.md"
            and Path(path).suffix.lower() in {".xlsx", ".html", ".pdf"}
        ):
            shared[path] = size
    shared["README.md"] = 0
    emit("Shared files: " + json.dumps(sorted(shared), ensure_ascii=False))
    if not args.apply:
        return
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for index, relative in enumerate(sorted(shared), 1):
        parts = PurePosixPath(relative).parts
        if ".." in parts or PurePosixPath(relative).is_absolute():
            raise ValueError(f"Unsafe remote path: {relative}")
        emit(f"[{index}/{len(shared)}] Download {relative}")
        downloaded = api.download_file(source, "dataset", relative, revision="master", force=True)
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == "README.md":
            destination.write_text(
                fault_readme(Path(downloaded).read_text(encoding="utf-8"), target), encoding="utf-8"
            )
        else:
            shutil.copyfile(downloaded, destination)
        hashes[relative] = hashlib.sha256(destination.read_bytes()).hexdigest()
    emit(f"Upload shared materials to {target}")
    api.upload_folder(
        target,
        "dataset",
        root,
        allow_patterns=sorted(shared),
        revision="master",
        commit_message="Document fault recordings and add shared collection assets",
        sync_remote_repo=False,
    )
    for relative, digest in hashes.items():
        downloaded = api.download_file(target, "dataset", relative, revision="master", force=True)
        if hashlib.sha256(Path(downloaded).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Remote verification failed: {relative}")
        emit(f"Verified {relative}")
    report = {"source": source, "target": target, "verified_sha256": hashes}
    (root / "initialization_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    emit("Shared materials uploaded and verified; no Session data or metadata index copied.")


if __name__ == "__main__":
    main()
