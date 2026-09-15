#!/usr/bin/env bash
# Reset the VPS checkout onto origin/main after verifying nothing on the box is
# unique to it. Preflight already confirmed: every tracked modification is a
# subset of what is being pulled, and every colliding untracked file is either
# byte-identical or an older copy of the incoming one.
set -euo pipefail
cd /opt/eth-trading-agent

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="/root/preflip-backup-$STAMP.tar.gz"

echo "==> Backing up working tree to $BACKUP"
tar czf "$BACKUP" \
  --exclude=.venv --exclude=.git --exclude=.cache --exclude=backups \
  --exclude='*.db' --exclude='*.db.bak*' --exclude=charts \
  -C /opt eth-trading-agent
ls -lh "$BACKUP"

echo "==> Discarding tracked modifications (CRLF noise + already-committed work)"
sudo -u ethagent git checkout -- .

echo "==> Clearing untracked files the pull needs to place"
git diff --name-only --diff-filter=A HEAD origin/main | sort > /tmp/_incoming.txt
git ls-files --others --exclude-standard | sort > /tmp/_untracked.txt
comm -12 /tmp/_incoming.txt /tmp/_untracked.txt > /tmp/_collisions.txt
while read -r f; do
  [ -z "$f" ] && continue
  echo "    rm $f"
  rm -f "$f"
done < /tmp/_collisions.txt

echo "==> Pulling"
sudo -u ethagent git pull --ff-only
git log --oneline -1
git status --short | head -20
