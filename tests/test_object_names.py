from robocap_rerun_tools.mocap_inventory import object_names


def test_only_actual_object_instances_are_published():
    assert object_names(["Body0", "Tracker0", "Unnamed0", "a0", "a1", "a1"]) == ["A0", "A1"]
    assert object_names(["a3"]) == ["A3"]


def test_scan_names_and_instances_share_pascal_case():
    assert object_names(["Open box", "open_box0", "CcOrange ", "Tray", "BigBox3"]) == [
        "BigBox3", "CcOrange0", "OpenBox0", "Tray0",
    ]
