from __future__ import annotations

import pytest

from robocap_rerun_tools.mocap_metadata import (
    build_mocap_capture_metadata,
    parse_mocap_capture_directory,
)


def test_parse_mocap_capture_directory_extracts_all_fields() -> None:
    metadata = parse_mocap_capture_directory("mocap-L01-S07-wang-yang-10p")

    assert metadata is not None
    assert metadata.as_record() == {
        "source_directory": "mocap-L01-S07-wang-yang-10p",
        "action_id": "L01",
        "collection_session_index": 7,
        "participant": "wang-yang",
        "repetition_count": 10,
    }


@pytest.mark.parametrize("suffix", ["1", "2", "001"])
def test_parse_mocap_capture_directory_ignores_numeric_suffix_after_p(suffix: str) -> None:
    name = f"mocap-L01-S07-wang-yang-10p{suffix}"

    metadata = parse_mocap_capture_directory(name)

    assert metadata is not None
    assert metadata.source_directory == name
    assert metadata.participant == "wang-yang"
    assert metadata.repetition_count == 10


@pytest.mark.parametrize(
    "name",
    [
        "mocap",
        "mocap-L01",
        "mocap-L1-S07-wangyang-10p",
        "mocap-L01-S07-wangyang",
        "mocap-L01-S07-wangyang-10p-copy",
        "other-L01-S07-wangyang-10p",
    ],
)
def test_parse_mocap_capture_directory_requires_complete_name(name: str) -> None:
    assert parse_mocap_capture_directory(name) is None


def test_build_mocap_capture_metadata_validates_user_edits() -> None:
    metadata = build_mocap_capture_metadata(
        "mocap-L01-S07-wangyang-10p",
        "a02",
        12.0,
        "participant_2",
        "4",
    )

    assert metadata.action_id == "A02"
    assert metadata.collection_session_index == 12
    assert metadata.participant == "participant_2"
    assert metadata.repetition_count == 4

    with pytest.raises(ValueError, match=r"\[A-Z\]NN"):
        build_mocap_capture_metadata("mocap-name", "walk", 1, "participant", 1)
    with pytest.raises(ValueError, match="repetition count"):
        build_mocap_capture_metadata("mocap-name", "P01", 1, "participant", 0)
