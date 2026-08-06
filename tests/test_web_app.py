import os
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


def test_web_inspect_prints_generated_markdown_report(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "_artifacts" / "segment1" / "inspection" / "frame_rate_report.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        "# Robocap/NOKOV inspection\n\n| file | kind |\n|---|---|\n| `third.mp4` | video |\n",
        encoding="utf-8",
    )
    captured = []
    monkeypatch.setattr(
        web_app,
        "run_cli_result",
        lambda args: (
            captured.extend(args) or (0, f"Wrote inspection reports to {report_path.parent}")
        ),
    )

    output = web_app.inspect_session(str(tmp_path), "segment1")

    assert captured == ["inspect", str(tmp_path), "--segment", "segment1"]
    assert "Wrote inspection reports" in output
    assert f"Report: `{report_path}`" in output
    assert "# Robocap/NOKOV inspection" in output
    assert "| `third.mp4` | video |" in output


def test_web_inspect_does_not_print_stale_report_after_failure(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "_artifacts" / "segment1" / "inspection" / "frame_rate_report.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("# stale report\n", encoding="utf-8")
    monkeypatch.setattr(web_app, "run_cli_result", lambda _args: (2, "inspection failed"))

    output = web_app.inspect_session(str(tmp_path), "segment1")

    assert "inspection failed" in output
    assert "Command failed with exit code 2" in output
    assert "stale report" not in output


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
