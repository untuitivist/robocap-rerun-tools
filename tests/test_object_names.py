from robocap_rerun_tools.mocap_inventory import object_names


def test_instances_are_retained_and_duplicates_removed():
    assert object_names(["Body0", "Tracker0", "Unnamed0", "a0", "a1", "a1"]) == ["A0", "A1"]
    assert object_names(["a3"]) == ["A3"]


def test_scan_names_and_instances_share_pascal_case():
    assert object_names(["Open box", "open_box0", "CcOrange ", "Tray", "BigBox3"]) == [
        "BigBox3", "CcOrange0", "OpenBox0", "Tray0",
    ]


def test_categories_are_distinct_and_normalization_is_idempotent():
    expected = ["BigBox3", "LittleBox0002", "LittleBox1", "LittleBox6", "StrongeBox1234"]
    assert object_names(["Box1", "Box6", "box0002", "BigBox3", "StrongeBox1234"]) == expected
    assert object_names(expected) == expected


def test_box_alias_does_not_change_other_box_categories():
    assert object_names(["Box0", "LittleBox0", "little box1", "BigBox1", "OpenBox2"]) == [
        "BigBox1", "LittleBox0", "LittleBox1", "OpenBox2",
    ]
