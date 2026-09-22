import json

import pytest

from robocap_rerun_tools.participant_metadata import enrich_record, load_catalog, parse_catalog


def test_catalog_matches_case_and_retains_unknown_measurements():
    catalog = parse_catalog(
        '{"participant":"wangyang","gender":"male","height_cm":null,"weight_kg":null}'
    )
    record = {"mocap_capture": {"participant": " WangYang "}}
    actual = enrich_record(record, catalog)
    assert actual["participant_gender"] == "male"
    assert actual["participant_height_cm"] is None
    assert actual["participant_weight_kg"] is None


def test_unmatched_keeps_remote_info_only_for_same_person():
    previous = {"mocap_capture": {"participant": "alice"}, "participant_height_cm": 160}
    record = {"mocap_capture": {"participant": "alice"}}
    assert enrich_record(record, {}, previous)["participant_height_cm"] == 160
    record["mocap_capture"]["participant"] = "bob"
    assert enrich_record(record, {}, previous)["participant_height_cm"] is None


@pytest.mark.parametrize(
    "document",
    [
        '{"participant":"a"}\n{"participant":"A"}',
        '{"name":"a"}',
        '{"participant":"a","height_cm":-1}',
        '{"participant":"a","weight_kg":true}',
    ],
)
def test_invalid_catalog_fails_instead_of_silently_publishing(document):
    with pytest.raises(ValueError):
        parse_catalog(document)


def test_fault_catalog_falls_back_only_when_file_missing(tmp_path):
    from modelscope_hub.errors import NotExistError

    path = tmp_path / "participants.jsonl"
    path.write_text(json.dumps({"participant": "alice", "gender": "female"}), encoding="utf-8")
    calls = []

    class Api:
        def download_file(self, repo, *args, **kwargs):
            calls.append(repo)
            if repo == "owner/fault":
                raise NotExistError("missing")
            return path

    assert "alice" in load_catalog(Api(), "owner/fault", "master", fallback="owner/normal")
    assert calls == ["owner/fault", "owner/normal"]

    class BrokenApi:
        def download_file(self, *args, **kwargs):
            raise OSError("network unavailable")

    with pytest.raises(OSError):
        load_catalog(BrokenApi(), "owner/fault", "master", fallback="owner/normal")
