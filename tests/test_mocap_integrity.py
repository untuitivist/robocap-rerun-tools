from pathlib import Path

import pytest

from robocap_rerun_tools.mocap_integrity import check_mocap_exports, read_requirements


def check(tmp_path, names=(), *, action="P01", rules=None):
    directory = tmp_path / f"mocap-{action}-S01-person-02p1"
    paths = [directory / name for name in names]
    config = tmp_path / "requirements.csv"
    if rules is not None:
        config.write_text("action_id,additional_bodies,objects\n" + rules, encoding="utf-8")
    rows = []

    def add(path, category, result, requirement=""):
        rows.append({"path": str(path), "category": category, "requirement": requirement, **result})

    check_mocap_exports([directory], paths, add, requirements_path=config)
    return rows


BASE = ["Body0.trc", "Body0.bvh", "Body0.csv", "Tracker0.trc", "Tracker0.csv"]


@pytest.mark.parametrize("missing", BASE)
def test_every_main_body_format_and_hat_format_is_required(tmp_path, missing):
    rows = check(tmp_path, [name for name in BASE if name != missing], rules="P01,,\n")
    absent = [row for row in rows if row["state"] == "missing"]
    assert len(absent) == 1 and absent[0]["path"].endswith(missing)


def test_body1_cannot_replace_body0_and_tracker_start_cannot_replace_hat(tmp_path):
    names = [name.replace("Body0", "Body1").replace("Tracker0", "Tracker-start") for name in BASE]
    rows = check(tmp_path, names, rules="P01,,\n")
    assert len([row for row in rows if row["state"] == "missing"]) == 5


def test_extra_body_is_optional_but_must_have_its_own_three_formats(tmp_path):
    assert all(row["state"] == "ok" for row in check(tmp_path, BASE, rules="P01,,\n"))
    rows = check(tmp_path, BASE + ["Body2.csv"], rules="P01,,\n")
    missing = [Path(row["path"]).name for row in rows if row["state"] == "missing"]
    assert missing == ["Body2.trc", "Body2.bvh"]


def test_multiple_props_require_a_pair_for_each_instance_not_bvh(tmp_path):
    rows = check(
        tmp_path,
        BASE + ["BigBox0.trc", "BigBox0.csv", "BigBox1.trc"],
        rules="P01,,BigBox0;BigBox1;Chair\n",
    )
    missing = {Path(row["path"]).name for row in rows if row["state"] == "missing"}
    assert missing == {"BigBox1.csv", "Chair.csv", "Chair.trc"}
    assert not any("BigBox0.bvh" in row["path"] for row in rows)


def test_unknown_roster_is_not_reported_as_no_required_objects(tmp_path):
    rows = check(tmp_path, BASE, rules="")
    assert any(row["state"] == "requirements_unknown" for row in rows)
    rows = check(tmp_path, BASE, rules="P01,,\n")
    assert all(row["state"] == "ok" for row in rows)


def test_formats_cannot_be_borrowed_from_other_takes(tmp_path):
    names = [f"mocap-P01-S01-person-02p1-{name}" for name in BASE]
    names[0] = names[0].replace("S01", "S02")
    rows = check(tmp_path, names, rules="P01,,\n")
    assert any(row["state"] == "ambiguous" for row in rows)
    assert len([row for row in rows if row["state"] == "missing"]) == 3


def test_case_insensitive_names_and_duplicate_files(tmp_path):
    names = [name.upper() for name in BASE]
    assert all(row["state"] == "ok" for row in check(tmp_path, names, rules="P01,,\n"))
    rows = check(tmp_path, names + ["backup/BODY0.TRC"], rules="P01,,\n")
    assert len([row for row in rows if row["state"] == "ambiguous"]) == 1


def test_binary_definition_or_export_is_not_a_complete_object(tmp_path):
    rows = check(tmp_path, BASE + ["Box0.mars", "Box1.trb"], rules="P01,,\n")
    assert len([row for row in rows if row["state"] == "missing"]) == 4
    rows = check(tmp_path, BASE + ["Tracker0.mars", "Body0.mars"], rules="P01,,\n")
    assert all(row["state"] == "ok" for row in rows)


def test_hh_does_not_impose_a_fixed_body_count(tmp_path):
    rows = check(tmp_path, BASE + ["Box.csv", "Box.trc"], action="HH01", rules="HH01,,Box\n")
    problems = [row for row in rows if row["state"] != "ok"]
    assert not problems
    assert all(row["state"] == "ok" for row in check(tmp_path, BASE, action="HH01", rules="HH01,,\n"))
    names = BASE + ["Body3.csv", "Body3.trc", "Body3.bvh"]
    assert all(
        row["state"] == "ok" for row in check(tmp_path, names, action="HH15", rules="HH15,,\n")
    )


def test_configured_extra_human_must_exist_even_when_not_exported(tmp_path):
    rows = check(tmp_path, BASE, rules="P01,Body2,\n")
    missing = {Path(row["path"]).name for row in rows if row["state"] == "missing"}
    assert missing == {"Body2.trc", "Body2.csv", "Body2.bvh"}


@pytest.mark.parametrize(
    "text",
    [
        "wrong,header\n",
        "action_id,additional_bodies,objects\nP01,,Box\np01,,Chair\n",
        "action_id,additional_bodies,objects\nP01,Chair,\n",
        "action_id,additional_bodies,objects\nP01,,Tracker0\n",
        "action_id,additional_bodies,objects\nP01,,Box;box\n",
        "action_id,additional_bodies,objects\nP01,,../Box\n",
        "action_id,additional_bodies,objects\nP01\n",
    ],
)
def test_bad_requirements_are_rejected(tmp_path, text):
    path = tmp_path / "rules.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        read_requirements(path)
