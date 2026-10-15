#!/usr/bin/env sh

set -u

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
CLI="$REPO_DIR/.venv/bin/robocap-rerun"
PYTHON_EXE="$REPO_DIR/.venv/bin/python"

NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}${no_proxy:+,$no_proxy}"
no_proxy="$NO_PROXY"
export NO_PROXY no_proxy

cd "$REPO_DIR" || exit 1

if [ "${ROBOCAP_LAUNCHER_UPDATED:-0}" != "1" ] && [ "${ROBOCAP_SKIP_UPDATE:-0}" != "1" ]; then
  if ! command -v git >/dev/null 2>&1; then
    echo "WARNING: Git was not found. Skipping update and starting with local code."
  elif [ ! -x "$PYTHON_EXE" ]; then
    echo "WARNING: Local Python is not ready. Skipping update for initial setup."
  else
    if "$PYTHON_EXE" "$REPO_DIR/scripts/startup_update.py"; then
      # Reload the launcher in case the pull updated this file.
      export ROBOCAP_LAUNCHER_UPDATED=1
      exec sh "$REPO_DIR/start_web.sh"
    else
      echo "WARNING: Git update failed. Skipping update and starting with local code."
    fi
  fi
fi
unset ROBOCAP_LAUNCHER_UPDATED

if ! command -v uv >/dev/null 2>&1; then
  PATH="${UV_INSTALL_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}:$PATH"
  export PATH
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "uv was not found. Installing uv automatically..."
  sh "$REPO_DIR/scripts/install_uv.sh" || exit 1
  if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv is still not on PATH. Open a new terminal and run ./start_web.sh again."
    exit 1
  fi
fi

echo "============================================================"
echo "Robocap Rerun Tools web launcher"
echo "Repo: $REPO_DIR"
echo "Time: $(date '+%Y-%m-%d %H:%M:%S %z')"
echo "uv:"
uv --version
echo "Python:"
if [ -x "$PYTHON_EXE" ]; then
  "$PYTHON_EXE" --version
fi
echo "CLI: $CLI"
echo "============================================================"

if [ "${ROBOCAP_SKIP_SYNC:-0}" != "1" ]; then
  echo "Synchronizing Python, Web, FFmpeg, and FFprobe dependencies with uv..."
  if ! uv sync --python 3.12 --extra web; then
    echo "uv sync failed."
    exit 1
  fi
fi

if [ ! -x "$CLI" ]; then
  echo "uv sync completed, but the CLI was not created: $CLI"
  exit 1
fi

"$CLI" web --open
EXIT_CODE=$?
echo "Web process exited with code $EXIT_CODE."
exit "$EXIT_CODE"
