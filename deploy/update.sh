#!/usr/bin/env bash
# Pull latest code and restart the agent (run on VPS after git push).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/eth-trading-agent}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/update.sh"
  exit 1
fi

APP_USER=$(stat -c '%U' "$APP_DIR")
PY="$APP_DIR/.venv/bin/python"

sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# Install dashboard unit first (existing VPS may never have had setup.sh re-run).
if [[ ! -f /etc/systemd/system/eth-dashboard.service ]]; then
  echo "==> Installing eth-dashboard.service"
  bash "$APP_DIR/deploy/install_dashboard.sh"
else
  systemctl daemon-reload
fi

# Must run from APP_DIR so `import audit` resolves.
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$PY' -c \"import audit, ledger, paper; audit.init_db(); ledger.init_db(); paper.init_db()\""

systemctl restart eth-agent
systemctl restart eth-dashboard
echo "Updated and restarted eth-agent + eth-dashboard."
echo "  journalctl -u eth-agent -f"
echo "  journalctl -u eth-dashboard -f"
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
