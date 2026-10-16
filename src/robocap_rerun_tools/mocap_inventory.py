"""Shared, actual exported entity names and source-to-package file inventory."""

from __future__ import annotations

import re
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


SCAN_CATEGORIES = (
    "ArmlessChair", "BigBox", "Box", "CcBlue", "CcGreen", "CcOrange", "CcRed",
    "CcYellow", "Cushion", "HighStep", "LowStep", "OpenBox", "PlatformTrolly",
    "Ramp", "StrongeBox", "Tray",
)


def object_category_name(name: str) -> str:
    """Normalize scan categories, dropping only the trailing instance number."""
    match = re.fullmatch(r"([A-Za-z][A-Za-z _-]*?)(\d*)", name.strip())
    if match is None:
        raise ValueError(f"Invalid object instance name: {name!r}")
    words = re.split(r"[ _-]+", match[1].strip())
    category = "".join(word[0].upper() + word[1:] for word in words)
    category = next((known for known in SCAN_CATEGORIES if known.casefold() == category.casefold()), category)
    return category


def object_names(names: Iterable[str]) -> list[str]:
    """Publish prop instances, excluding people, trackers and unnamed tracks."""
    return sorted(
        {object_instance_name(name) for name in names if name_category(name.strip()) == "object_candidate"},
        key=str.casefold,
    )


def object_instance_name(name: str) -> str:
    """Retain explicit instance suffixes; use zero for legacy unnumbered names."""
    category = object_category_name(name)
    suffix = re.search(r"\d+$", name.strip())
    return category + (suffix[0] if suffix else "0")
