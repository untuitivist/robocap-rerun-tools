"""Best-effort, noninteractive Git refresh before starting the local application."""
from __future__ import annotations

import os
import signal
import subprocess
import sys


def fetch_with_timeout(command: list[str], env: dict[str, str], seconds: float = 30) -> int:
    process = subprocess.Popen(command, env=env, start_new_session=os.name != "nt")
    try:
        return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=False, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.poll() is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        print(f"WARNING: Git network update timed out after {seconds:g}s. Starting with local code.", flush=True)
        return 1


def main() -> int:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
           "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10"}
    git = ["git", "-c", "credential.interactive=false", "-c", "merge.autostash=false",
           "-c", "rebase.autostash=false"]
    try:
        upstream = subprocess.run(git + ["rev-parse", "--abbrev-ref", "@{upstream}"],
                                  env=env, capture_output=True, text=True, check=False)
        if upstream.returncode:
            print("WARNING: Git update failed. No configured upstream; starting with local code.", flush=True)
            return 1
        print("Checking for code updates (network timeout: 30s)...", flush=True)
        # Bound only the network operation; never kill a local checkout halfway through.
        if fetch_with_timeout(git + ["fetch"], env):
            print("WARNING: Git update failed. Starting with local code.", flush=True)
            return 1
        return subprocess.call(git + ["merge", "--ff-only", upstream.stdout.strip()], env=env)
    except OSError as exc:
        print(f"WARNING: Git update failed: {exc}. Starting with local code.", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
