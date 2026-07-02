#!/usr/bin/env bash
# Install or refresh eth-dashboard systemd unit (run on VPS as root).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/eth-trading-agent}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install_dashboard.sh"
  exit 1
fi

APP_USER=$(stat -c '%U' "$APP_DIR")

echo "==> Installing eth-dashboard.service (user=$APP_USER, dir=$APP_DIR)"
sed "s|/opt/eth-trading-agent|$APP_DIR|g; s|User=ethagent|User=$APP_USER|g; s|Group=ethagent|Group=$APP_USER|g" \
  "$APP_DIR/deploy/eth-dashboard.service" > /etc/systemd/system/eth-dashboard.service

systemctl daemon-reload
systemctl enable eth-dashboard
systemctl restart eth-dashboard
systemctl status eth-dashboard --no-pager

echo ""
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
echo "Logs: journalctl -u eth-dashboard -f"
