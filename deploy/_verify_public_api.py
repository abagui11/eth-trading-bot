"""Post-deploy smoke check for the eva.finance public API. Run on the VPS."""
import json
import urllib.request

BASE = "http://localhost:8080/api/public"

with urllib.request.urlopen(f"{BASE}/strategies", timeout=30) as r:
    d = json.load(r)

print("books present:", [k for k in ("hq", "mill", "yield", "kalshi") if k in d])
print("as_of:", d.get("as_of"))
ab = d.get("abstention", {})
print(f"abstention: {ab.get('abstain_pct')}% of {ab.get('cycles')} cycles")
hq = d.get("hq", {}).get("live", {})
print(f"hq live: n={hq.get('n_closed')} pnl={hq.get('pnl_usd')} wr={hq.get('win_rate_pct')}")
hp = d.get("hq", {}).get("paper", {})
print(f"hq paper: n={hp.get('n_closed')} equity={hp.get('equity_usd')} pts={len(hp.get('series') or [])}")
m = d.get("mill", {})
print(f"mill: {m.get('n_ideas')} ideas, {m.get('ideas_per_day')}/day, hit {m.get('win_rate_pct')}%")
y = d.get("yield", {})
print(f"yield: nav={y.get('nav_usd')} pnl_pct={y.get('pnl_pct')} days={y.get('n_days')}")
k = d.get("kalshi", {})
print(f"kalshi: epoch={k.get('epoch_pnl_usd')} total={k.get('total_pnl_usd')} bots={list((k.get('bots') or {}))}")

# Beta signup round trip (honeypot + real), then clean the test row out.
req = urllib.request.Request(
    f"{BASE}/beta",
    data=json.dumps({"email": "deploy-smoke@eva.finance", "name": "deploy smoke",
                     "note": "post-deploy verification"}).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=30) as r:
    print("beta signup:", r.status, json.load(r))

import sqlite3
con = sqlite3.connect("/opt/eth-trading-agent/ledger.db")
rows = con.execute(
    "SELECT id, email, name FROM beta_signups ORDER BY id DESC LIMIT 3").fetchall()
print("beta_signups rows:", rows)
con.execute("DELETE FROM beta_signups WHERE email = 'deploy-smoke@eva.finance'")
con.commit()
print("cleaned test row; remaining:",
      con.execute("SELECT COUNT(*) FROM beta_signups").fetchone()[0])
con.close()
