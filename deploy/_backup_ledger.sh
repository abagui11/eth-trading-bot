#!/usr/bin/env bash
# Consistent snapshot of ledger.db before a destructive edit.
set -euo pipefail
cd /opt/eth-trading-agent

DEST="/root/ledger_backup_$(date -u +%Y%m%dT%H%M%SZ).db"
sudo -u ethagent sqlite3 ledger.db ".backup '/tmp/_ledger_snap.db'"
mv /tmp/_ledger_snap.db "$DEST"
ls -la "$DEST"
sqlite3 "$DEST" 'PRAGMA integrity_check;'
echo "backup: $DEST"
