from __future__ import annotations

import argparse
import mmap
import re
import struct
import zipfile
import zlib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

OLD_DIR_NAME = "nokov"
MOCAP_DIR_NAME = "mocap"
SESSION_NAME_PATTERN = re.compile(r"(?:^|_)session\d+\Z", re.IGNORECASE)
GENERATED_TEXT_SUFFIXES = {".html", ".js", ".json", ".jsonl", ".md", ".tsv", ".txt"}
GENERATED_ROOT_NAMES = {"_analysis", "_artifacts", "_modelscope_dataset"}
PATH_SEPARATORS = b"/\\"
RELATIVE_PREFIXES = b"\"'`\t\r\n ([{:=,>"
TERMINATORS = b"\"'`\t\r\n )]},;"


def is_session_dir(path: Path) -> bool:
    return SESSION_NAME_PATTERN.search(path.name) is not None


def discover_old_session_dirs(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob(OLD_DIR_NAME) if path.is_dir() and is_session_dir(path.parent)
    )


def validate_rename(old_path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved_old = old_path.resolve()
    if resolved_old == resolved_root or resolved_root not in resolved_old.parents:
        raise ValueError(f"Refusing to rename a directory outside the requested root: {old_path}")
    if old_path.name.lower() != OLD_DIR_NAME or not is_session_dir(old_path.parent):
        raise ValueError(f"Not a session-level {OLD_DIR_NAME}/ directory: {old_path}")
    target = old_path.with_name(MOCAP_DIR_NAME)
    if target.exists():
        raise FileExistsError(f"Target directory already exists: {target}")
    return target


def rename_session_dirs(paths: Iterable[Path], root: Path) -> list[tuple[Path, Path]]:
    renamed: list[tuple[Path, Path]] = []
    for old_path in paths:
        target = validate_rename(old_path, root)
        old_path.rename(target)
        renamed.append((old_path, target))
    return renamed


def generated_text_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in GENERATED_TEXT_SUFFIXES
        and any(part.lower() in GENERATED_ROOT_NAMES for part in path.relative_to(root).parts)
    )


def previous_path_component(buffer: mmap.mmap, position: int) -> bytes:
    cursor = position - 1
    while cursor >= 0 and buffer[cursor] in PATH_SEPARATORS:
        cursor -= 1
    end = cursor + 1
    while cursor >= 0 and buffer[cursor] not in PATH_SEPARATORS + b"\r\n\t\"'`":
        cursor -= 1
    return bytes(buffer[cursor + 1 : end])


def is_directory_reference(buffer: mmap.mmap, position: int) -> bool:
    after_position = position + len(OLD_DIR_NAME)
    previous = buffer[position - 1] if position else None
    following = buffer[after_position] if after_position < len(buffer) else None
    if following is not None and following not in PATH_SEPARATORS + TERMINATORS:
        return False
    if previous is None or previous in RELATIVE_PREFIXES:
        return True
    if previous not in PATH_SEPARATORS:
        return False
    component = previous_path_component(buffer, position).decode("ascii", errors="ignore")
    return is_session_dir(Path(component))


def rewrite_text_references(path: Path) -> int:
    if path.stat().st_size == 0:
        return 0
    old_token = OLD_DIR_NAME.encode("ascii")
    new_token = MOCAP_DIR_NAME.encode("ascii")
    replacements = 0
    with path.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as buffer:
        position = buffer.find(old_token)
        while position >= 0:
            next_position = position + len(old_token)
            if is_directory_reference(buffer, position):
                buffer[position:next_position] = new_token
                replacements += 1
            position = buffer.find(old_token, next_position)
        if replacements:
            buffer.flush()
    return replacements


def migrated_archive_name(name: str) -> str:
    path = PurePosixPath(name.replace("\\", "/"))
    parts = [MOCAP_DIR_NAME if part.lower() == OLD_DIR_NAME else part for part in path.parts]
    migrated = PurePosixPath(*parts).as_posix()
    if name.endswith(("/", "\\")) and not migrated.endswith("/"):
        migrated += "/"
    return migrated


def zip_requires_migration(path: Path) -> bool:
    with zipfile.ZipFile(path) as archive:
        return any(
            migrated_archive_name(info.filename) != info.filename for info in archive.infolist()
        )


def encoded_zip_name(name: str, utf8: bool) -> bytes:
    return name.encode("utf-8" if utf8 else "cp437")


def patch_unicode_path_extra(
    buffer: mmap.mmap, extra_start: int, extra_length: int, raw_name: bytes
) -> None:
    cursor = extra_start
    end = extra_start + extra_length
    while cursor + 4 <= end:
        field_id, field_length = struct.unpack_from("<HH", buffer, cursor)
        value_start = cursor + 4
        value_end = value_start + field_length
        if value_end > end:
            raise zipfile.BadZipFile("Invalid ZIP extra field length")
        if field_id == 0x7075 and field_length >= 5:
            unicode_start = value_start + 5
            unicode_name = bytes(buffer[unicode_start:value_end]).decode("utf-8")
            migrated = migrated_archive_name(unicode_name).encode("utf-8")
            if migrated != buffer[unicode_start:value_end]:
                if len(migrated) != value_end - unicode_start:
                    raise ValueError("ZIP Unicode path migration changed encoded name length")
                buffer[value_start + 1 : value_start + 5] = struct.pack("<L", zlib.crc32(raw_name))
                buffer[unicode_start:value_end] = migrated
        cursor = value_end


def patch_zip_name(
    buffer: mmap.mmap, name_start: int, name_length: int, utf8: bool
) -> tuple[bytes, bool]:
    raw_name = bytes(buffer[name_start : name_start + name_length])
    name = raw_name.decode("utf-8" if utf8 else "cp437")
    migrated_name = migrated_archive_name(name)
    if migrated_name == name:
        return raw_name, False
    migrated = encoded_zip_name(migrated_name, utf8)
    if len(migrated) != name_length:
        raise ValueError("ZIP path migration changed encoded name length")
    buffer[name_start : name_start + name_length] = migrated
    return migrated, True


def rewrite_zip(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        central_directory_offset = archive.start_dir
        output_names = [migrated_archive_name(info.filename) for info in infos]
    if len(output_names) != len(set(output_names)):
        raise ValueError(f"Archive path collision after migration: {path}")

    renamed_entries = 0
    with path.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as buffer:
        for info in infos:
            offset = info.header_offset
            if buffer[offset : offset + 4] != b"PK\x03\x04":
                raise zipfile.BadZipFile(f"Invalid local ZIP header at {offset}: {path}")
            flags = struct.unpack_from("<H", buffer, offset + 6)[0]
            name_length, extra_length = struct.unpack_from("<HH", buffer, offset + 26)
            name_start = offset + 30
            raw_name, _ = patch_zip_name(buffer, name_start, name_length, bool(flags & 0x800))
            patch_unicode_path_extra(buffer, name_start + name_length, extra_length, raw_name)

        offset = central_directory_offset
        for _info in infos:
            if buffer[offset : offset + 4] != b"PK\x01\x02":
                raise zipfile.BadZipFile(f"Invalid central ZIP header at {offset}: {path}")
            flags = struct.unpack_from("<H", buffer, offset + 8)[0]
            name_length, extra_length, comment_length = struct.unpack_from(
                "<HHH", buffer, offset + 28
            )
            name_start = offset + 46
            raw_name, renamed = patch_zip_name(buffer, name_start, name_length, bool(flags & 0x800))
            patch_unicode_path_extra(buffer, name_start + name_length, extra_length, raw_name)
            renamed_entries += int(renamed)
            offset = name_start + name_length + extra_length + comment_length
        if renamed_entries:
            buffer.flush()

    with zipfile.ZipFile(path) as verification:
        if any(
            migrated_archive_name(info.filename) != info.filename
            for info in verification.infolist()
        ):
            raise zipfile.BadZipFile(f"ZIP still contains {OLD_DIR_NAME}/ entries: {path}")
    return renamed_entries


def affected_zip_files(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("*.zip") if zip_requires_migration(path))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename session-level nokov/ directories and generated path references to mocap/."
    )
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="Apply changes; otherwise only print a plan."
    )
    parser.add_argument(
        "--rewrite-zip",
        action="store_true",
        help="Also rewrite top-level ZIP entry names containing a nokov path component.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)

    old_dirs = discover_old_session_dirs(root)
    for old_path in old_dirs:
        print(f"directory: {old_path} -> {old_path.with_name(MOCAP_DIR_NAME)}")
    zip_files = affected_zip_files(root) if args.rewrite_zip else []
    for path in zip_files:
        print(f"zip: {path}")
    text_files = generated_text_files(root)
    print(
        f"plan: directories={len(old_dirs)}, generated_text_files={len(text_files)}, "
        f"zip_archives={len(zip_files)}"
    )
    if not args.apply:
        return 0

    renamed = rename_session_dirs(old_dirs, root)
    text_replacements = sum(rewrite_text_references(path) for path in text_files)
    zip_entries = sum(rewrite_zip(path) for path in zip_files)
    print(
        f"done: directories={len(renamed)}, text_references={text_replacements}, "
        f"zip_entries={zip_entries}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
