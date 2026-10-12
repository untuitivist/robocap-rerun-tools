import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("launcher", ["start_web.bat", "start_web.sh"])
def test_git_failure_continues_to_local_startup(tmp_path: Path, launcher: str) -> None:
    if not shutil.which("git") or not shutil.which("uv"):
        pytest.skip("Git and uv are required for launcher integration tests")
    if launcher.endswith(".bat"):
        if os.name != "nt":
            pytest.skip("Windows batch launcher")
        command = ["cmd.exe", "/d", "/c", launcher]
    else:
        shell = shutil.which("sh")
        if not shell and os.name == "nt":
            candidate = Path(shutil.which("git")).parent.parent / "bin" / "sh.exe"
            if candidate.is_file():
                shell = str(candidate)
        if not shell:
            pytest.skip("POSIX shell unavailable")
        command = [shell, launcher]
    root = Path(__file__).resolve().parents[1]
    shutil.copyfile(root / launcher, tmp_path / launcher)
    # A fresh repository has no remote: pull fails immediately without network access.
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    env = os.environ.copy()
    env.pop("ROBOCAP_LAUNCHER_UPDATED", None)
    env["ROBOCAP_SKIP_SYNC"] = "1"
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, check=False)
    output = result.stdout.decode("utf-8", errors="replace")
    assert "WARNING: Git update failed." in output
    assert "Robocap Rerun Tools web launcher" in output
    assert "uv sync completed, but the CLI was not created:" in output
    assert result.returncode == 1  # Missing local CLI, not the earlier Git failure.
