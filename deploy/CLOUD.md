# Cloud deployment — automatic hourly trades + subscriber onboarding

Run the bot on a VPS so it sends trade suggestions every hour without your PC on.

---

## Overview

| Component | What it does |
|-----------|----------------|
| `main.py` | Telegram bot (chat + `/start`) + hourly trade cycle |
| `systemd` (`eth-agent.service`) | Keeps `main.py` running 24/7, restarts on crash |
| `ledger.db` → `subscribers` | Records everyone who messaged the bot |
| `ALLOWED_TELEGRAM_IDS` in `.env` | Manual paywall — only these IDs get suggestions + chat |

---

## Part 1 — One-time cloud setup

### 1. Stop the bot on your PC

Only **one** process can poll Telegram with the same bot token.

```powershell
# Kill local main.py if running (Ctrl+C in that terminal)
```

### 2. Push code to GitHub

```powershell
cd "C:\Users\bagui\OneDrive\Documents\Republic\projects\trading_bot_MVP"
git add .
git commit -m "Interactive agent v2"
git push origin main
```

### 3. Create a VPS

- **Ubuntu 22.04+** (Hetzner, DigitalOcean, etc.) — ~$5–6/mo
- Note the server **45.33.97.27**
- SSH in as root: `ssh root@45.33.97.27`

### 4. Install the app on the server

```bash
export REPO_URL=https://github.com/YOUR_USER/YOUR_REPO.git
curl -sSL https://raw.githubusercontent.com/YOUR_USER/YOUR_REPO/main/deploy/setup.sh | bash
# Or after cloning: sudo REPO_URL=... bash deploy/setup.sh
```

Or from a local copy:

```bash
sudo REPO_URL=https://github.com/abagui11/eth-trading-bot.git bash deploy/setup.sh
```

### 5. Configure secrets on the server

```bash
nano /opt/eth-trading-agent/.env
```

Required keys (see `.env.example`):

```env
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-4-6
TELEGRAM_BOT_TOKEN=...
ALLOWED_TELEGRAM_IDS=YOUR_TELEGRAM_ID
MARKET_DATA_API=https://api.coinbase.com/api/v3/brokerage/market
PORTFOLIO_VALUE=1000
PAPER_PORTFOLIO_VALUE=1000
```

**Important:** Leave `TELEGRAM_CHAT_ID` **empty** unless it is a *different* chat from your user ID (avoids duplicate hourly messages).

### 6. Start the service

```bash
sudo systemctl start eth-agent
sudo systemctl status eth-agent
sudo journalctl -u eth-agent -f    # live logs — Ctrl+C to exit
```

First hourly cycle runs ~10 seconds after start, then every hour.

### 7. Verify

```bash
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python /opt/eth-trading-agent/status.py
```

You should get a Telegram DM within a minute of the first cycle.

---

## Part 2 — Adding subscribers (manual allowlist)

### Flow for a new user

1. **You** share the bot link (e.g. `t.me/YourBotName`).
2. **They** open it and send **`/start`** (they may see the paywall — that's expected).
3. Their `telegram_id` is saved in `ledger.db` → table **`subscribers`**.
4. **You** approve them by adding their ID to `ALLOWED_TELEGRAM_IDS`.
5. **Restart** the service so `.env` reloads.
6. They send **`/start`** again — now they get welcome + hourly DMs.

They do **not** need @userinfobot if they message your bot first.

### On your PC (while testing locally)

```powershell
python subscribers.py
```

Shows pending users and copy-paste hints for `.env`.

Or SQLite:

```powershell
sqlite3 ledger.db
```

```sql
.headers on
.mode column
SELECT telegram_id, username, active, last_seen FROM subscribers;
```

### On the cloud server

```bash
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python /opt/eth-trading-agent/subscribers.py
```

Or:

```bash
sqlite3 /opt/eth-trading-agent/ledger.db "SELECT telegram_id, username, active, last_seen FROM subscribers;"
```

### Approve someone

Edit `.env` on the server:

```bash
sudo nano /opt/eth-trading-agent/.env
```

Add their ID (comma-separated):

```env
ALLOWED_TELEGRAM_IDS=2037245798,987654321
```

Restart:

```bash
sudo systemctl restart eth-agent
```

Tell them to `/start` the bot again.

---

## Part 3 — Day-to-day operations

### Deploy code updates

On the server:

```bash
sudo bash /opt/eth-trading-agent/deploy/update.sh
```

(Pulls latest git, reinstalls deps, restarts `eth-agent`.)

### View logs

```bash
sudo journalctl -u eth-agent -f
```

### Manual trade cycle (on server)

```bash
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python /opt/eth-trading-agent/agent.py
```

### Back up data

```bash
cp /opt/eth-trading-agent/ledger.db ~/ledger-backup-$(date +%Y%m%d).db
```

Contains suggestions, subscribers, and paper PnL history.

### Service commands

```bash
sudo systemctl stop eth-agent      # stop
sudo systemctl start eth-agent     # start
sudo systemctl restart eth-agent   # restart after .env change
sudo systemctl status eth-agent    # health check
```

---

## Part 4 — Public dashboard

The read-only dashboard lives in `dashboard/` and runs as a separate systemd service. It reads the same `ledger.db` and `charts/` as the bot.

### Start the dashboard (on server)

```bash
sudo systemctl start eth-dashboard
sudo systemctl status eth-dashboard
```

Default URL on the VPS (internal test):

```text
http://YOUR_SERVER_IP:8080
```

From your PC, open that URL in a browser once port 8080 is open in the firewall (testing only).

### Public HTTPS link (recommended)

1. Buy a domain (optional ~$10/yr) or use a subdomain you already own.
2. Add a DNS **A record** pointing to your VPS IP (e.g. `dashboard` → `45.33.97.27`).
3. Install Caddy for automatic HTTPS:

```bash
sudo apt install -y caddy
sudo nano /etc/caddy/Caddyfile
```

```text
dashboard.yourdomain.com {
    reverse_proxy localhost:8080
}
```

```bash
sudo systemctl reload caddy
```

Your public link: `https://dashboard.yourdomain.com` — open it from any device.

### Deploy dashboard updates

Same as the bot — push to GitHub, then on the server:

```bash
sudo bash /opt/eth-trading-agent/deploy/update.sh
```

This restarts both `eth-agent` and `eth-dashboard`.

### Backfill chart-read scores (older cycles)

After upgrading, run once to score historical hourly audits:

```bash
sudo -u ethagent bash /opt/eth-trading-agent/deploy/backfill_audit_scores.py
```

### Dashboard service commands

```bash
sudo systemctl stop eth-dashboard
sudo systemctl start eth-dashboard
sudo systemctl restart eth-dashboard
sudo journalctl -u eth-dashboard -f
```

If `eth-dashboard.service` is missing on an older VPS (only ran `update.sh`, not full `setup.sh`):

```bash
sudo bash /opt/eth-trading-agent/deploy/install_dashboard.sh
```

Then open `http://YOUR_SERVER_IP:8080` (allow port 8080 in the cloud firewall if needed).

---

## Checklist

- [ ] Local `main.py` stopped before starting cloud
- [ ] `.env` on server has all keys + `ALLOWED_TELEGRAM_IDS`
- [ ] `TELEGRAM_CHAT_ID` empty or different from allowlist IDs
- [ ] `systemctl status eth-agent` shows **active (running)**
- [ ] You received an hourly DM on Telegram
- [ ] New users: `/start` → `subscribers.py` → add ID → restart
