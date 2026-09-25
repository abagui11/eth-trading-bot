"""Smoke: build the Investor Analytics payload against given DB paths."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if len(sys.argv) > 2:
    os.environ["KALSHI_DB"] = sys.argv[2]

import config

if len(sys.argv) > 1:
    config.LEDGER_DB = sys.argv[1]

from dashboard.edge_analytics import build_edge_payload

p = build_edge_payload()
print("generated_at:", p["generated_at"])
print(f"{'book':28s} {'n':>5s} {'days':>4s} {'total':>9s} {'win':>4s} {'P':>5s} {'sharpe':>6s} {'pts':>5s}")
for b in p["books"]:
    print(f"{b['label']:28s} {b['n']:5d} {b['days']:4d} {b['total']:9.2f} "
          f"{str(b['win_pct']) + '%' if b['win_pct'] is not None else '—':>4s} "
          f"{b['p_edge'] if b['p_edge'] is not None else '—':>5} "
          f"{b['sharpe'] if b['sharpe'] is not None else 'n/a':>6} "
          f"{len(b['equity']):5d}")
if p["scaling"]:
    for s in p["scaling"]["books"]:
        print(f"scaling: {s['label']:42s} net {s['net_usd_per_ct']*100:+.2f}c/ct "
              f"x {s['trades_per_day']}/d -> $[{s['usd_per_day'][0]} .. {s['usd_per_day'][-1]}]")
    print("cap:", p["scaling"]["cap_ct"])
print("payload bytes:", len(json.dumps(p)))
