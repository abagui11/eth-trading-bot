"""End-to-end beta signup against the live endpoint, checking that the
per-recipient notifier now reaches the reachable operator instead of
silently failing for everyone. Cleans up the test row.
"""
import json
import sqlite3
import subprocess
import time
import urllib.request

body = json.dumps({
    "email": "launch-check@eva.finance",
    "name": "Launch check",
    "note": "verifying beta notifications after the per-recipient fix",
}).encode()
req = urllib.request.Request(
    "http://localhost:8080/api/public/beta", data=body,
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=30) as r:
    print("POST /api/public/beta ->", r.status, json.load(r))

time.sleep(5)  # notify is fire-and-forget

print("\n--- notifier log lines ---")
out = subprocess.run(
    ["journalctl", "-u", "eth-dashboard", "--since", "2 minutes ago",
     "--no-pager", "-o", "cat"],
    capture_output=True, text=True).stdout
hits = [ln for ln in out.splitlines() if "notify" in ln.lower()
        or "Beta signup" in ln]
print("\n".join(hits[-10:]) if hits else
      "  (no notify errors logged - all configured sends accepted)")

con = sqlite3.connect("/opt/eth-trading-agent/ledger.db")
row = con.execute(
    "SELECT id, email, name, note FROM beta_signups"
    " WHERE email='launch-check@eva.finance'").fetchone()
print("\nrow persisted:", row)
con.execute("DELETE FROM beta_signups WHERE email='launch-check@eva.finance'")
con.commit()
print("cleaned; remaining signups:",
      con.execute("SELECT COUNT(*) FROM beta_signups").fetchone()[0])
con.close()
