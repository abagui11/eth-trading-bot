#!/usr/bin/env bash
set -uo pipefail
DB=/opt/eth-trading-agent/ledger.db

echo "=== equal-risk sizing: risk_usd identical, qty differs with stop width ==="
sqlite3 -header -column "$DB" "
SELECT variant, product_id, side,
       round(entry,2) AS entry,
       round(stop_loss,2) AS stop,
       round(abs(entry-stop_loss)/entry*100,2) AS stop_pct,
       round(qty,6) AS qty,
       round(risk_usd,2) AS risk_usd,
       round(qty*abs(entry-stop_loss),2) AS implied_risk
FROM variant_positions;"

echo
echo "=== target reach in R (first must be >=0.5, last >=2.0) ==="
sqlite3 -header -column "$DB" "
SELECT id, variant, side, take_profits,
       round(abs(entry-stop_loss),2) AS risk_per_unit
FROM variant_positions;"

echo
echo "=== mark-to-market tracking ==="
sqlite3 -header -column "$DB" "
SELECT id, variant, status, tps_hit,
       round(mfe_r,3) AS mfe_r, round(mae_r,3) AS mae_r,
       path_checked_at
FROM variant_positions;"

echo
echo "=== control book untouched and healthy ==="
echo -n "  paper_positions rows: "; sqlite3 "$DB" "SELECT COUNT(*) FROM paper_positions;"
echo -n "  paper_trades rows:    "; sqlite3 "$DB" "SELECT COUNT(*) FROM paper_trades;"
echo -n "  live_trades rows:     "; sqlite3 "$DB" "SELECT COUNT(*) FROM live_trades;"
echo -n "  suggestions today:    "; sqlite3 "$DB" "SELECT COUNT(*) FROM suggestions WHERE ts >= date('now');"

echo
echo "=== swing-LLM token cost (per call) ==="
journalctl -u eth-agent --since "30 minutes ago" --no-pager \
  | grep 'anthropic_usage eva_swing_llm' | sed 's/^.*anthropic_usage/  /'

echo
echo "=== scheduler health ==="
echo -n "  day scans:  "; journalctl -u eth-agent --since "30 minutes ago" --no-pager | grep -c 'Running job "eva_day_scan'
echo -n "  swing runs: "; journalctl -u eth-agent --since "30 minutes ago" --no-pager | grep -c 'Running job "eva_swing_llm'
echo -n "  errors:     "; journalctl -u eth-agent --since "30 minutes ago" --no-pager -p err | grep -c . 
