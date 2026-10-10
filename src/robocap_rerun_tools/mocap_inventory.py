"""Shared, actual exported entity names and source-to-package file inventory."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .obj_inventory import EXTENSIONS, filename_parts, name_category


def mocap_names(paths: Iterable[Path]) -> list[str]:
    """Keep source spelling, deduplicate formats/case, exclude unnamed tracks/videos."""
    names = {}
    for path in sorted(paths, key=lambda value: value.as_posix()):
        if path.suffix.lower() not in EXTENSIONS:
            continue
        name, _ = filename_parts(path)
        if name_category(name) != "unnamed":
            names.setdefault(name.casefold(), name)
    return sorted(names.values(), key=str.casefold)


def manifest_mocap_names(records) -> list[str]:
    # Prefer the original source path: legacy capture directory prefixes can only
    # be parsed reliably before their canonical rename to mocap/.
    return mocap_names(
        Path(record.get("source") or record["packaged_as"])
        for record in records
        if "/" in record["packaged_as"]
        and record["packaged_as"].partition("/")[0].casefold().startswith("mocap")
    )
