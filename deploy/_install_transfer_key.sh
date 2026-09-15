#!/usr/bin/env bash
# Install the Transfer-scoped CDP key from the JSON the portal hands you.
#
# Takes the file rather than a pasted string on purpose: an argument or a
# prompt would land in shell history, in the process list, or in a chat
# transcript, and this is the one credential in the system that can move money
# OFF the exchange. The source file is shredded once it is in place.
#
#   scp cdp_api_key.json root@45.33.97.27:/root/
#   bash deploy/_install_transfer_key.sh /root/cdp_api_key.json
set -euo pipefail

APP=/opt/eth-trading-agent
ENV_FILE="$APP/.env"
SRC="${1:-}"

if [[ -z "$SRC" || ! -f "$SRC" ]]; then
  echo "usage: $0 /path/to/cdp_api_key.json" >&2
  exit 1
fi

command -v jq >/dev/null || { echo "installing jq"; apt-get install -y -qq jq; }

NAME=$(jq -r '.name // .id // empty' "$SRC")
# .env holds one line per value, so real newlines are stored escaped; config.py
# turns them back (private_key.replace("\\n", "\n")) before signing.
KEY=$(jq -r '.privateKey // .private_key // empty' "$SRC" | sed -z 's/\n/\\n/g')

if [[ -z "$NAME" || -z "$KEY" ]]; then
  echo "could not find name/privateKey in $SRC" >&2
  exit 1
fi

if ! grep -q "EC PRIVATE KEY" <<<"$KEY"; then
  echo "WARNING: this does not look like an ECDSA key." >&2
  echo "The signer uses ES256; an Ed25519 key will not work as-is." >&2
fi

set_var() {                       # set_var KEY VALUE — replace or append
  local k="$1" v="$2"
  if grep -q "^${k}=" "$ENV_FILE"; then
    # Value goes in via a file to keep the secret out of the argument list.
    printf '%s' "$v" > /tmp/.v.$$
    python3 - "$ENV_FILE" "$k" /tmp/.v.$$ <<'PY'
import sys
env, key, valfile = sys.argv[1], sys.argv[2], sys.argv[3]
val = open(valfile).read()
lines = open(env).read().splitlines(True)
with open(env, "w") as fh:
    for line in lines:
        fh.write(f'{key}="{val}"\n' if line.startswith(key + "=") else line)
PY
    shred -u /tmp/.v.$$ 2>/dev/null || rm -f /tmp/.v.$$
  else
    printf '%s="%s"\n' "$k" "$v" >> "$ENV_FILE"
  fi
}

cp -a "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
set_var COINBASE_TRANSFER_KEY_NAME "$NAME"
set_var COINBASE_TRANSFER_PRIVATE_KEY "$KEY"

chown ethagent:ethagent "$ENV_FILE"
chmod 600 "$ENV_FILE"

shred -u "$SRC" 2>/dev/null || rm -f "$SRC"

echo "installed. key id: $(sed 's#.*/##' <<<"$NAME" | cut -c1-4)***$(sed 's#.*/##' <<<"$NAME" | tail -c 5)"
echo "source file shredded."
echo
echo "now verify with:  sudo -u ethagent $APP/.venv/bin/python deploy/_check_transfer_key.py"
