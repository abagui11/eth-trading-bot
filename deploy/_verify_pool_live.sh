#!/usr/bin/env bash
# Post-flip verification: the gate resolves, nobody lost access, no money moved.
set -uo pipefail
cd /opt/eth-trading-agent

echo "=== services ==="
systemctl is-active eth-agent eth-dashboard

echo
echo "=== pool state ==="
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python - <<'PY'
import access, bot_config, pool

print("POOL_ENABLED      ", bot_config.POOL_ENABLED)
print("admins            ", pool.admin_ids())
print("risk per Accept   ", f"{bot_config.POOL_RISK_PCT * 100:.1f}% of available cash")
print("min equity/deposit", bot_config.POOL_MIN_EQUITY_USD, "/", bot_config.POOL_MIN_DEPOSIT_USD)

accounts = pool.list_accounts()
print(f"\naccounts ({len(accounts)}):")
for a in accounts:
    uid = int(a["telegram_id"])
    print(
        f"  {uid:<12} {str(a['username'] or ''):<12} "
        f"approved={pool.is_approved(uid)!s:<5} funded={pool.is_funded(uid)!s:<5} "
        f"cash=${float(a['cash_usd']):,.2f} reserved=${float(a['reserved_usd']):,.2f}"
    )

print("\ntester cash claimed $%.2f (must be 0.00 until a Credit)" % pool.total_tester_cash())
print("intents frozen    ", pool.intents_frozen())

ids = access.broadcast_recipient_ids()
print(f"\nbroadcast recipients ({len(ids)}): {sorted(ids)}")
print("is_allowed sample  ", {i: access.is_allowed(i) for i in sorted(ids)})
PY

echo
echo "=== recent agent log ==="
journalctl -u eth-agent -n 12 --no-pager
