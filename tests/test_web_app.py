import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from robocap_rerun_tools import web_app


def test_web_app_builds_with_report_viewer() -> None:
    assert web_app.build_app() is not None


def test_run_process_does_not_set_a_timeout(monkeypatch) -> None:
    invocation = {}

    def fake_run(args, **kwargs):
        invocation["args"] = args
        invocation["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="Done.\n", stderr="")

    monkeypatch.setattr(web_app.subprocess, "run", fake_run)

    assert web_app.run_process(["tool", "arg"]) == (0, "Done.")
    assert "timeout" not in invocation["kwargs"]


def test_web_adds_localhost_to_no_proxy(monkeypatch) -> None:
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "example.com")

    web_app.ensure_localhost_no_proxy()

    assert os.environ["NO_PROXY"] == "example.com,127.0.0.1,localhost"
    assert os.environ["no_proxy"] == "example.com,127.0.0.1,localhost"


def test_web_inspect_prints_generated_html_report_path(tmp_path, monkeypatch) -> None:
    report_path = (
        tmp_path / "_artifacts" / "segment1" / "inspection" / "timestamp_anomaly_detail_table.html"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<!doctype html><title>report</title>", encoding="utf-8")
    captured = []
    monkeypatch.setattr(
        web_app,
        "run_cli_result",
        lambda args: (
            captured.extend(args) or (0, f"Wrote timestamp anomaly inspection to {report_path}")
        ),
    )

    output = web_app.inspect_session(str(tmp_path), "segment1")

    assert captured == ["inspect", str(tmp_path), "--segment", "segment1"]
    assert "Wrote timestamp anomaly inspection" in output
    assert f"Timestamp anomaly HTML: `{report_path}`" in output
    assert "<!doctype html>" not in output


def test_web_inspect_does_not_print_stale_report_after_failure(tmp_path, monkeypatch) -> None:
    report_path = (
        tmp_path / "_artifacts" / "segment1" / "inspection" / "timestamp_anomaly_detail_table.html"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<title>stale report</title>\n", encoding="utf-8")
    monkeypatch.setattr(web_app, "run_cli_result", lambda _args: (2, "inspection failed"))

    output = web_app.inspect_session(str(tmp_path), "segment1")

    assert "inspection failed" in output
    assert "Command failed with exit code 2" in output
    assert "stale report" not in output


def test_check_environment_includes_repository_details(monkeypatch) -> None:
    queried_tools: list[str] = []

    def fake_which(name: str) -> str:
        queried_tools.append(name)
        return f"C:/tools/{name}.exe"

    monkeypatch.setattr(web_app.shutil, "which", fake_which)
    monkeypatch.setattr(web_app, "first_line", lambda command: f"{command[0]} version")
    monkeypatch.setattr(web_app, "package_version", lambda _name: "1.0")
    monkeypatch.setattr(
        web_app,
        "git_repository_report",
        lambda **_kwargs: (
            "## Git repository\n\n- branch: `master`\n- remote_origin: `https://example`"
        ),
    )

    report = web_app.check_environment()

    assert "git" in queried_tools
    assert "## Git repository" in report
    assert "- branch: `master`" in report
    assert "- remote_origin: `https://example`" in report
    assert "- ffmpeg:" in report
    assert "- ffprobe:" in report


def test_git_repository_report_fetches_and_reports_behind_state(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(web_app, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_app.shutil, "which", lambda name: "git.exe" if name == "git" else None)
    calls: list[list[str]] = []

    def fake_run_process(command, cwd=None):
        calls.append(command)
        arguments = command[1:]
        responses = {
            ("fetch", "--prune", "origin"): (0, ""),
            ("branch", "--show-current"): (0, "master"),
            ("rev-parse", "--short=12", "HEAD"): (0, "abc123"),
            ("remote", "get-url", "origin"): (0, "https://github.com/example/repo.git"),
            (
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
            ): (0, "origin/master"),
            ("status", "--porcelain"): (0, ""),
            ("rev-list", "--left-right", "--count", "HEAD...origin/master"): (0, "0\t3"),
        }
        return responses[tuple(arguments)]

    monkeypatch.setattr(web_app, "run_process", fake_run_process)

    report = web_app.git_repository_report(fetch=True)

    assert ["git.exe", "fetch", "--prune", "origin"] in calls
    assert "- behind: `3`" in report
    assert "update available (3 commits behind)" in report
    assert "- working_tree: `clean`" in report


def test_code_update_refuses_dirty_worktree_without_stopping_web(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(web_app, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_app.shutil, "which", lambda name: "git.exe" if name == "git" else None)
    monkeypatch.setattr(web_app, "run_process", lambda *_args, **_kwargs: (0, " M local.py"))
    launched: list[str] = []
    monkeypatch.setattr(
        web_app, "launch_update_window", lambda mode: launched.append(mode) or "launched"
    )

    message = web_app.update_code_and_restart()

    assert not launched
    assert "Working tree is not clean" in message
    assert "No process was stopped" in message


def test_code_update_launches_clean_fast_forward_flow(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(web_app, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_app.shutil, "which", lambda name: "git.exe" if name == "git" else None)
    monkeypatch.setattr(web_app, "run_process", lambda *_args, **_kwargs: (0, ""))
    launched: list[str] = []
    monkeypatch.setattr(
        web_app, "launch_update_window", lambda mode: launched.append(mode) or "launched"
    )

    assert web_app.update_code_and_restart() == "launched"
    assert launched == ["code"]


def test_windows_update_script_preflights_before_stopping_and_fast_forward_pull() -> None:
    script = (web_app.PROJECT_ROOT / "scripts" / "web_update_and_restart.bat").read_text(
        encoding="utf-8"
    )

    preflight = script.index("git status --porcelain")
    stop_web = script.index("taskkill /PID")
    pull = script.index("git pull --ff-only")
    install = script.index('uv pip install -e ".[web]"')

    assert preflight < stop_web < pull < install
    assert "No process was stopped and no files were changed." in script
    assert 'call "%REPO_DIR%\\start_web.bat"' in script


def test_scan_files_reflects_detected_robowrist_streams(tmp_path) -> None:
    gt_dir = tmp_path / "nokov"
    gt_dir.mkdir()

    without_wrist = web_app.scan_files(str(tmp_path), "segment1", str(gt_dir), True)

    assert "Robowrist streams: 0" in without_wrist[0]
    assert without_wrist[4]["value"] is False
    assert without_wrist[4]["interactive"] is False

    wrist_dir = tmp_path / "robowrist_device_left"
    wrist_dir.mkdir()
    (wrist_dir / "robowrist_segment1_video_left_down.mp4").write_bytes(b"")

    preserved_off = web_app.scan_files(str(tmp_path), "segment1", str(gt_dir), False)
    detected_on = web_app.scan_files(str(tmp_path), "segment1", str(gt_dir), True)

    assert "Robowrist streams: 1" in detected_on[0]
    assert preserved_off[4]["value"] is False
    assert preserved_off[4]["interactive"] is True
    assert detected_on[4]["value"] is True
    assert detected_on[4]["interactive"] is True


def test_scan_rrd_files_selects_newest_recording(tmp_path) -> None:
    old_rrd = tmp_path / "a_old.rrd"
    new_rrd = tmp_path / "z_new.rrd"
    old_rrd.write_bytes(b"")
    new_rrd.write_bytes(b"")
    os.utime(old_rrd, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    os.utime(new_rrd, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))

    _summary, update = web_app.scan_rrd_files(str(tmp_path))

    assert update["value"] == str(new_rrd)
    assert update["choices"] == [str(new_rrd), str(old_rrd)]


def test_scan_timestamp_reports_selects_newest_report(tmp_path) -> None:
    old_report = tmp_path / "a" / "timestamp_anomaly_detail_table.html"
    new_report = tmp_path / "b" / "timestamp_anomaly_detail_table.html"
    old_report.parent.mkdir()
    new_report.parent.mkdir()
    old_report.write_text("old", encoding="utf-8")
    new_report.write_text("new", encoding="utf-8")
    os.utime(old_report, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    os.utime(new_report, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))

    summary, update = web_app.scan_timestamp_reports(str(tmp_path))

    assert "Timestamp anomaly reports: 2" in summary
    assert update["value"] == str(new_report)
    assert update["choices"] == [str(new_report), str(old_report)]


def test_open_timestamp_report_uses_default_browser(tmp_path, monkeypatch) -> None:
    report = tmp_path / "timestamp_anomaly_detail_table.html"
    report.write_text("<!doctype html>", encoding="utf-8")
    opened: list[tuple[str, int]] = []
    monkeypatch.setattr(
        web_app.webbrowser,
        "open",
        lambda uri, new=0: opened.append((uri, new)) or True,
    )

    message = web_app.open_timestamp_report(str(report))

    assert opened == [(report.resolve().as_uri(), 2)]
    assert str(report.resolve()) in message


def test_modelscope_status_never_exposes_token() -> None:
    settings = SimpleNamespace(
        token="secret-token",
        token_source=".env",
        endpoint="https://modelscope.cn",
        repo_id="owner/egomocap",
        env_path="C:/repo/.env",
    )

    status = web_app.format_modelscope_status(settings, "中文")

    assert "已配置" in status
    assert "owner/egomocap" in status
    assert "secret-token" not in status


def test_web_does_not_duplicate_dataset_structure_documentation() -> None:
    assert "modelscope_help" not in web_app.LANGUAGE_PACKS["English"]
    assert "modelscope_help" not in web_app.LANGUAGE_PACKS["中文"]
    assert "<dataset_root>/" not in web_app.EN_DOC
    assert "<dataset_root>/" not in web_app.ZH_DOC


def test_web_modelscope_auth_reports_invalid_endpoint(monkeypatch) -> None:
    from robocap_rerun_tools import modelscope_publisher

    settings = modelscope_publisher.ModelScopeSettings(
        None,
        "https://modelscope.cn",
        modelscope_publisher.DEFAULT_ENV_PATH,
        "missing",
    )
    monkeypatch.setattr(modelscope_publisher, "load_modelscope_settings", lambda: settings)

    output = web_app.check_modelscope_web_auth("", "not-an-origin", "中文")

    assert "HTTP(S) origin" in output


def test_web_modelscope_save_persists_repo_id(monkeypatch) -> None:
    from robocap_rerun_tools import modelscope_publisher

    captured: dict[str, object] = {}

    def fake_save(token, endpoint, *, repo_id=None):
        captured.update(token=token, endpoint=endpoint, repo_id=repo_id)
        return modelscope_publisher.ModelScopeSettings(
            token,
            endpoint,
            Path("C:/repo/.env"),
            "unsaved Web input",
            repo_id,
        )

    monkeypatch.setattr(modelscope_publisher, "save_modelscope_settings", fake_save)

    message, status, cleared_token = web_app.save_modelscope_web_settings(
        "secret-token", "https://modelscope.cn", "owner/egomocap", "中文"
    )

    assert captured == {
        "token": "secret-token",
        "endpoint": "https://modelscope.cn",
        "repo_id": "owner/egomocap",
    }
    assert "owner/egomocap" in status
    assert "secret-token" not in message + status
    assert cleared_token == ""


def test_web_modelscope_stage_builds_compressed_cli_command(tmp_path, monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(web_app, "run_cli", lambda args: captured.extend(args) or "Done.")

    output = web_app.stage_modelscope_data(
        str(tmp_path),
        "segment1",
        "P03",
        True,
        False,
        True,
        720,
        24,
    )

    assert output == "Done."
    assert captured[:4] == ["modelscope-stage", str(tmp_path), "--primitive-id", "P03"]
    assert "--refresh-inspection" in captured
    assert "--include-rrd" in captured
    assert "--raw-video" not in captured
    assert "--dataset-root" not in captured
    assert captured[captured.index("--proxy-height") + 1] == "720"


def test_web_modelscope_upload_never_passes_token_on_command_line(tmp_path, monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(web_app, "run_cli", lambda args: captured.extend(args) or "Done.")

    output = web_app.upload_modelscope_data(
        str(tmp_path),
        "owner/egomocap",
        "master",
        True,
        "private",
        "cc-by-4.0",
        True,
        6,
    )

    assert output == "Done."
    assert captured[0] == "modelscope-upload"
    assert captured[1] == str(tmp_path.parent / "_modelscope_dataset")
    assert captured[captured.index("--repo-id") + 1] == "owner/egomocap"
    assert "--create-if-missing" in captured
    assert "--no-cache" not in captured
    assert all("token" not in item.lower() for item in captured)


def test_web_modelscope_upload_omits_blank_repo_id_for_env_fallback(tmp_path, monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(web_app, "run_cli", lambda args: captured.extend(args) or "Done.")

    output = web_app.upload_modelscope_data(
        str(tmp_path), "", "master", False, "private", "", True, 4
    )

    assert output == "Done."
    assert "--repo-id" not in captured
