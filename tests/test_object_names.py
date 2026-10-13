from robocap_rerun_tools.mocap_inventory import object_names


def test_instances_are_deduplicated_as_scan_categories():
    assert object_names(["Body0", "Tracker0", "Unnamed0", "a0", "a1", "a1"]) == ["A"]
    assert object_names(["a3"]) == ["A"]


def test_scan_names_and_instances_share_pascal_case():
    assert object_names(["Open box", "open_box0", "CcOrange ", "Tray", "BigBox3"]) == [
        "BigBox", "CcOrange", "OpenBox", "Tray",
    ]


def test_categories_are_distinct_and_normalization_is_idempotent():
    expected = ["BigBox", "Box", "StrongeBox"]
    assert object_names(["Box1", "Box6", "box0002", "BigBox3", "StrongeBox1234"]) == expected
    assert object_names(expected) == expected
