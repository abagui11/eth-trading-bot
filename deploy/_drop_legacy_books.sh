#!/usr/bin/env bash
# Retire the four pre-pool demo books. Dry-runs unless passed --delete.
set -euo pipefail
cd /opt/eth-trading-agent

IDS="82655100 1547696638 2037245798 5779224281"
ARG="${1:-}"

for id in $IDS; do
  echo "=== $id ==="
  sudo -u ethagent .venv/bin/python deploy/_drop_demo_book.py "$id" $ARG
  echo
done

echo "--- user_accounts remaining ---"
sudo -u ethagent sqlite3 -header -column ledger.db 'SELECT * FROM user_accounts;'
