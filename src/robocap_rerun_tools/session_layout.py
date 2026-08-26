from __future__ import annotations

from pathlib import Path

CANONICAL_MOCAP_DIR_NAME = "mocap"
MOCAP_DIR_PREFIX = "mocap"


def is_mocap_directory_name(name: str) -> bool:
    return name.casefold().startswith(MOCAP_DIR_PREFIX)


def discover_mocap_directories(session_dir: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                child
                for child in session_dir.iterdir()
                if child.is_dir() and is_mocap_directory_name(child.name)
            ),
            key=lambda path: path.name.casefold(),
        )
    )


def is_path_under_mocap(path: Path, session_dir: Path) -> bool:
    try:
        relative = path.relative_to(session_dir)
    except ValueError:
        return False
    return len(relative.parts) > 1 and is_mocap_directory_name(relative.parts[0])


def canonical_mocap_relative_path(path: Path, session_dir: Path) -> Path:
    relative = path.relative_to(session_dir)
    if len(relative.parts) <= 1 or not is_mocap_directory_name(relative.parts[0]):
        return relative
    return Path(CANONICAL_MOCAP_DIR_NAME, *relative.parts[1:])
