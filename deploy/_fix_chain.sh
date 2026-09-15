#!/usr/bin/env bash
set -euo pipefail
cd /opt/eth-trading-agent
sed -i '/^POOL_DEPOSIT_CHAIN=/d' .env
echo 'POOL_DEPOSIT_CHAIN=Ethereum mainnet (ERC-20)' >> .env
grep -E '^POOL_DEPOSIT' .env
systemctl restart eth-agent eth-dashboard
sleep 10
systemctl is-active eth-agent eth-dashboard
echo "--- as configured ---"
sudo -u ethagent .venv/bin/python -c 'import config; print(repr(config.POOL_DEPOSIT_ADDRESS)); print(repr(config.POOL_DEPOSIT_CHAIN))'
