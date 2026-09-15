#!/usr/bin/env bash
# Set the pool admin id and grandfather the pre-pool access list in.
set -euo pipefail
cd /opt/eth-trading-agent

ADMIN_ID="${1:?usage: _flip_pool.sh <admin_telegram_id>}"
PY=/opt/eth-trading-agent/.venv/bin/python

cp -a .env ".env.bak-poolflip-$(date -u +%Y%m%dT%H%M%SZ)"

if grep -q '^POOL_ADMIN_TELEGRAM_IDS=' .env; then
  sed -i "s/^POOL_ADMIN_TELEGRAM_IDS=.*/POOL_ADMIN_TELEGRAM_IDS=$ADMIN_ID/" .env
else
  printf '\n# Who receives Admit + Credit cards and may run /credit //debit.\nPOOL_ADMIN_TELEGRAM_IDS=%s\n' "$ADMIN_ID" >> .env
fi
grep '^POOL_ADMIN_TELEGRAM_IDS=' .env

echo
echo "==> Bootstrap dry run"
sudo -u ethagent "$PY" deploy/pool_bootstrap.py --dry-run

echo
echo "==> Applying"
sudo -u ethagent "$PY" deploy/pool_bootstrap.py --apply
