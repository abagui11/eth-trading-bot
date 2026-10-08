#!/bin/bash
set -euo pipefail
HUB=/opt/eth-trading-agent/.env
BOT=/opt/kalshi-15m-bot/.env
KEY_PATH=$(grep -E '^KALSHI_PRIVATE_KEY_PATH=' "$BOT" | head -1 | cut -d= -f2-)
case "$KEY_PATH" in
  /*) ;;
  *) KEY_PATH="/opt/kalshi-15m-bot/$KEY_PATH" ;;
esac
test -f "$KEY_PATH"
mkdir -p /opt/eth-trading-agent/secrets
HUB_KEY=/opt/eth-trading-agent/secrets/kalshi.pem
cp -a "$KEY_PATH" "$HUB_KEY"
chown ethagent:ethagent "$HUB_KEY"
chmod 600 "$HUB_KEY"

python3 - <<'PY'
from pathlib import Path
hub = Path("/opt/eth-trading-agent/.env")
bot = Path("/opt/kalshi-15m-bot/.env")
bot_vals = {}
for line in bot.read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, _, v = line.partition("=")
        bot_vals[k.strip()] = v.strip()
updates = {
    "KALSHI_API_KEY_ID": bot_vals["KALSHI_API_KEY_ID"],
    "KALSHI_PRIVATE_KEY_PATH": "/opt/eth-trading-agent/secrets/kalshi.pem",
}
if bot_vals.get("KALSHI_API_BASE"):
    updates["KALSHI_API_BASE"] = bot_vals["KALSHI_API_BASE"]
lines = hub.read_text().splitlines()
keys_seen = set()
out = []
for line in lines:
    if "=" in line and not line.strip().startswith("#"):
        k = line.split("=", 1)[0].strip()
        if k in updates:
            out.append(f"{k}={updates[k]}")
            keys_seen.add(k)
            continue
    out.append(line)
for k, v in updates.items():
    if k not in keys_seen:
        out.append(f"{k}={v}")
hub.write_text("\n".join(out) + "\n")
print("wrote hub env keys:", ", ".join(updates))
PY

grep -E '^KALSHI_API_KEY_ID=|^KALSHI_PRIVATE_KEY_PATH=|^KALSHI_API_BASE=' "$HUB" | sed 's/=.*/=***/'
systemctl restart eth-agent
sleep 2
systemctl is-active eth-agent
cd /opt/eth-trading-agent
sudo -u ethagent .venv/bin/python - <<'PY'
import kalshi_gateway, strategy_catalog
print("configured", kalshi_gateway.configured())
try:
    print("balance_ok", round(kalshi_gateway.get_balance_usd(), 2))
except Exception as e:
    print("balance_err", type(e).__name__, str(e)[:160])
print("kalshi_wick_executable", strategy_catalog.is_executable("kalshi_wick"))
print("kalshi_reversal_executable", strategy_catalog.is_executable("kalshi_reversal"))
PY
