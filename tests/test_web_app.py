import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from robocap_rerun_tools import web_app


def collect_stream(stream) -> str:
    snapshots = list(stream)
    assert snapshots
    return snapshots[-1]


def fake_cli_stream(captured: list[str], output: str = "Done.", returncode: int = 0):
    def stream(args):
        captured.extend(args)
        rendered = output
        if returncode:
            rendered += f"\nCommand failed with exit code {returncode}."
        yield rendered
        return web_app.StreamCommandResult(returncode, output, rendered)

    return stream


def test_web_app_builds_with_report_viewer() -> None:
    app = web_app.build_app()
    config_data = app.get_config_file()
    config = json.dumps(config_data, ensure_ascii=False)
    api_names = [dependency["api_name"] for dependency in config_data["dependencies"]]

    assert app is not None
    assert "数据集根目录" in config
    assert "扫描 Session" in config
    assert "检查动捕比例（8：240 FPS，4：120 FPS）" in config
    assert "参与上传的 RRD 文件" in config
    assert "ratio 和 Offset 默认从“导出 RRD”页填入" in config
    assert sum(name.startswith("rrd_alignment_defaults") for name in api_names) == 2
    assert "保留原始视频" not in config
    assert "仓库不存在时创建" not in config


def test_discover_session_directories_finds_direct_and_nested_sessions(tmp_path) -> None:
    direct = tmp_path / "20260820_030527_session80"
    nested = tmp_path / "EgoMotionActions" / "P03" / "20260821_040000_session81"
    ordinary = tmp_path / "notes"
    for directory in (direct, nested, ordinary):
        directory.mkdir(parents=True)
    (direct / "robocap_segment1_video_left.mp4").write_bytes(b"")
    (nested / "robocap_segment1_imu_left.db").write_bytes(b"")
    (ordinary / "readme.txt").write_text("not a session", encoding="utf-8")

    discovered = web_app.discover_session_directories(tmp_path)

    assert discovered == sorted(
        [direct.resolve(), nested.resolve()], key=lambda path: str(path).casefold()
    )


def test_discover_session_directories_excludes_generated_and_calibration_trees(tmp_path) -> None:
    valid = tmp_path / "source" / "20260821_040000_session81"
    ignored_roots = [
        tmp_path / "_analysis",
        tmp_path / "_artifacts",
        tmp_path / "_modelscope_dataset",
        tmp_path / "raw_calibration",
    ]
    valid.mkdir(parents=True)
    (valid / "robocap_segment1_video_left.mp4").write_bytes(b"")
    for ignored_root in ignored_roots:
        copied = ignored_root / "P01" / "copied_session"
        copied.mkdir(parents=True)
        (copied / "robocap_segment1_video_left.mp4").write_bytes(b"")

    assert web_app.discover_session_directories(tmp_path) == [valid.resolve()]


def test_session_dropdown_choices_use_relative_labels_and_absolute_values(tmp_path) -> None:
    session = tmp_path / "EgoMotionActions" / "P03" / "session81"
    session.mkdir(parents=True)

    choices = web_app.session_dropdown_choices(tmp_path, [session])

    assert choices == [(str(session.relative_to(tmp_path)), str(session.resolve()))]


def test_scan_dataset_sessions_preserves_current_selection_and_settings(tmp_path) -> None:
    first = tmp_path / "session01"
    current = tmp_path / "session02"
    settings_path = tmp_path / "settings" / "web.json"
    for session in (first, current):
        session.mkdir()
        (session / "robocap_segment1_video_left.mp4").write_bytes(b"")

    message, update = web_app.scan_dataset_sessions(
        str(tmp_path), str(current), "中文", settings_path
    )

    assert "识别到 Session：2" in message
    assert update["value"] == str(current.resolve())
    assert update["choices"] == [
        ("session01", str(first.resolve())),
        ("session02", str(current.resolve())),
    ]
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["dataset_root"] == str(tmp_path.resolve())
    assert settings["session_dir"] == str(current.resolve())


def test_session_browser_settings_restore_selection_and_preserve_other_values(tmp_path) -> None:
    settings_path = tmp_path / "web.json"
    settings_path.write_text(
        json.dumps({"default_offset": 5, "offset_unit": web_app.OFFSET_UNIT}),
        encoding="utf-8",
    )
    session = tmp_path / "P01" / "session01"
    other_session = tmp_path / "P02" / "session02"
    for directory in (session, other_session):
        directory.mkdir(parents=True)
        (directory / "robocap_segment1_video_left.mp4").write_bytes(b"")

    web_app.save_session_browser_settings(tmp_path, session, settings_path)
    root, choices, selected = web_app.load_session_browser_settings(settings_path)

    assert root == str(tmp_path.resolve())
    assert choices == [
        (str(session.relative_to(tmp_path)), str(session.resolve())),
        (str(other_session.relative_to(tmp_path)), str(other_session.resolve())),
    ]
    assert selected == str(session.resolve())
    assert web_app.load_default_offset(settings_path) == 5


def test_select_session_clears_session_dependent_file_controls(tmp_path) -> None:
    settings_path = tmp_path / "web.json"
    updates = web_app.select_session(tmp_path, tmp_path / "session01", settings_path)

    assert [update.get("value") for update in updates] == [
        "",
        [],
        "",
        None,
        None,
        [],
        [],
        "",
        True,
    ]
    assert updates[1]["choices"] == []
    assert updates[3]["choices"] == []
    assert updates[4]["choices"] == []
    assert updates[5]["choices"] == []
    assert updates[6]["choices"] == []
    assert updates[8]["interactive"] is True
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["session_dir"] == str(tmp_path / "session01")


def test_run_process_does_not_set_a_timeout(monkeypatch) -> None:
    invocation = {}

    def fake_run(args, **kwargs):
        invocation["args"] = args
        invocation["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="Done.\n", stderr="")

    monkeypatch.setattr(web_app.subprocess, "run", fake_run)

    assert web_app.run_process(["tool", "arg"]) == (0, "Done.")
    assert "timeout" not in invocation["kwargs"]


def test_stream_process_output_refreshes_before_completion_and_parses_progress(
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_app, "STREAM_REFRESH_SECONDS", 0.02)
    script = "\n".join(
        [
            "import sys, time",
            "print('[1/2] inspect first', flush=True)",
            "time.sleep(0.08)",
            "sys.stdout.write('50% upload\\r')",
            "sys.stdout.flush()",
            "time.sleep(0.08)",
            "print('[2/2] inspect second', flush=True)",
        ]
    )

    snapshots = list(
        web_app.stream_process_output(
            [sys.executable, "-u", "-c", script],
            display_command=["test-progress"],
        )
    )

    assert any("Status: RUNNING" in item and "inspect first" in item for item in snapshots[:-1])
    assert "50% upload" in snapshots[-1]
    assert "Status: COMPLETED" in snapshots[-1]
    assert "100.0% 2/2" in snapshots[-1]


def test_stream_process_output_reports_failure_exit_code() -> None:
    snapshots = list(
        web_app.stream_process_output(
            [sys.executable, "-u", "-c", "print('failed step'); raise SystemExit(3)"],
            display_command=["test-failure"],
        )
    )

    assert "Status: FAILED (exit code 3)" in snapshots[-1]
    assert "Command failed with exit code 3." in snapshots[-1]


def test_live_command_output_bounds_retained_log_lines(monkeypatch) -> None:
    monkeypatch.setattr(web_app, "STREAM_LOG_MAX_LINES", 2)
    live = web_app.LiveCommandOutput(["test-log-limit"])

    live.add("first")
    live.add("second")
    live.add("third")

    assert live.dropped_lines == 1
    assert live.output() == "... 1 earlier log lines omitted ...\nsecond\nthird"


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
        "stream_cli_command",
        fake_cli_stream(
            captured,
            f"Wrote timestamp anomaly inspection to {report_path}",
        ),
    )

    output = collect_stream(web_app.inspect_session(str(tmp_path), "segment1", 4))

    assert captured == [
        "inspect",
        str(tmp_path),
        "--segment",
        "segment1",
        "--mocap-ratio",
        "4",
    ]
    assert "Wrote timestamp anomaly inspection" in output
    assert f"Timestamp anomaly HTML: `{report_path}`" in output
    assert "<!doctype html>" not in output


def test_web_inspect_does_not_print_stale_report_after_failure(tmp_path, monkeypatch) -> None:
    report_path = (
        tmp_path / "_artifacts" / "segment1" / "inspection" / "timestamp_anomaly_detail_table.html"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text("<title>stale report</title>\n", encoding="utf-8")
    monkeypatch.setattr(
        web_app,
        "stream_cli_command",
        fake_cli_stream([], "inspection failed", returncode=2),
    )

    output = collect_stream(web_app.inspect_session(str(tmp_path), "segment1"))

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
    assert "- ffmpeg_source:" in report
    assert "ffmpeg-binaries-compat" in report


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


def test_git_repository_report_does_not_claim_fresh_status_after_fetch_failure(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(web_app, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_app.shutil, "which", lambda name: "git.exe" if name == "git" else None)

    def fake_run_process(command, cwd=None):
        arguments = command[1:]
        responses = {
            ("fetch", "--prune", "origin"): (1, "network unavailable"),
            ("branch", "--show-current"): (0, "master"),
            ("rev-parse", "--short=12", "HEAD"): (0, "abc123"),
            ("remote", "get-url", "origin"): (0, "https://github.com/example/repo.git"),
            (
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
            ): (0, "origin/master"),
            ("status", "--porcelain"): (0, "?? local.tmp"),
            ("rev-list", "--left-right", "--count", "HEAD...origin/master"): (0, "0\t0"),
        }
        return responses[tuple(arguments)]

    monkeypatch.setattr(web_app, "run_process", fake_run_process)

    report = web_app.git_repository_report(fetch=True)

    assert "- fetch_origin: `failed`" in report
    assert "- cached_ahead: `0`" in report
    assert "- cached_behind: `0`" in report
    assert "- update_status: `unknown (fetch failed; remote-tracking data may be stale)`" in report
    assert "- update_status: `up to date`" not in report
    assert "- working_tree: `dirty (1 paths)`" in report


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
    install = script.index("uv sync --extra web")

    assert preflight < stop_web < pull < install
    assert "No process was stopped and no files were changed." in script
    assert 'call "%REPO_DIR%\\start_web.bat"' in script


def test_windows_launcher_syncs_uv_managed_web_and_media_dependencies() -> None:
    script = (web_app.PROJECT_ROOT / "start_web.bat").read_text(encoding="utf-8")

    assert "uv sync --extra web" in script
    assert "uv pip install" not in script
    assert "ffmpeg" in script.lower()
    assert "ffprobe" in script.lower()


def test_scan_files_reflects_detected_robowrist_streams(tmp_path) -> None:
    gt_dir = tmp_path / "mocap"
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


def test_scan_modelscope_rrd_files_selects_current_segment_only(tmp_path) -> None:
    segment1 = tmp_path / "_artifacts" / "segment1" / "inspection"
    segment2 = tmp_path / "_artifacts" / "segment2" / "inspection"
    segment1.mkdir(parents=True)
    segment2.mkdir(parents=True)
    (segment1 / "frame.rrd").write_bytes(b"")
    (segment1 / "time.rrd").write_bytes(b"")
    (segment2 / "other.rrd").write_bytes(b"")

    summary, update = web_app.scan_modelscope_rrd_files(str(tmp_path), "segment1")

    expected = [
        str(Path("_artifacts") / "segment1" / "inspection" / "frame.rrd"),
        str(Path("_artifacts") / "segment1" / "inspection" / "time.rrd"),
    ]
    assert "Selectable RRD files: 2" in summary
    assert update["choices"] == expected
    assert update["value"] == expected


def test_scan_modelscope_mocap_files_selects_all_packageable_files(tmp_path) -> None:
    mocap_dir = tmp_path / "Mocap-NOKOV"
    nested = mocap_dir / "take01"
    nested.mkdir(parents=True)
    (mocap_dir / "rigid-body.csv").write_text("frame,x,y,z\n", encoding="utf-8")
    (nested / "motion.trc").write_text("Frame#\tTime\n", encoding="utf-8")
    (mocap_dir / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (mocap_dir / "preview.rrd").write_bytes(b"")

    summary, update = web_app.scan_modelscope_mocap_files(str(tmp_path))

    expected = [
        str(Path("Mocap-NOKOV") / "rigid-body.csv"),
        str(Path("Mocap-NOKOV") / "take01" / "motion.trc"),
    ]
    assert "Selectable Mocap files: 2" in summary
    assert update["choices"] == expected
    assert update["value"] == expected


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
    monkeypatch.setattr(web_app, "stream_cli_command", fake_cli_stream(captured))

    output = collect_stream(
        web_app.stage_modelscope_data(
            str(tmp_path),
            "segment1",
            "P03",
            True,
            [
                str(Path("Mocap-NOKOV") / "motion.trc"),
                str(Path("Mocap-NOKOV") / "rigid-body.csv"),
            ],
            [
                str(Path("_artifacts") / "segment1" / "inspection" / "frame.rrd"),
                str(Path("_artifacts") / "segment1" / "inspection" / "time.rrd"),
            ],
            True,
            "auto",
            -2,
            4,
        )
    )

    assert output == "Done."
    assert captured[:4] == ["modelscope-stage", str(tmp_path), "--primitive-id", "P03"]
    assert "--refresh-inspection" in captured
    assert captured.count("--mocap-file") == 2
    assert captured.count("--rrd-file") == 2
    assert "--include-rrd" not in captured
    assert "--raw-video" not in captured
    assert "--dataset-root" not in captured
    assert "--proxy-height" not in captured
    assert "--proxy-crf" not in captured
    assert "--aligned-intersection" in captured
    assert captured[captured.index("--ratio") + 1] == "auto"
    assert captured[captured.index("--offset") + 1] == "-2"
    assert captured[captured.index("--inspection-mocap-ratio") + 1] == "4"


def test_web_modelscope_stage_requires_mocap_selection(tmp_path, monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(web_app, "stream_cli_command", fake_cli_stream(captured))

    output = collect_stream(
        web_app.stage_modelscope_data(
            str(tmp_path),
            "segment1",
            "P03",
            False,
            [],
            [],
            False,
            "auto",
            0,
        )
    )

    assert "No Mocap files selected" in output
    assert "未选择 Mocap 文件" in output
    assert captured == []


def test_modelscope_intersection_defaults_follow_rrd_alignment() -> None:
    assert web_app.rrd_alignment_defaults(" 8 ", -3.0) == ("8", -3)
    assert web_app.rrd_alignment_defaults("", 5) == ("auto", 5)


def test_web_modelscope_upload_never_passes_token_on_command_line(tmp_path, monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(web_app, "stream_cli_command", fake_cli_stream(captured))

    output = collect_stream(
        web_app.upload_modelscope_data(
            str(tmp_path),
            "owner/egomocap",
            "master",
            True,
            6,
        )
    )

    assert output == "Done."
    assert captured[0] == "modelscope-upload"
    assert captured[1] == str(tmp_path.parent / "_modelscope_dataset")
    assert captured[captured.index("--repo-id") + 1] == "owner/egomocap"
    assert "--create-if-missing" not in captured
    assert "--visibility" not in captured
    assert "--license" not in captured
    assert "--no-cache" not in captured
    assert all("token" not in item.lower() for item in captured)


def test_web_modelscope_upload_omits_blank_repo_id_for_env_fallback(tmp_path, monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(web_app, "stream_cli_command", fake_cli_stream(captured))

    output = collect_stream(
        web_app.upload_modelscope_data(str(tmp_path), "", "master", True, 4)
    )

    assert output == "Done."
    assert "--repo-id" not in captured
