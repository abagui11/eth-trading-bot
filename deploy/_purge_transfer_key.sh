#!/usr/bin/env bash
# Remove the transfer key from .env and shred the backups around it.
#
# Used when a key has to be treated as compromised. "It currently returns 401"
# is not a security property -- a key that cannot authenticate today can be
# re-enabled, so the secret is removed rather than left inert.
set -euo pipefail

APP=/opt/eth-trading-agent
cd "$APP"

python3 - <<'PY'
path = "/opt/eth-trading-agent/.env"
out = []
for line in open(path).read().splitlines(True):
    if line.startswith("COINBASE_TRANSFER_KEY_NAME="):
        out.append('COINBASE_TRANSFER_KEY_NAME=""\n')
    elif line.startswith("COINBASE_TRANSFER_PRIVATE_KEY="):
        out.append('COINBASE_TRANSFER_PRIVATE_KEY=""\n')
    else:
        out.append(line)
open(path, "w").write("".join(out))
PY

# Backups were taken before the key was written, but they accumulate secrets
# over time and nothing reads them.
for f in .env.bak.*; do
  [ -e "$f" ] || continue
  shred -u "$f" 2>/dev/null || rm -f "$f"
done

rm -f /root/cdp_key.json /root/cdp_api_key*.json 2>/dev/null || true

chown ethagent:ethagent .env
chmod 600 .env

echo "transfer key entries cleared:"
grep -E '^COINBASE_TRANSFER' .env || echo "  (none present)"
echo
echo "leftover key files in /root:"
ls /root/*.json 2>/dev/null || echo "  (none)"
