from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "migrate_mocap_layout.py"
SPEC = importlib.util.spec_from_file_location("migrate_mocap_layout", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def test_rename_only_session_level_nokov_directories(tmp_path: Path) -> None:
    collection_root = tmp_path / "nokov"
    session = collection_root / "20210101_121127_session23"
    old_dir = session / "nokov"
    old_dir.mkdir(parents=True)
    (old_dir / "motion.trc").write_text("data", encoding="utf-8")

    discovered = migration.discover_old_session_dirs(collection_root)
    renamed = migration.rename_session_dirs(discovered, collection_root)

    assert renamed == [(old_dir, session / "mocap")]
    assert collection_root.is_dir()
    assert (session / "mocap" / "motion.trc").read_text(encoding="utf-8") == "data"


def test_rewrite_text_references_preserves_collection_name_and_vendor_terms(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.html"
    report.write_text(
        r"Z:\DATASETS\Frodobots\nokov\20210101_121127_session23"
        "\n"
        r"Z:\DATASETS\Frodobots\nokov\20210101_121127_session23\nokov\motion.trc"
        "\n"
        '"nokov/video.mp4"\n'
        "NOKOV/GT is a vendor label",
        encoding="utf-8",
    )

    replacements = migration.rewrite_text_references(report)
    result = report.read_text(encoding="utf-8")

    assert replacements == 2
    assert r"Z:\DATASETS\Frodobots\nokov\20210101_121127_session23" in result
    assert r"20210101_121127_session23\mocap\motion.trc" in result
    assert '"mocap/video.mp4"' in result
    assert "NOKOV/GT is a vendor label" in result


def test_rewrite_zip_renames_only_matching_path_components(tmp_path: Path) -> None:
    archive_path = tmp_path / "session.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("nokov/motion.trc", "motion")
        archive.writestr("docs/nokov_notes.txt", "notes")

    renamed = migration.rewrite_zip(archive_path)

    assert renamed == 1
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["mocap/motion.trc", "docs/nokov_notes.txt"]
        assert archive.read("mocap/motion.trc") == b"motion"


def test_rewrite_zip_does_not_read_preexisting_bad_crc_data(tmp_path: Path) -> None:
    archive_path = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("nokov/motion.trc", b"motion")
    with zipfile.ZipFile(archive_path) as archive:
        info = archive.getinfo("nokov/motion.trc")
    data_offset = info.header_offset + 30 + len(info.filename.encode("ascii")) + len(info.extra)
    with archive_path.open("r+b") as archive_file:
        archive_file.seek(data_offset)
        original = archive_file.read(1)
        archive_file.seek(data_offset)
        archive_file.write(bytes([original[0] ^ 0xFF]))

    with (
        zipfile.ZipFile(archive_path) as archive,
        pytest.raises(zipfile.BadZipFile, match="Bad CRC-32"),
    ):
        archive.read("nokov/motion.trc")

    assert migration.rewrite_zip(archive_path) == 1
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["mocap/motion.trc"]
