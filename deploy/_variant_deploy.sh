#!/usr/bin/env bash
# Install the Eva variant experiment on the server. Idempotent.
set -euo pipefail
APP=/opt/eth-trading-agent
STAGE=/tmp/eva_variants
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BAK="$APP/.bak-variants-$STAMP"

cd "$APP"
mkdir -p "$BAK/dashboard/templates" "$BAK/patterns" "$BAK/deploy"

echo "=== backing up files this deploy overwrites -> $BAK ==="
for f in agent.py bot_config.py ledger.py main.py research.py \
         dashboard/app.py dashboard/templates/index.html \
         deploy/PROJECT_STATE.md deploy/CLOUD.md; do
  [ -f "$f" ] && cp -a "$f" "$BAK/$f" && echo "  saved $f"
done

echo
echo "=== installing ==="
install -o ethagent -g ethagent -m 644 \
  "$STAGE/agent.py" "$STAGE/bot_config.py" "$STAGE/ledger.py" \
  "$STAGE/main.py" "$STAGE/research.py" \
  "$STAGE/eva_variants.py" "$STAGE/eva_day.py" "$STAGE/eva_swing.py" \
  "$STAGE/eva_swing_llm.py" "$STAGE/eva_variants_bridge.py" "$APP/"
install -o ethagent -g ethagent -m 644 "$STAGE/app.py" "$APP/dashboard/app.py"
install -o ethagent -g ethagent -m 644 "$STAGE/index.html" \
  "$APP/dashboard/templates/index.html"
install -o ethagent -g ethagent -m 644 "$STAGE/fvg.py" \
  "$STAGE/structure_shift.py" "$APP/patterns/"
install -o ethagent -g ethagent -m 644 "$STAGE/test_eva_variants.py" \
  "$STAGE/test_ict_detectors.py" "$APP/tests/"
install -o ethagent -g ethagent -m 644 "$STAGE/EVA_VARIANTS_PLAN.md" \
  "$STAGE/EVA_VARIANTS_PREREG.md" "$STAGE/PROJECT_STATE.md" \
  "$STAGE/CLOUD.md" "$APP/deploy/"
echo "  done"

echo
echo "=== compile check ==="
"$APP/.venv/bin/python" -m py_compile \
  agent.py bot_config.py ledger.py main.py research.py \
  eva_variants.py eva_day.py eva_swing.py eva_swing_llm.py \
  eva_variants_bridge.py patterns/fvg.py patterns/structure_shift.py \
  dashboard/app.py && echo "  all modules compile"

echo
echo "=== variant tests on the server ==="
cd "$APP"
sudo -u ethagent "$APP/.venv/bin/python" -m pytest \
  tests/test_eva_variants.py tests/test_ict_detectors.py -q 2>&1 | tail -5

echo
echo "=== config sanity ==="
sudo -u ethagent "$APP/.venv/bin/python" - <<'PY'
import bot_config as bc
print("  EVA_VARIANTS_ENABLED :", bc.EVA_VARIANTS_ENABLED)
print("  EVA_LIVE_VARIANT     :", bc.EVA_LIVE_VARIANT)
print("  EVA_EXPERIMENT_EPOCH :", bc.EVA_EXPERIMENT_EPOCH)
print("  VARIANT_RISK_USD     :", bc.VARIANT_RISK_USD)
print("  day scan / swing llm :", bc.EVA_DAY_SCAN_INTERVAL_SEC, "/",
      bc.EVA_SWING_LLM_INTERVAL_SEC)
assert bc.EVA_LIVE_VARIANT == "control", "live variant must be control"
print("  OK: control holds the live sleeve")
PY

echo
echo "=== create variant tables ==="
sudo -u ethagent "$APP/.venv/bin/python" -c "import eva_variants; eva_variants.init_db(); print('  tables ready')"
sqlite3 "$APP/ledger.db" ".tables" | tr ' ' '\n' | grep -i variant | sed 's/^/  /'

echo
echo "=== restart ==="
systemctl restart eth-agent
sleep 12
systemctl restart eth-dashboard
sleep 6
systemctl is-active eth-agent eth-dashboard

echo
echo "=== scheduler jobs registered? ==="
journalctl -u eth-agent --since "2 minutes ago" --no-pager | grep -iE "eva (variants|swing)" | sed 's/^/  /' || echo "  (none yet)"

echo
echo "=== errors since restart ==="
journalctl -u eth-agent --since "2 minutes ago" --no-pager -p err | tail -15

echo
echo "=== endpoint ==="
curl -s "http://127.0.0.1:8000/api/eva/variants" | head -c 600
echo
echo "rollback: cp -a $BAK/. $APP/ && systemctl restart eth-agent eth-dashboard"
