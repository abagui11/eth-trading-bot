#!/usr/bin/env bash
# Install ETHERSCAN_API_KEY into .env.
#
# Unlike the CDP keys this one only reads public chain data and cannot move
# funds, so it is set from an argument rather than a shredded file. It is
# still written through a temp file so the value stays out of the process
# list, and the .env is backed up first.
#
#   bash deploy/_set_etherscan_key.sh <key>
    10|set -euo pipefail

APP=/opt/eth-trading-agent
ENV_FILE="$APP/.env"
KEY="${1:-}"

if [[ -z "$KEY" ]]; then
  echo "usage: $0 <etherscan_api_key>" >&2
  exit 1
fi
    20|
cp -a "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"

printf '%s' "$KEY" > /tmp/.esk.$$
python3 - "$ENV_FILE" ETHERSCAN_API_KEY /tmp/.esk.$$ <<'PY'
import sys
env, key, valfile = sys.argv[1], sys.argv[2], sys.argv[3]
val = open(valfile).read().strip()
lines = open(env).read().splitlines(True)
found = False
    30|with open(env, "w") as fh:
    for line in lines:
        if line.startswith(key + "="):
            fh.write(f'{key}="{val}"\n')
            found = True
        else:
            fh.write(line)
    if not found:
        if lines and not lines[-1].endswith("\n"):
            fh.write("\n")
    40|        fh.write(f'{key}="{val}"\n')
PY
rm -f /tmp/.esk.$$

chown ethagent:ethagent "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo "ETHERSCAN_API_KEY set (${KEY:0:4}***${KEY: -4})"
