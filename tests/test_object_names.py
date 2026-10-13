from robocap_rerun_tools.mocap_inventory import object_names


def test_only_actual_object_instances_are_published():
    assert object_names(["Body0", "Tracker0", "Unnamed0", "a0", "a1", "a1"]) == ["a0", "a1"]
    assert object_names(["a3"]) == ["a3"]
