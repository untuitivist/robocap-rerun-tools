import os
import shutil
import subprocess
import sys
import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("launcher", ["start_web.bat", "start_web.sh"])
def test_missing_python_continues_to_local_startup(tmp_path: Path, launcher: str) -> None:
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
    env.pop("ROBOCAP_SKIP_UPDATE", None)
    env["ROBOCAP_SKIP_SYNC"] = "1"
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, check=False)
    output = result.stdout.decode("utf-8", errors="replace")
    assert "Skipping update for initial setup." in output
    assert "Robocap Rerun Tools web launcher" in output
    assert "uv sync completed, but the CLI was not created:" in output
    assert result.returncode == 1  # Missing local CLI, not the earlier Git failure.


def test_network_timeout_terminates_process(capsys) -> None:
    path = Path(__file__).resolve().parents[1] / "scripts" / "startup_update.py"
    spec = importlib.util.spec_from_file_location("startup_update", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.fetch_with_timeout(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        os.environ.copy(), seconds=0.2,
    )
    assert result == 1
    assert "timed out" in capsys.readouterr().out


def test_no_upstream_returns_failure_without_network(tmp_path: Path) -> None:
    if not shutil.which("git"):
        pytest.skip("Git required")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    helper = Path(__file__).resolve().parents[1] / "scripts" / "startup_update.py"
    result = subprocess.run([sys.executable, str(helper)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "No configured upstream" in result.stdout


@pytest.mark.parametrize(("command", "code", "message"), [
    ("exit 0", 0, "uv installer completed"),
    ("exit 7", 1, "installation failed"),
    ("Start-Sleep -Seconds 60", 1, "installation timed out"),
])
def test_uv_installer_outcomes(command: str, code: int, message: str) -> None:
    shell = shutil.which("powershell")
    if os.name != "nt" or not shell:
        pytest.skip("Windows PowerShell required")
    helper = Path(__file__).resolve().parents[1] / "scripts" / "install_uv.ps1"
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "ByPass", "-File", str(helper),
         "-TimeoutSeconds", "2", "-InstallerCommand", command],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout
    if code:
        assert "VPN/proxy" in result.stdout


@pytest.mark.parametrize(("command", "code", "message"), [
    ("exit 0", 0, "uv installer completed"),
    ("exit 7", 1, "installation failed"),
    ("sleep 60", 1, "installation timed out"),
])
def test_shell_uv_installer_outcomes(command: str, code: int, message: str) -> None:
    shell = shutil.which("sh")
    if not shell and os.name == "nt" and shutil.which("git"):
        candidate = Path(shutil.which("git")).parent.parent / "bin" / "sh.exe"
        if candidate.is_file():
            shell = str(candidate)
    if not shell:
        pytest.skip("POSIX shell required")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [shell, "scripts/install_uv.sh", "1", command], cwd=root,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout
    if code:
        assert "VPN/proxy" in result.stdout
