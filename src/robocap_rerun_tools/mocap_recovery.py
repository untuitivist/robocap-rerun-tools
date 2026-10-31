from __future__ import annotations

import os
import re
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from .session_layout import discover_mocap_directories, is_mocap_directory_name

SESSION_TIMESTAMP_PATTERN = re.compile(r"(?<!\d)(\d{8}_\d{6})(?!\d)")
SESSION_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
CREATION_TIMEZONE = timezone(timedelta(hours=8), name="UTC+08:00")
MOTION_SUFFIXES = frozenset({".trc", ".bvh", ".csv"})
RECOVERY_EXPORT_SUFFIXES = MOTION_SUFFIXES | {".mp4"}
TIMESTAMP_REPORT_NAME = "timestamp_anomaly_detail_table.html"
RecoveryProgress = Callable[[str, int, int | None, str], None]


@dataclass(frozen=True)
class MocapCandidate:
    path: Path
    created_at: datetime


@dataclass(frozen=True)
class RecoveryTarget:
    session_dir: Path
    session_timestamp: datetime
    existing_directories: tuple[Path, ...]
    report_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class RecoveryMatch:
    target: RecoveryTarget
    candidate: MocapCandidate
    delta_seconds: float


@dataclass(frozen=True)
class RecoveryPlan:
    matches: tuple[RecoveryMatch, ...]
    skipped_same_name: tuple[RecoveryMatch, ...]
    candidates_without_motion: tuple[Path, ...]
    unmatched_candidates: tuple[MocapCandidate, ...]
    unmatched_sessions: tuple[Path, ...]
    unparseable_sessions: tuple[Path, ...]


def contains_motion_file(directory: Path) -> bool:
    return any(
        path.is_file() and path.suffix.casefold() in MOTION_SUFFIXES
        for path in directory.rglob("*")
    )


def discover_timestamp_reports(session_dir: Path) -> tuple[Path, ...]:
    artifacts = session_dir / "_artifacts"
    if not artifacts.is_dir():
        return ()
    return tuple(
        sorted(
            (path for path in artifacts.rglob(TIMESTAMP_REPORT_NAME) if path.is_file()),
            key=lambda path: str(path).casefold(),
        )
    )


def parse_session_timestamp(session_dir: Path) -> datetime | None:
    match = SESSION_TIMESTAMP_PATTERN.search(session_dir.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), SESSION_TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def candidate_creation_time(directory: Path) -> datetime:
    stat = directory.stat()
    timestamp = getattr(stat, "st_birthtime", stat.st_ctime)
    return datetime.fromtimestamp(timestamp, UTC).astimezone(CREATION_TIMEZONE)


def session_has_frame_count_difference(session_dir: Path) -> bool:
    from .dataset_statistics import (
        classify_frame_count_anomalies,
        discover_segment_references,
        load_report_payload,
    )

    for reference in discover_segment_references(session_dir):
        if not reference.report_path.is_file():
            continue
        try:
            anomalies = classify_frame_count_anomalies(load_report_payload(reference.report_path))
        except (OSError, UnicodeError, ValueError):
            continue
        if anomalies:
            return True
    return False


def discover_candidate_directories(
    root: Path,
    progress: RecoveryProgress | None = None,
) -> tuple[tuple[MocapCandidate, ...], tuple[Path, ...]]:
    candidates: list[MocapCandidate] = []
    without_motion: list[Path] = []
    for scanned, (current, directory_names, _) in enumerate(os.walk(root), start=1):
        current_path = Path(current)
        if progress is not None:
            progress("candidate_scan", scanned, None, str(current_path))
        mocap_names = [name for name in directory_names if is_mocap_directory_name(name)]
        directory_names[:] = [name for name in directory_names if name not in mocap_names]
        for name in mocap_names:
            path = (current_path / name).resolve()
            if contains_motion_file(path):
                candidates.append(MocapCandidate(path, candidate_creation_time(path)))
                status = "usable"
            else:
                without_motion.append(path)
                status = "no_motion"
            if progress is not None:
                progress("candidate", len(candidates) + len(without_motion), None, f"{status}|{path}")
    return (
        tuple(sorted(candidates, key=lambda item: (item.created_at, str(item.path).casefold()))),
        tuple(sorted(without_motion, key=lambda path: str(path).casefold())),
    )


def _roots_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def build_recovery_plan(
    dataset_root: Path,
    source_root: Path,
    sessions: list[Path],
    progress: RecoveryProgress | None = None,
) -> RecoveryPlan:
    dataset_root = dataset_root.expanduser().resolve()
    source_root = source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise ValueError(f"Mocap source root is not a directory: {source_root}")
    if _roots_overlap(dataset_root, source_root):
        raise ValueError("Mocap source root must not overlap the dataset root.")

    targets: list[RecoveryTarget] = []
    unparseable: list[Path] = []
    for index, session in enumerate(sessions, start=1):
        mocap_directories = discover_mocap_directories(session)
        has_motion = any(contains_motion_file(path) for path in mocap_directories)
        if (
            len(mocap_directories) == 1
            and has_motion
            and not session_has_frame_count_difference(session)
        ):
            if progress is not None:
                progress("session", index, len(sessions), f"clean|{session.name}")
            continue
        timestamp = parse_session_timestamp(session)
        if timestamp is None:
            unparseable.append(session)
            if progress is not None:
                progress("session", index, len(sessions), f"unparseable|{session.name}")
            continue
        targets.append(
            RecoveryTarget(
                session_dir=session,
                session_timestamp=timestamp,
                existing_directories=tuple(mocap_directories),
                report_paths=discover_timestamp_reports(session),
            )
        )
        if progress is not None:
            progress("session", index, len(sessions), f"target|{session.name}")

    candidates, without_motion = discover_candidate_directories(source_root, progress)
    if progress is not None:
        progress(
            "matching",
            0,
            len(targets) * len(candidates),
            f"targets={len(targets)}|candidates={len(candidates)}",
        )
    available_targets = set(range(len(targets)))
    available_candidates = set(range(len(candidates)))
    matched_pairs: list[RecoveryMatch] = []
    pairs = sorted(
        (
            abs((target.session_timestamp - candidate.created_at).total_seconds()),
            target_index,
            candidate_index,
        )
        for target_index, target in enumerate(targets)
        for candidate_index, candidate in enumerate(candidates)
    )
    for delta_seconds, target_index, candidate_index in pairs:
        if target_index not in available_targets or candidate_index not in available_candidates:
            continue
        matched_pairs.append(
            RecoveryMatch(targets[target_index], candidates[candidate_index], delta_seconds)
        )
        available_targets.remove(target_index)
        available_candidates.remove(candidate_index)
    if progress is not None:
        progress("matching", len(pairs), len(pairs), f"matched={len(matched_pairs)}")

    matches: list[RecoveryMatch] = []
    skipped_same_name: list[RecoveryMatch] = []
    matches.extend(matched_pairs)

    return RecoveryPlan(
        matches=tuple(sorted(matches, key=lambda item: item.target.session_timestamp)),
        skipped_same_name=tuple(
            sorted(skipped_same_name, key=lambda item: item.target.session_timestamp)
        ),
        candidates_without_motion=without_motion,
        unmatched_candidates=tuple(candidates[index] for index in sorted(available_candidates)),
        unmatched_sessions=tuple(targets[index].session_dir for index in sorted(available_targets)),
        unparseable_sessions=tuple(sorted(unparseable, key=lambda path: str(path).casefold())),
    )


def destination_for_match(match: RecoveryMatch) -> Path:
    return match.target.session_dir / match.candidate.path.name


def copy_conflicts(match: RecoveryMatch) -> tuple[Path, ...]:
    destination = destination_for_match(match)
    return (destination,) if destination.exists() and not destination.is_dir() else ()


def _move_candidate_exports(
    source: Path,
    destination: Path,
    progress: RecoveryProgress | None,
    moved: list[tuple[Path, Path]],
    backups: list[tuple[Path, Path]],
    attempted: list[Path],
) -> None:
    files = sorted(
        (path for path in source.rglob("*")
         if path.is_file() and path.suffix.casefold() in RECOVERY_EXPORT_SUFFIXES),
        key=lambda path: str(path).casefold(),
    )
    if not any(path.suffix.casefold() in MOTION_SUFFIXES for path in files):
        raise ValueError(f"No TRC/BVH/CSV exports remain in {source}")
    if any(path.is_symlink() for path in files):
        raise ValueError(f"Refusing to move linked exports from {source}")
    total_bytes = sum(path.stat().st_size for path in files)
    copied_bytes = 0
    if progress is not None:
        progress("move_bytes", 0, total_bytes, str(source))

    def copy_file(source_file: str, destination_file: str) -> str:
        nonlocal copied_bytes
        source_path = Path(source_file)
        destination_path = Path(destination_file)
        with source_path.open("rb") as reader, destination_path.open("wb") as writer:
            while chunk := reader.read(4 * 1024 * 1024):
                writer.write(chunk)
                copied_bytes += len(chunk)
                if progress is not None:
                    progress(
                        "move_bytes", copied_bytes, total_bytes,
                        str(source_path.relative_to(source)),
                    )
        shutil.copystat(source_path, destination_path)
        return str(destination_path)

    destination.mkdir(exist_ok=True)
    for path in files:
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise FileExistsError(f"Refusing to overwrite non-regular file: {target}")
            backup = target.with_name(f".{target.name}.robocap-recovery-backup-{uuid.uuid4().hex}")
            target.rename(backup)
            backups.append((target, backup))
        size = path.stat().st_size
        before = copied_bytes
        attempted.append(target)
        if path.suffix.casefold() == ".mp4":
            copy_file(str(path), str(target))
        else:
            shutil.move(str(path), str(target), copy_function=copy_file)
            moved.append((path, target))
        copied_bytes = before + size
        if progress is not None:
            progress("move_bytes", copied_bytes, total_bytes, str(path.relative_to(source)))


def move_recovery_match(
    match: RecoveryMatch,
    progress: RecoveryProgress | None = None,
) -> Path:
    conflicts = copy_conflicts(match)
    if conflicts:
        raise FileExistsError(f"Refusing to overwrite existing paths: {list(conflicts)}")
    destination = destination_for_match(match)
    backups: list[tuple[Path, Path]] = []
    report_backups: list[tuple[Path, Path]] = []
    moved: list[tuple[Path, Path]] = []
    attempted: list[Path] = []
    try:
        for report in match.target.report_paths:
            backup = report.parent / f".{report.name}.robocap-recovery-backup-{uuid.uuid4().hex}"
            report.rename(backup)
            report_backups.append((report, backup))
        _move_candidate_exports(match.candidate.path, destination, progress, moved, backups, attempted)
    except BaseException:
        # Restore moved exports before removing the partial destination. If restoring
        # fails, leave both destination and backups intact for manual recovery.
        for source_file, destination_file in reversed(moved):
            shutil.move(str(destination_file), str(source_file))
        for target in reversed(attempted):
            if target.exists():
                target.unlink()
        for report, backup in reversed(report_backups):
            backup.rename(report)
        for target, backup in reversed(backups):
            backup.rename(target)
        raise
    for _, backup in report_backups:
        backup.unlink()
    for _, backup in backups:
        backup.unlink()
    return destination
