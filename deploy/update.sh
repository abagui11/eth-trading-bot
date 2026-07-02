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

# Migrate ledger.db (e.g. audit_verdicts.score columns) before services restart.
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -c "import audit, ledger, paper; audit.init_db(); ledger.init_db(); paper.init_db()"

# Install dashboard unit on existing VPS installs that pre-date eth-dashboard.service.
if [[ ! -f /etc/systemd/system/eth-dashboard.service ]]; then
  echo "==> Installing eth-dashboard.service"
  sed "s|/opt/eth-trading-agent|$APP_DIR|g; s|User=ethagent|User=$APP_USER|g; s|Group=ethagent|Group=$APP_USER|g" \
    "$APP_DIR/deploy/eth-dashboard.service" > /etc/systemd/system/eth-dashboard.service
  systemctl daemon-reload
  systemctl enable eth-dashboard
fi

systemctl restart eth-agent
systemctl restart eth-dashboard
echo "Updated and restarted eth-agent + eth-dashboard."
echo "  journalctl -u eth-agent -f"
echo "  journalctl -u eth-dashboard -f"
