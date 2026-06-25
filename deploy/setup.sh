#!/usr/bin/env bash
# Bootstrap the ETH trading agent on a fresh Ubuntu/Debian VPS.
# Run as root: curl -sSL <raw-url>/setup.sh | bash
# Or from a cloned repo: sudo bash deploy/setup.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/eth-trading-agent}"
APP_USER="${APP_USER:-ethagent}"
REPO_URL="${REPO_URL:-}"

echo "==> ETH Trading Agent — VPS setup"
echo "    App dir:  $APP_DIR"
echo "    App user: $APP_USER"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/setup.sh"
  exit 1
fi

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git

if ! id "$APP_USER" &>/dev/null; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"

# Install app code: clone from GitHub or copy from current directory (dev deploy).
if [[ -n "$REPO_URL" ]]; then
  if [[ ! -d "$APP_DIR/.git" ]]; then
    sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
  else
    echo "==> Pulling latest in $APP_DIR"
    sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
  fi
elif [[ -f "$(dirname "$0")/../scheduler.py" ]]; then
  echo "==> Copying repo from $(dirname "$0")/.."
  rsync -a --exclude '.env' --exclude '.venv' --exclude 'charts' --exclude '*.db' \
    --exclude '__pycache__' "$(dirname "$0")/../" "$APP_DIR/"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
else
  echo "Set REPO_URL=https://github.com/abagui11/eth-trading-bot.git or run from a cloned repo."
  exit 1
fi

echo "==> Python venv + dependencies"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo ""
  echo "!!  Edit secrets before starting:"
  echo "    nano $APP_DIR/.env"
  echo ""
fi

mkdir -p "$APP_DIR/charts"
chown "$APP_USER:$APP_USER" "$APP_DIR/charts"

echo "==> Installing systemd service"
sed "s|/opt/eth-trading-agent|$APP_DIR|g; s|User=ethagent|User=$APP_USER|g; s|Group=ethagent|Group=$APP_USER|g" \
  "$APP_DIR/deploy/eth-agent.service" > /etc/systemd/system/eth-agent.service

systemctl daemon-reload
systemctl enable eth-agent

echo ""
echo "==> Setup complete"
echo ""
echo "Next steps:"
echo "  1. nano $APP_DIR/.env          # add API keys"
echo "  2. sudo systemctl start eth-agent"
echo "  3. sudo systemctl status eth-agent"
echo "  4. sudo journalctl -u eth-agent -f   # live logs"
echo "  5. sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/status.py"
echo "  6. sudo -u $APP_USER $APP_DIR/.venv/bin/python $APP_DIR/subscribers.py  # pending users"
echo "  7. See deploy/CLOUD.md for full ops + onboarding guide"
echo ""
