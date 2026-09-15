"""Beta-notification delivery check. Run with the app venv on the VPS:

    /opt/eth-trading-agent/.venv/bin/python deploy/_check_beta_email.py

Reports which operators Resend will actually accept mail for, given the
current ALERT_EMAIL_FROM. Use it to confirm the fix after verifying
republictech.io at resend.com/domains.

Uses `requests` deliberately — it is what dashboard/public_api.py calls, and
urllib gets a Cloudflare 403 on its user-agent that says nothing about Resend.
"""
import os
import sys

sys.path.insert(0, "/opt/eth-trading-agent")
os.chdir("/opt/eth-trading-agent")

import requests  # noqa: E402

env = {}
for line in open(".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k] = v.strip().strip('"').strip("'")

key = env.get("RESEND_API_KEY", "")
sender = env.get("ALERT_EMAIL_FROM", "(unset)")
to = [a.strip() for a in env.get(
    "BETA_SIGNUP_EMAIL_TO",
    "abagui@republictech.io,daniel@republictech.io").split(",") if a.strip()]
H = {"Authorization": f"Bearer {key}"}

print(f"sender: {sender}")
print(f"recipients: {to}")

print("\n--- verified domains ---")
r = requests.get("https://api.resend.com/domains", headers=H, timeout=20)
print(f"  HTTP {r.status_code}: {r.text[:400]}")

print("\n--- send to both recipients in one call (what the app does) ---")
r = requests.post(
    "https://api.resend.com/emails", headers=H, timeout=20,
    json={"from": sender, "to": to,
          "subject": "[Eva beta] delivery test",
          "text": "Delivery test for the eva.finance beta signup notifier."},
)
print(f"  HTTP {r.status_code}: {r.text[:500]}")

print("\n--- send to each recipient individually ---")
for addr in to:
    r = requests.post(
        "https://api.resend.com/emails", headers=H, timeout=20,
        json={"from": sender, "to": [addr],
              "subject": "[Eva beta] delivery test",
              "text": "Delivery test for the eva.finance beta signup notifier."},
    )
    print(f"  {addr}: HTTP {r.status_code} {r.text[:300]}")
