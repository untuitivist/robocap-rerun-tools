import subprocess

from robocap_rerun_tools import web_app


def test_run_process_does_not_set_a_timeout(monkeypatch) -> None:
    invocation = {}

    def fake_run(args, **kwargs):
        invocation["args"] = args
        invocation["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="Done.\n", stderr="")

    monkeypatch.setattr(web_app.subprocess, "run", fake_run)

    assert web_app.run_process(["tool", "arg"]) == (0, "Done.")
    assert "timeout" not in invocation["kwargs"]
