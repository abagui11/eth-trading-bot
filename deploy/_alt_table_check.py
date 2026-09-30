#!/usr/bin/env python3
"""Read the hub's own /api/kalshi and print the altcoin section. Read-only.

Checks the thing the tab actually renders, rather than re-querying the ledger
and hoping the bridge agrees with it.

    /opt/eth-trading-agent/.venv/bin/python deploy/_alt_table_check.py
"""
from __future__ import annotations

import json
import urllib.request

URL = "http://localhost:8080/api/kalshi/performance"

with urllib.request.urlopen(URL, timeout=120) as resp:
    data = json.load(resp)

alt = data.get("altcoins") or {}
print(f"available       : {alt.get('available')}")
print(f"epoch           : {alt.get('epoch')}  ({alt.get('epoch_label')})")
print(f"first trade     : {alt.get('first_trade')}  ({alt.get('first_trade_label')})")
print()
head = f"{'Bot':<6} {'Series':<10} {'Mode':<6} {'Trades':>6} {'W/L':>7} {'Win%':>5} {'Early':>5} {'P&L':>8} {'Equity':>9}"
print(head)
print("-" * len(head))
for b in alt.get("bots", []):
    wr = f"{b['win_rate'] * 100:.0f}%" if b.get("win_rate") is not None else "-"
    trades = f"{b['closed']}" + (f"+{b['open']}o" if b.get("open") else "")
    print(
        f"{b['label']:<6} {b.get('series',''):<10} {b['mode']:<6} {trades:>6} "
        f"{str(b['wins']) + 'W/' + str(b['losses']) + 'L':>7} {wr:>5} "
        f"{b.get('early_exits', 0):>5} {b['epoch_pnl_usd']:>+8.2f} "
        f"{b['equity_usd']:>9.2f}"
    )

print("\nopen altcoin positions:")
for p in alt.get("open", []):
    print(f"  {p['bot_id']:<14} {p['market_ticker']:<28} "
          f"{p['side']} x{p['contracts']} @ {p['entry_cents']:.1f}c")

print("\nmain comparison still shows:", [b["bot_id"] for b in data.get("bots", [])])
leaked = [p["market_ticker"] for p in data.get("open", [])
          if p["bot_id"].startswith("eva_wick_")
          and p["bot_id"] != "eva_wick"]
print("altcoin rows leaking into the live feed:", leaked or "none")
