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


def test_check_environment_omits_repository_details(monkeypatch) -> None:
    queried_tools: list[str] = []

    def fake_which(name: str) -> str:
        queried_tools.append(name)
        return f"C:/tools/{name}.exe"

    monkeypatch.setattr(web_app.shutil, "which", fake_which)
    monkeypatch.setattr(web_app, "first_line", lambda command: f"{command[0]} version")
    monkeypatch.setattr(web_app, "package_version", lambda _name: "1.0")

    report = web_app.check_environment()

    assert "git" not in queried_tools
    assert "project_root" not in report
    assert "## Git" not in report
    assert "- branch:" not in report
    assert "- commit:" not in report
    assert "- remote:" not in report
    assert "- ffmpeg:" in report
    assert "- ffprobe:" in report
