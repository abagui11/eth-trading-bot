"""Idempotent in-place patch: mount the public marketing API.

Run on the VPS from /opt/eth-trading-agent. The server checkout is on a
different commit than the dev tree, so app.py/config.py are edited by anchor
rather than overwritten. Safe to re-run: every edit checks for itself first.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

STAMP = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
changed = []


def patch(path: Path, anchor: str, addition: str, marker: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        print(f"  = {path} already patched")
        return
    if anchor not in text:
        sys.exit(f"  ! anchor not found in {path}: {anchor!r}")
    shutil.copy2(path, path.with_suffix(path.suffix + f".bak-publicapi-{STAMP}"))
    path.write_text(text.replace(anchor, addition, 1), encoding="utf-8")
    changed.append(str(path))
    print(f"  + patched {path}")


app = Path("dashboard/app.py")
patch(
    app,
    "from dashboard.intel_api import router as intel_router",
    "from dashboard.intel_api import router as intel_router\n"
    "from dashboard import public_api",
    "from dashboard import public_api",
)
patch(
    app,
    "    app.include_router(intel_router)",
    "    public_api.init_db()\n\n"
    "    app.include_router(intel_router)\n"
    "    # Marketing-site API (eva.finance). In production Caddy proxies\n"
    "    # eva.finance/api/* here same-origin; the CORS entry below exists only\n"
    "    # so a local `astro dev` (:4321) can hit a local dashboard in dev.\n"
    "    app.include_router(public_api.router)\n"
    "    from fastapi.middleware.cors import CORSMiddleware\n\n"
    "    app.add_middleware(\n"
    "        CORSMiddleware,\n"
    '        allow_origins=["http://localhost:4321"],\n'
    '        allow_methods=["GET", "POST"],\n'
    '        allow_headers=["Content-Type"],\n'
    "    )",
    "app.include_router(public_api.router)",
)

cfg = Path("config.py")
patch(
    cfg,
    "INVESTOR_ACCESS_TOKEN: str | None = _optional(\"INVESTOR_ACCESS_TOKEN\")",
    "# Beta signups from the eva.finance marketing site (POST /api/public/beta).\n"
    "# The signup row in ledger.db is the source of truth; this is only who gets\n"
    "# the notification email. Requires RESEND_API_KEY and a verified-domain\n"
    "# ALERT_EMAIL_FROM to deliver to external addresses.\n"
    "BETA_SIGNUP_EMAIL_TO: str = (\n"
    '    _optional("BETA_SIGNUP_EMAIL_TO")\n'
    '    or "abagui@republictech.io,daniel@republictech.io"\n'
    ")\n\n"
    "INVESTOR_ACCESS_TOKEN: str | None = _optional(\"INVESTOR_ACCESS_TOKEN\")",
    "BETA_SIGNUP_EMAIL_TO",
)

print("changed:", changed or "nothing (already up to date)")
