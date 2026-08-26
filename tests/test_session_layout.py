from pathlib import Path

from robocap_rerun_tools.session_layout import (
    canonical_mocap_relative_path,
    discover_mocap_directories,
    is_mocap_directory_name,
    is_path_under_mocap,
)


def test_mocap_directory_name_is_a_case_insensitive_prefix() -> None:
    for name in ("mocap", "mocap_01", "Mocap-NOKOV", "MOCAPcapture"):
        assert is_mocap_directory_name(name)

    for name in ("nokov", "test1", "capture_mocap"):
        assert not is_mocap_directory_name(name)


def test_discover_mocap_directories_only_returns_direct_children(tmp_path: Path) -> None:
    session = tmp_path / "session"
    (session / "mocap_02").mkdir(parents=True)
    (session / "Mocap-01").mkdir()
    (session / "other" / "mocap_nested").mkdir(parents=True)
    (session / "mocap_file").write_text("not a directory", encoding="utf-8")

    assert [path.name for path in discover_mocap_directories(session)] == [
        "Mocap-01",
        "mocap_02",
    ]


def test_mocap_paths_are_detected_and_mapped_to_canonical_dataset_path(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session"
    motion = session / "Mocap-NOKOV" / "take" / "motion.trc"
    nested_elsewhere = session / "other" / "mocap_nested" / "motion.trc"
    root_file = session / "mocap_notes.txt"

    assert is_path_under_mocap(motion, session)
    assert not is_path_under_mocap(nested_elsewhere, session)
    assert not is_path_under_mocap(root_file, session)
    assert canonical_mocap_relative_path(motion, session) == Path(
        "mocap", "take", "motion.trc"
    )
    assert canonical_mocap_relative_path(nested_elsewhere, session) == Path(
        "other", "mocap_nested", "motion.trc"
    )
    assert canonical_mocap_relative_path(root_file, session) == Path("mocap_notes.txt")
