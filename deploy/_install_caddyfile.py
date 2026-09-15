"""Rebuild /etc/caddy/Caddyfile from the dashboard block + the eva.finance
snippet at /tmp/eva.snippet. Run on the VPS as root. Backs up first and
normalises line endings (the snippet is authored on Windows).
"""
import pathlib
import shutil
import subprocess
import time

CADDYFILE = pathlib.Path("/etc/caddy/Caddyfile")
SNIPPET = pathlib.Path("/tmp/eva.snippet")

DASHBOARD = "dashboard.eva.finance {\n\treverse_proxy localhost:8080\n}\n\n"

stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
if CADDYFILE.exists():
    backup = CADDYFILE.with_name(f"Caddyfile.bak-{stamp}")
    shutil.copy2(CADDYFILE, backup)
    print(f"backed up -> {backup}")

snippet = SNIPPET.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "")
CADDYFILE.write_text(DASHBOARD + snippet, encoding="utf-8")
print(f"wrote {CADDYFILE} ({len(DASHBOARD + snippet)} bytes)")

subprocess.run(["caddy", "fmt", "--overwrite", str(CADDYFILE)], check=True)

# Caddy runs as the caddy user; validating as root can create root-owned log
# files that the service then cannot open. Pre-create as caddy instead.
log = pathlib.Path("/var/log/caddy/eva-finance.log")
log.parent.mkdir(parents=True, exist_ok=True)
log.touch(exist_ok=True)
shutil.chown(log.parent, "caddy", "caddy")
shutil.chown(log, "caddy", "caddy")
log.chmod(0o644)

r = subprocess.run(
    ["sudo", "-u", "caddy", "caddy", "validate", "--config", str(CADDYFILE)],
    capture_output=True, text=True,
)
tail = (r.stderr or r.stdout).strip().splitlines()[-1:]
print("validate:", "\n".join(tail) or r.returncode)
if r.returncode != 0:
    raise SystemExit("validation failed - config NOT reloaded")

subprocess.run(["systemctl", "reload", "caddy"], check=True)
print("reloaded")
