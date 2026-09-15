#!/usr/bin/env bash
# Pull, set POOL_DEPOSIT_ADDRESS, restart, and show what /deposit will say.
set -euo pipefail
cd /opt/eth-trading-agent

ADDRESS="${1:?usage: _set_deposit_address.sh <0x address>}"
PY=/opt/eth-trading-agent/.venv/bin/python

sudo -u ethagent git pull --ff-only
cp -a .env ".env.bak-deposit-$(date -u +%Y%m%dT%H%M%SZ)"

if grep -q '^POOL_DEPOSIT_ADDRESS=' .env; then
  sed -i "s|^POOL_DEPOSIT_ADDRESS=.*|POOL_DEPOSIT_ADDRESS=$ADDRESS|" .env
else
  printf '\n# Tester deposit address. Shared with the yield wallet: a txid is\n# required on /deposit, and deposits are swept to Coinbase before credit.\nPOOL_DEPOSIT_ADDRESS=%s\n' "$ADDRESS" >> .env
fi
grep '^POOL_DEPOSIT_ADDRESS=' .env

systemctl restart eth-agent eth-dashboard
sleep 5
systemctl is-active eth-agent eth-dashboard

echo
echo "=== what a tester sees on /deposit ==="
sudo -u ethagent "$PY" - <<'PY'
import config, pool, telegram_ui

print(telegram_ui.format_deposit_instructions())
print("\n--- controls ---")
print("address configured  ", bool(config.POOL_DEPOSIT_ADDRESS))
print("chain label         ", config.POOL_DEPOSIT_CHAIN or "(unset -> confirm with admin)")
print("hash required       ", pool.request_deposit(1, 600.0).get("reason") == "not_approved" or "n/a")
print("unswept tester cash $%.2f" % pool.pending_inbound_usd())
PY
