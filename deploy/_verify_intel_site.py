"""Post-deploy check for the intelligence-layer rework of eva.finance.

Runs on the VPS. Confirms Caddy serves every page (including the new
/intelligence route), that unknown paths still return a true 404, and that
the public API is serving the new intelligence block.
"""
import json
import subprocess
import urllib.request

PAGES = [
    ("/", 200),
    ("/intelligence", 200),
    ("/strategies", 200),
    ("/case-studies", 200),
    ("/beta", 200),
    ("/docs", 200),
    ("/docs/whitepaper", 200),
    ("/docs/engine", 200),
    ("/docs/strategies", 200),
    ("/docs/getting-started", 200),
    ("/definitely-not-a-page", 404),
]

fails = []
print("--- pages (via Caddy, Host: eva.finance)")
for path, want in PAGES:
    code = subprocess.run(
        ["curl", "-s", "-L", "-o", "/dev/null", "-w", "%{http_code}",
         "-H", "Host: eva.finance", f"http://127.0.0.1{path}"],
        capture_output=True, text=True).stdout.strip()
    ok = code == str(want)
    if not ok:
        fails.append(f"{path}: got {code}, want {want}")
    print(f"  {'ok ' if ok else 'FAIL'} {code:<4} {path}")

print("--- new page actually carries the new content")
html = urllib.request.urlopen(
    urllib.request.Request("http://127.0.0.1/intelligence",
                           headers={"Host": "eva.finance"})).read().decode()
for needle in ("The four-year cycle", "Multi-timeframe stance",
               "ICT structure, implemented as code", "never mints a", "Bear drawdown"):
    ok = needle in html
    if not ok:
        fails.append(f"/intelligence missing: {needle}")
    print(f"  {'ok ' if ok else 'FAIL'} {needle!r}")

print("--- homepage reframed")
home = urllib.request.urlopen(
    urllib.request.Request("http://127.0.0.1/", headers={"Host": "eva.finance"})).read().decode()
for needle, want in (("One brain. Every horizon.", True),
                     ("Knows when to do nothing", False),
                     ("abstention-first", False),
                     ("One brain, five clocks", True)):
    present = needle in home
    ok = present is want
    if not ok:
        fails.append(f"homepage {needle!r}: present={present}, want={want}")
    print(f"  {'ok ' if ok else 'FAIL'} {'has' if want else 'no '} {needle!r}")

print("--- public API intelligence block")
api = json.load(urllib.request.urlopen(
    "http://127.0.0.1:8080/api/public/strategies"))
intel = api.get("intelligence", {})
cyc = intel.get("cycle", {})
print(f"  cycle    {cyc.get('phase')} day {cyc.get('days_since_halving')}"
      f" ({cyc.get('progress_pct')}% to {cyc.get('next_halving_est')})")
print(f"  thesis   {cyc.get('thesis')}")
grid = intel.get("stances", {}).get("grid", [])
print(f"  stances  {len(grid)} cells: {grid}")
print(f"  macro    {intel.get('macro')}")
if len(grid) != 6:
    fails.append(f"expected 6 stance cells, got {len(grid)}")
if not cyc.get("phase"):
    fails.append("no cycle phase in API")

print()
if fails:
    print(f"FAILURES ({len(fails)}):")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
