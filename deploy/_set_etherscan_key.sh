#!/usr/bin/env bash
# Install ETHERSCAN_API_KEY into .env.
#
# Unlike the CDP keys this one only reads public chain data and cannot move
# funds, so it is set from an argument rather than a shredded file. The .env
# is backed up first and the result is checked, because a half-written .env
# takes the whole bot down.
#
#   bash deploy/_set_etherscan_key.sh <key>
set -euo pipefail

APP=/opt/eth-trading-agent
ENV_FILE="$APP/.env"
KEY="${1:-}"

if [[ -z "$KEY" ]]; then
  echo "usage: $0 <etherscan_api_key>" >&2
  exit 1
fi

BACKUP="$ENV_FILE.bak.$(date +%s)"
cp -a "$ENV_FILE" "$BACKUP"
BEFORE=$(wc -l < "$ENV_FILE")

grep -v '^ETHERSCAN_API_KEY=' "$BACKUP" > "$ENV_FILE"
printf 'ETHERSCAN_API_KEY="%s"\n' "$KEY" >> "$ENV_FILE"

AFTER=$(wc -l < "$ENV_FILE")
if (( AFTER < BEFORE )); then
  cp -a "$BACKUP" "$ENV_FILE"
  echo "refused: line count fell $BEFORE -> $AFTER, restored backup" >&2
  exit 1
fi

chown ethagent:ethagent "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo "ETHERSCAN_API_KEY set (${KEY:0:4}***${KEY: -4}), $AFTER lines"
