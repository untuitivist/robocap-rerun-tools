#!/usr/bin/env sh

set -u

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
CLI="$REPO_DIR/.venv/bin/robocap-rerun"
PYTHON_EXE="$REPO_DIR/.venv/bin/python"

NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}${no_proxy:+,$no_proxy}"
no_proxy="$NO_PROXY"
export NO_PROXY no_proxy

cd "$REPO_DIR" || exit 1

if ! command -v uv >/dev/null 2>&1; then
  echo "uv was not found on PATH."
  echo "Install uv first, then run ./start_web.sh again."
  exit 1
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
  if ! uv sync --extra web; then
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
