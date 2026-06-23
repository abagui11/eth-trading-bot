#!/usr/bin/env bash
# Pull latest code and restart the agent (run on VPS after git push).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/eth-trading-agent}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/update.sh"
  exit 1
fi

APP_USER=$(stat -c '%U' "$APP_DIR")
sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
systemctl restart eth-agent
echo "Updated and restarted. journalctl -u eth-agent -f"
