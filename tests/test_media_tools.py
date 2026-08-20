from pathlib import Path

from robocap_rerun_tools import media_tools


def test_activate_media_tools_falls_back_to_complete_system_pair(monkeypatch) -> None:
    paths = {
        "ffmpeg": "C:/system/ffmpeg.exe",
        "ffprobe": "C:/system/ffprobe.exe",
    }
    monkeypatch.setattr(media_tools.shutil, "which", paths.get)
    monkeypatch.setattr(
        media_tools,
        "bundled_media_tools",
        lambda: media_tools.MediaTools(None, None, "unsupported platform wheel"),
    )

    active = media_tools.activate_media_tools()

    assert active == media_tools.MediaTools(
        "C:/system/ffmpeg.exe", "C:/system/ffprobe.exe", "system PATH"
    )


def test_activate_media_tools_exposes_uv_bundled_pair(monkeypatch, tmp_path: Path) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"")
    ffprobe.write_bytes(b"")
    monkeypatch.setattr(media_tools.shutil, "which", lambda name: f"C:/system/{name}.exe")
    monkeypatch.setattr(
        media_tools,
        "bundled_media_tools",
        lambda: media_tools.MediaTools(str(ffmpeg), str(ffprobe), "uv bundled"),
    )
    monkeypatch.setenv("PATH", "C:/other-tools")

    active = media_tools.activate_media_tools()

    assert active.source == "uv bundled"
    assert active.ffmpeg == str(ffmpeg)
    assert active.ffprobe == str(ffprobe)
    assert Path(media_tools.os.environ["PATH"].split(media_tools.os.pathsep)[0]) == tmp_path
