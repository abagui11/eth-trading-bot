#!/usr/bin/env bash
# Point POOL_DEPOSIT_ADDRESS at a Coinbase deposit address, after verifying
# Coinbase actually generated it. A typo here sends tester USDC somewhere
# nobody controls and there is no undo, so the check is not optional.
#
#   deploy/_set_deposit_address.sh 0x<address> "Ethereum mainnet"
set -euo pipefail
cd /opt/eth-trading-agent

ADDRESS="${1:?usage: _set_deposit_address.sh <0x address> [chain label]}"
CHAIN="${2:-}"

sudo -u ethagent git pull --ff-only

echo "=== verifying the address against Coinbase ==="
sudo -u ethagent .venv/bin/python deploy/_check_deposit_address.py "$ADDRESS"

cp -a .env ".env.bak-deposit-$(date -u +%Y%m%dT%H%M%SZ)"

set_var() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

set_var POOL_DEPOSIT_ADDRESS "$ADDRESS"
[ -n "$CHAIN" ] && set_var POOL_DEPOSIT_CHAIN "$CHAIN"
grep -E '^POOL_DEPOSIT' .env

systemctl restart eth-agent eth-dashboard
sleep 8
systemctl is-active eth-agent eth-dashboard

echo
echo "=== what a tester sees ==="
sudo -u ethagent .venv/bin/python deploy/_show_wallet_copy.py
