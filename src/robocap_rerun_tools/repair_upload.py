"""Locate original recordings and replace files identified by a corruption CSV."""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import math
import os
import shutil
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .modelscope_publisher import (
    _download_remote_metadata,
    _hub_api,
    _metadata_document,
    _redacted_error,
    _remote_file_fields,
    load_modelscope_settings,
    merge_metadata_entries,
    validate_repo_id,
    validate_upload_date,
)
from .participant_metadata import enrich_record, load_catalog

SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "_artifacts",
    "_modelscope_dataset",
    "$recycle.bin",
    "system volume information",
    "windows",
}
SUPPORTED = {"video_missing", "video_unreadable", "mocap_csv_missing", "mocap_trc_unparseable"}


def safe_relative(value: str) -> str:
    value = value.replace("\\", "/")
    if not value or ":" in value or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ValueError(f"Unsafe relative path: {value!r}")
    if PurePosixPath(value).is_absolute():
        raise ValueError(f"Absolute path is not allowed: {value!r}")
    return value


def read_requests(path: Path) -> list[dict]:
    grouped = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            destination = safe_relative(row["session_path"].strip())
            parts = destination.split("/")
            if (
                len(parts) != 4
                or parts[0] != "EgoMotionActions"
                or parts[-1] != row["session"].strip()
            ):
                raise ValueError(f"Unexpected Session destination: {destination}")
            validate_upload_date(parts[1])
            if row["error_type"] not in SUPPORTED:
                raise ValueError(f"Unsupported error type: {row['error_type']}")
            files = [safe_relative(p.strip()) for p in row["bad_file"].split(";") if p.strip()]
            if not files or any(
                PurePosixPath(p).suffix.lower() not in {".mp4", ".trc", ".csv"} for p in files
            ):
                raise ValueError(f"Unsupported repair files: {files}")
            item = grouped.setdefault(
                destination,
                {
                    "session_path": destination,
                    "session": parts[-1],
                    "bad_files": [],
                    "error_types": [],
                },
            )
            item["bad_files"] = sorted(set(item["bad_files"]) | set(files))
            item["error_types"] = sorted(set(item["error_types"]) | {row["error_type"]})
    if not grouped:
        raise ValueError("The corruption CSV contains no requests")
    return list(grouped.values())


def search_sessions(roots: list[Path], names: set[str], log=print) -> dict[str, list[Path]]:
    found = defaultdict(list)
    seen = set()
    count = 0
    for root in roots:
        if not root.is_dir():
            log(f"ROOT UNAVAILABLE: {root}")
            continue
        log(f"SCAN ROOT: {root}")
        for directory, dirs, _ in os.walk(
            root, followlinks=False, onerror=lambda e: log(f"SCAN ERROR: {e}")
        ):
            current = Path(directory)
            identity = os.path.normcase(str(current.resolve()))
            if identity in seen:
                dirs[:] = []
                continue
            seen.add(identity)
            count += 1
            if current.name in names:
                found[current.name].append(current)
                log(f"FOUND {current.name}: {current}")
                dirs[:] = []
            else:
                dirs[:] = [
                    d
                    for d in dirs
                    if d.casefold() not in SKIP_DIRS
                    and not (current / d).is_symlink()
                    and not getattr(os.path, "isjunction", lambda _: False)(current / d)
                ]
            if count % 500 == 0:
                log(f"Scanned {count} directories; matched {len(found)}/{len(names)} Session names")
    return dict(found)


def locate_file(session: Path, requested: str) -> Path:
    relative = PurePosixPath(requested)
    if relative.parts[0].casefold().startswith("mocap"):
        roots = [
            p for p in session.iterdir() if p.is_dir() and p.name.casefold().startswith("mocap")
        ]
        matches = [p for root in roots for p in root.rglob(relative.name) if p.is_file()]
    else:
        matches = [session / requested] if (session / requested).is_file() else []
    matches = [p for p in matches if p.resolve().is_relative_to(session.resolve())]
    if len(matches) != 1:
        raise ValueError(f"{requested}: expected one local file, found {len(matches)}")
    if matches[0].stat().st_size == 0:
        raise ValueError(f"Empty local file: {matches[0]}")
    return matches[0]


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_trc(path: Path) -> None:
    with path.open(encoding="utf-8-sig", errors="strict") as stream:
        lines = iter(enumerate(stream, 1))
        header = None
        declared_frames = None
        metadata_columns = None
        for _, line in lines:
            cells = line.split()
            if "NumFrames" in cells:
                metadata_columns = cells
            elif metadata_columns is not None and cells:
                declared_frames = int(cells[metadata_columns.index("NumFrames")])
                metadata_columns = None
            if line.startswith("Frame#"):
                header = line.rstrip("\r\n").split("\t")
                break
        if header is None:
            raise ValueError("TRC has no Frame# header")
        offset = (
            3
            if len(header) > 2 and any(k in header[2].casefold() for k in ("time", "stamp"))
            else 2
        )
        markers = sum(bool(cell.strip()) for cell in header[offset:])
        if not markers:
            raise ValueError("TRC has no marker columns")
        width = offset + 3 * markers
        frames = valid = 0
        for number, line in lines:
            if not line.strip():
                continue
            cells = line.rstrip("\r\n").split("\t")
            if not cells[0].strip().isdigit():
                if frames:
                    raise ValueError(f"TRC unexpected text at line {number}")
                continue
            while len(cells) > width and not cells[-1].strip():
                cells.pop()
            if len(cells) != width:
                raise ValueError(f"TRC line {number}: expected {width} columns, found {len(cells)}")
            float(cells[1])
            for cell in cells[offset:]:
                if cell.strip():
                    valid += math.isfinite(float(cell))
            frames += 1
        if not frames or not valid:
            raise ValueError("TRC has no usable coordinate data")
        if declared_frames is not None and frames != declared_frames:
            raise ValueError(
                f"TRC truncated/count mismatch: header {declared_frames}, rows {frames}"
            )


def validate_file(path: Path, ffmpeg: str, ffprobe: str, log_path: Path) -> None:
    if path.suffix.lower() == ".mp4":
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=True,
        )
        if not json.loads(probe.stdout).get("streams"):
            raise ValueError(f"No video stream: {path}")
        with log_path.open("ab") as stream:
            proc = subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-err_detect",
                    "explode",
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-f",
                    "null",
                    "-",
                ],
                stdout=stream,
                stderr=stream,
                check=False,
            )
        if proc.returncode:
            raise ValueError(f"Full video decode failed: {path}; see {log_path}")
    elif path.suffix.lower() == ".trc":
        validate_trc(path)
    else:
        import numpy as np

        from .exporter import parse_nokov_csv

        times, names, positions, _ = parse_nokov_csv(path, 1.0, None)
        if not len(times) or not names or not np.isfinite(positions).any():
            raise ValueError(f"No usable CSV motion data: {path}")


def remote_file_path(requested: str, records: dict) -> str:
    if requested in records:
        return requested
    name = PurePosixPath(requested).name
    matches = [p for p in records if PurePosixPath(p).name == name]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError(f"Ambiguous remote file: {requested}")
    if PurePosixPath(requested).parts[0].casefold().startswith("mocap"):
        parents = {
            str(PurePosixPath(p).parent)
            for p in records
            if PurePosixPath(p).parts[0].casefold().startswith("mocap")
            and PurePosixPath(p).suffix.lower() in {".csv", ".trc", ".bvh", ".mp4"}
        }
        if len(parents) != 1:
            raise ValueError(f"Cannot determine remote Mocap directory for {requested}")
        return safe_relative(next(iter(parents)) + "/" + name)
    return requested


def repair_one(api, settings, request, source, files, output, log):
    destination = request["session_path"]
    manifest_remote = destination + "/manifest.json"
    downloaded = api.download_file(
        settings.repo_id, "dataset", manifest_remote, revision="master", force=True
    )
    manifest = json.loads(Path(downloaded).read_text(encoding="utf-8"))
    if (
        manifest.get("dataset_path") != destination
        or manifest.get("session_id") != request["session"]
    ):
        raise ValueError("Remote manifest identity does not match requested destination")
    records = {safe_relative(r["packaged_as"]): dict(r) for r in manifest["files"]}
    stage = output / "staged" / request["session"]
    replacements = {}
    for requested, local in files.items():
        remote = remote_file_path(requested, records)
        packaged = stage / destination / remote
        packaged.parent.mkdir(parents=True, exist_ok=True)
        expected_hash = sha256(local)
        shutil.copyfile(local, packaged)
        if sha256(packaged) != expected_hash:
            raise ValueError(f"Source changed during copy: {local}")
        size = packaged.stat().st_size
        record = records.get(remote, {})
        record.update(
            source=local.relative_to(source).as_posix(),
            packaged_as=remote,
            original_bytes=size,
            packaged_bytes=size,
            compressed_video=False,
            kind="video_raw" if local.suffix.lower() == ".mp4" else "data",
            sha256=expected_hash,
        )
        records[remote] = record
        replacements[remote] = {"bytes": size, "sha256": expected_hash}
    manifest["files"] = list(records.values())
    manifest["repair"] = {"at": datetime.now().astimezone().isoformat(), "files": replacements}
    for attempt in range(4):
        try:
            document = _download_remote_metadata(api, settings.repo_id, "master")
            entries = [json.loads(line) for line in document.splitlines() if line.strip()]
            matches = [e for e in entries if e.get("session_path") == destination]
            conflicts = [
                e
                for e in entries
                if e.get("session_id") == request["session"]
                and e.get("primitive_id") == manifest["primitive_id"]
                and e.get("session_path") != destination
            ]
            if len(matches) > 1 or conflicts:
                raise ValueError("Duplicate/conflicting Session identity in remote metadata")
            entry = (
                dict(matches[0])
                if matches
                else {
                    k: manifest[k]
                    for k in (
                        "primitive_id",
                        "session_id",
                        "upload_batch_id",
                        "segment",
                        "duration_s",
                        "device_ids",
                    )
                }
            )
            entry.update(
                session_path=destination,
                manifest=manifest_remote,
                inspection_html=destination + "/timestamp_anomaly_detail_table.html",
                file_count=len(records),
                packaged_bytes=sum(r["packaged_bytes"] for r in records.values()),
            )
            for key in ("mocap_capture", "quality_status", "error_types", "quality_details"):
                if key in manifest:
                    entry[key] = manifest[key]
            catalog = load_catalog(api, settings.repo_id, "master", progress=log)
            enriched = enrich_record(entry, catalog, entry)
            manifest.update({k: v for k, v in enriched.items() if k.startswith("participant_")})
            manifest_local = stage / manifest_remote
            manifest_local.parent.mkdir(parents=True, exist_ok=True)
            manifest_local.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (stage / "metadata.jsonl").write_text(
                _metadata_document(merge_metadata_entries(entries, [enriched])), encoding="utf-8"
            )
            allow = [destination + "/" + p for p in replacements] + [
                manifest_remote,
                "metadata.jsonl",
            ]
            log(
                f"UPLOAD {destination}: {len(replacements)} replacement files, attempt {attempt + 1}/4"
            )
            api.upload_folder(
                settings.repo_id,
                "dataset",
                stage,
                revision="master",
                allow_patterns=[glob.escape(p) for p in allow],
                use_cache=False,
                sync_remote_repo=False,
                commit_message=f"Repair listed corrupt files in {request['session']}",
            )
            actual = {
                _remote_file_fields(item)[0]: _remote_file_fields(item)[2]
                for item in api.legacy.list_dataset_files_paginated(
                    settings.repo_id, root_path=destination
                )
            }
            if any(
                actual.get(destination + "/" + p) != info["bytes"]
                for p, info in replacements.items()
            ):
                raise ValueError("Remote verification failed: repaired file size mismatch")
            saved = json.loads(
                Path(
                    api.download_file(
                        settings.repo_id, "dataset", manifest_remote, revision="master", force=True
                    )
                ).read_text(encoding="utf-8")
            )
            if saved.get("repair") != manifest["repair"]:
                raise ValueError("Remote manifest verification failed")
            latest = [
                json.loads(line)
                for line in _download_remote_metadata(api, settings.repo_id, "master").splitlines()
                if line.strip()
            ]
            if enriched not in latest:
                raise ValueError("Remote metadata verification failed")
            return replacements
        except Exception as exc:
            log(f"ATTEMPT FAILED: {_redacted_error(exc, settings.token)}")
            if attempt == 3:
                raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--root", type=Path, action="append", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_artifacts/repair_upload")
        / datetime.now(UTC).astimezone().strftime("%Y%m%d_%H%M%S"),
    )
    parser.add_argument("--repo-id")
    parser.add_argument(
        "--apply", action="store_true", help="Validate source files and upload replacements"
    )
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)

    def log(message):
        print(message, flush=True)
        with (args.output / "run.log").open("a", encoding="utf-8") as stream:
            stream.write(str(message) + "\n")

    requests = read_requests(args.csv)
    log(f"Requests: {len(requests)} Sessions; apply={args.apply}")
    candidates = search_sessions(args.root, {r["session"] for r in requests}, log)
    settings = load_modelscope_settings()
    if args.repo_id:
        from dataclasses import replace

        settings = replace(settings, repo_id=validate_repo_id(args.repo_id))
    api = _hub_api(settings) if args.apply else None
    if args.apply and (not settings.repo_id or not settings.token):
        raise ValueError("Configure MODELSCOPE_REPO_ID and MODELSCOPE_API_TOKEN in .env")
    if args.apply:
        from .cli import resolve_ffprobe
        from .frame_comparison import _resolve_ffmpeg

        ffmpeg = _resolve_ffmpeg(None)
        ffprobe = resolve_ffprobe("ffprobe", ffmpeg)
    results = []
    for number, request in enumerate(requests, 1):
        result = dict(
            request,
            status="not_found",
            candidates=[str(p) for p in candidates.get(request["session"], [])],
            detail=[],
        )
        log(f"[{number}/{len(requests)}] {request['session_path']}")
        valid = []
        for source in candidates.get(request["session"], []):
            try:
                files = {p: locate_file(source, p) for p in request["bad_files"]}
                if args.apply:
                    for local in files.values():
                        log(f"VALIDATE {local}")
                        validate_file(
                            local,
                            ffmpeg,
                            ffprobe,
                            args.output / (request["session"] + "_validation.log"),
                        )
                valid.append((source, files))
            except Exception as exc:  # noqa: BLE001 - keep other source copies eligible
                result["detail"].append(f"{source}: {_redacted_error(exc, settings.token)}")
        try:
            if valid:
                if len(valid) > 1:
                    signatures = {
                        tuple((k, sha256(v)) for k, v in sorted(files.items()))
                        for _, files in valid
                    }
                    if len(signatures) > 1:
                        raise ValueError(
                            "Multiple valid sources differ: choose one search root explicitly"
                        )
                source, files = valid[0]
                result.update(source=str(source), status="ready_not_uploaded")
                if args.apply:
                    result["replacements"] = repair_one(
                        api, settings, request, source, files, args.output, log
                    )
                    result["status"] = "uploaded_verified"
            elif result["candidates"]:
                result["status"] = "source_invalid"
        except Exception as exc:  # noqa: BLE001 - isolate a failed Session from the repair batch
            result.update(status="failed")
            result["detail"].append(_redacted_error(exc, settings.token))
        results.append(result)
        log(json.dumps(result, ensure_ascii=False))
        (args.output / "results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with (args.output / "results.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=["session_path", "status", "source", "detail"]
            )
            writer.writeheader()
            for r in results:
                writer.writerow({k: r.get(k, "") for k in writer.fieldnames})
    log("FINISHED; results: " + str(args.output.resolve()))
    return int(any(r["status"] not in {"ready_not_uploaded", "uploaded_verified"} for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
