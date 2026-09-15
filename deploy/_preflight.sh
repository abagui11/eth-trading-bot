#!/usr/bin/env bash
# Read-only preflight before resetting the VPS checkout onto origin/main.
set -uo pipefail
cd /opt/eth-trading-agent

echo "=== live data files: tracked? blank means safely gitignored ==="
git ls-files --error-unmatch .env ledger.db ohlc.db 2>/dev/null

echo
echo "=== untracked files the pull wants to add ==="
git diff --name-only --diff-filter=A HEAD origin/main | sort > /tmp/_incoming.txt
git ls-files --others --exclude-standard | sort > /tmp/_untracked.txt
comm -12 /tmp/_incoming.txt /tmp/_untracked.txt | tee /tmp/_collisions.txt

echo
echo "=== of those, which differ from the incoming version, ignoring CRLF ==="
while read -r f; do
  [ -z "$f" ] && continue
  if git show "origin/main:$f" | diff -q --strip-trailing-cr - "$f" >/dev/null 2>&1; then
    echo "  same     $f"
  else
    echo "  DIFFERS  $f"
  fi
done < /tmp/_collisions.txt

echo
echo "=== tracked modifications unique to the server, ignoring CRLF ==="
git diff --ignore-cr-at-eol --stat origin/main -- $(git diff --name-only | tr '\n' ' ')
