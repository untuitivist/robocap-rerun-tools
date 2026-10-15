#!/usr/bin/env sh

set -u

install_timeout=${1:-120}
installer_command=${2:-'
if command -v curl >/dev/null 2>&1; then
  installer=$(curl --fail --silent --show-error --location --connect-timeout 15 https://astral.sh/uv/install.sh) || exit 1
elif command -v wget >/dev/null 2>&1; then
  installer=$(wget -qO- --timeout=15 https://astral.sh/uv/install.sh) || exit 1
else
  echo "ERROR: Install curl or wget first."
  exit 1
fi
sh -c "$installer"
'}

if ! command -v timeout >/dev/null 2>&1; then
  echo "ERROR: Install coreutils (timeout command) before installing uv automatically."
  exit 1
fi

echo "Installing uv (timeout: $install_timeout seconds)..."
# Bound both the script download and the installer, including its child processes.
timeout -k 5 "$install_timeout" sh -c "$installer_command"
result=$?
if [ "$result" -eq 0 ]; then
  echo "uv installer completed."
  exit 0
fi
if [ "$result" -eq 124 ] || [ "$result" -eq 137 ]; then
  echo "ERROR: uv installation timed out after $install_timeout seconds."
else
  echo "ERROR: uv installation failed (exit code: $result)."
fi
echo "Enable your VPN/proxy, then run ./start_web.sh again to download uv."
exit 1
