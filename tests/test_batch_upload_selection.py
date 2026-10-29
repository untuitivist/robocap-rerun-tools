import pytest

from robocap_rerun_tools import web_app


@pytest.mark.parametrize("clean,fault,expected", [
    (True, False, ["normal"]), (False, True, ["fault"]),
    (True, True, ["normal", "fault"]), (False, False, []),
])
def test_batch_selection_routes_serially(monkeypatch, clean, fault, expected):
    calls = []
    def upload(*args, quality_target):
        calls.append((quality_target, "start"))
        yield quality_target + " progress"
        calls.append((quality_target, "end"))
    monkeypatch.setattr(web_app, "bulk_upload_clean_modelscope_sessions", upload)
    output = list(web_app.bulk_upload_selected_modelscope_sessions(
        "root", 4, False, upload_clean=clean, upload_fault=fault,
    ))
    assert calls == [(target, phase) for target in expected for phase in ("start", "end")]
    assert output
    if len(expected) == 2:
        assert "normal progress" in output[-1] and "fault progress" in output[-1]


def test_batch_selection_defaults_to_normal(monkeypatch):
    calls = []
    def upload(*args, quality_target):
        calls.append(quality_target)
        yield "done"
    monkeypatch.setattr(web_app, "bulk_upload_clean_modelscope_sessions", upload)
    list(web_app.bulk_upload_selected_modelscope_sessions("root", 4, False))
    assert calls == ["normal"]
