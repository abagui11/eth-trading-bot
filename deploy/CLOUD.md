# Cloud deployment — automatic hourly trades + subscriber onboarding

Run the bot on a VPS so it sends trade suggestions every hour without your PC on.

> **Architecture & status:** see [`PROJECT_STATE.md`](PROJECT_STATE.md). When you change runtime behaviour, config, or deploy steps, update that file and/or this one in the same commit.

---

## Overview

| Component | What it does |
|-----------|----------------|
| `main.py` | Telegram bot (chat + `/start` + inline buttons) + dual-asset hourly trade cycle + watchdog scanner |
| `systemd` (`eth-agent.service`) | Keeps `main.py` running 24/7, restarts on crash |
| `ledger.db` → `subscribers` | Records everyone who messaged the bot |
| `PAYWALL_ENABLED` in `.env` | `false` for open beta link access; set `true` to enforce `ALLOWED_TELEGRAM_IDS` |

The live strategy evaluates **ETH-USD and BTC-USD** in both the hourly cycle and watchdog. Both assets share one paper book; W1 ETH/BTC relative strength is advisory context and a watchdog soft gate.

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
PAYWALL_ENABLED=false
ALLOWED_TELEGRAM_IDS=YOUR_TELEGRAM_ID
DASHBOARD_PUBLIC_URL=https://dashboard.yourdomain.com
MARKET_DATA_API=https://api.coinbase.com/api/v3/brokerage/market
PORTFOLIO_VALUE=5000
PAPER_PORTFOLIO_VALUE=5000
# Optional macro headline feeds (defaults to CNBC + CoinDesk if unset)
# MACRO_FEED_URLS=https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114,https://www.coindesk.com/arc/outboundfeeds/rss/
# MACRO_KEYWORD_EXTRA=fusaka
# MACRO_WEBHOOK_SECRET=your-random-secret
```

**Important:** Leave `TELEGRAM_CHAT_ID` **empty** unless it is a *different* chat from your user ID (avoids duplicate hourly messages).

For the beta, keep `PAYWALL_ENABLED=false`. Anyone with the bot link can send `/start`, use the inline keyboard, and receive bot access without being added to `ALLOWED_TELEGRAM_IDS`. `DASHBOARD_PUBLIC_URL` supplies the Telegram **Agent journal** button and **My book** magic links; use the final public HTTPS URL with no trailing path.

Optional: set `ME_TOKEN_SECRET` in `.env` for `/me` HMAC links (defaults to `TELEGRAM_BOT_TOKEN` if unset).

**Open account** creates a personal demo paper book ($500 / $1,000 / $2,500 once). Demo capital — not real funding. Legacy users who Funded before are migrated to a $1,000 personal account (`python deploy/migrate_personal_accounts.py`, also runs on `paper.init_db`). Trade suggestions arrive as a **concise card** (decision chart + friendly caption with Accept / Reject / **See more**). Only Accept deploys that user's cash. **See more** loads the detailed charts and full audited rationale. The public dashboard shows the **agent/house** journal plus participation aggregates; personal equity is on `/me` via **My book**.

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

## Part 2 — Subscriber onboarding

### Open beta flow (`PAYWALL_ENABLED=false`)

1. **You** share the bot link (for example, `https://t.me/YourBotName`).
2. **They** open it and send **`/start`**.
3. Their `telegram_id` is saved in `ledger.db` → `subscribers`, and the bot returns the inline keyboard.
4. They can use **Open account**, **My Metrics**, **My book**, **Agent journal**, and **Research** immediately.

No manual approval or @userinfobot lookup is required in beta mode.

### Restricted flow (`PAYWALL_ENABLED=true`)

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

(Pulls latest git, reinstalls deps, restarts `eth-agent` and `eth-dashboard`.)

### One-time: reset paper book to $5k epoch (Jul 2026)

After pulling code that bumps `PORTFOLIO_VALUE` / `PAPER_PORTFOLIO_VALUE` to **5000**, update `.env` on the server, then archive the old $1k paper trades and start fresh:

```bash
sudo nano /opt/eth-trading-agent/.env
# Set:
#   PORTFOLIO_VALUE=5000
#   PAPER_PORTFOLIO_VALUE=5000

sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \
  /opt/eth-trading-agent/deploy/reset_paper_epoch.py --yes

sudo systemctl restart eth-agent eth-dashboard
```

This moves all `paper_trades` / `paper_positions` into archive tables (label `legacy_1k`), resets cash to $5,000, and seeds the house row in `paper_contributions`. New ETH and BTC trades use a fixed **25% of live paper equity** (`TRADE_DEPLOY_PCT`) with product-specific quantity caps. A subscriber's later **Fund** action adds a separate fake $1,000 deposit to this same book. The dashboard shows archived trades in a separate section.

Dry-run first (no writes):

```bash
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \
  /opt/eth-trading-agent/deploy/reset_paper_epoch.py --dry-run
```

**Back up first:** `cp /opt/eth-trading-agent/ledger.db ~/ledger-backup-$(date +%Y%m%d).db`

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

Your public link: `https://dashboard.yourdomain.com` — open it from any device. Set the same value as `DASHBOARD_PUBLIC_URL` in `/opt/eth-trading-agent/.env`, then restart `eth-agent` so Telegram's **Agent journal** and **My book** links use it.

After deploying personal books, run once (or rely on `paper.init_db` auto-migrate):

```bash
cd /opt/eth-trading-agent
source .venv/bin/activate
python deploy/migrate_personal_accounts.py
sudo systemctl restart eth-agent eth-dashboard
```

The first hourly cycle may also send the one-time launch notice to subscribers.

The dashboard includes dual ETH/BTC live spots, shared paper-book performance, paginated trade/cycle history with per-asset labels, chart-read score tooltips, and a **Macro news monitor** section (active classified headlines, recent ingested items, posture gates).

### Macro headline webhook (optional push ingest)

Push headlines into the same pipeline as RSS (keyword score → Haiku classify → pulse if severity ≥ 4).

1. Set `MACRO_WEBHOOK_SECRET` in `/opt/eth-trading-agent/.env`
2. POST to the dashboard (HTTPS via Caddy recommended):

```bash
curl -X POST "https://dashboard.yourdomain.com/api/macro/ingest" \
  -H "Authorization: Bearer YOUR_MACRO_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"title":"U.S. revokes Iran oil authorization after tanker attacks","url":"https://...","force_classify":true}'
```

Fields: `title` (required), `url`, `summary`, `source`, `published_at`, `force_classify` (bypass keyword promote threshold).

**Telegram manual ingest:** send `/macro <headline>` from `MONITOR_CHAT_ID` or `TELEGRAM_ADMIN_CHAT_ID` (always force-classifies).

**Read API:** `GET /api/macro` — JSON for dashboard refresh (posture, active events, recent ingested).

### Deploy dashboard updates

Same as the bot — push to GitHub, then on the server:

```bash
sudo bash /opt/eth-trading-agent/deploy/update.sh
```

This restarts both `eth-agent` and `eth-dashboard`.

### Research reports (`/research` in Telegram)

Subscribers can run `/research` for the topic catalog. Snapshot topics need outbound HTTPS to Coinbase, Hyperliquid, Kraken Futures, Gate.io (primary perp/funding on US VPS), CoinGecko, and blockchain.info. Binance/Bybit are tried last but often return 451/403 from US-hosted servers.

SFP pattern studies need historical OHLC in `ohlc.db` (ETH and/or BTC):

```bash
# ETH (default) — daily + hourly
sudo -u ethagent bash -c 'cd /opt/eth-trading-agent && .venv/bin/python backfill.py --all'

# Both products
sudo -u ethagent bash -c 'cd /opt/eth-trading-agent && .venv/bin/python backfill.py --all --product all'

# BTC only
sudo -u ethagent bash -c 'cd /opt/eth-trading-agent && .venv/bin/python backfill.py --all --product BTC-USD'
```

Run once on a fresh VPS (or after DB wipe). Daily history powers `d1_sfps` / `weekly_sfp` / `w1_invalidations`; hourly backfill is required for H12 studies. Backfill also rebuilds the deterministic `sfp_events` index used for grounded counts.

Telegram topics: `/research d1_sfps`, `weekly_sfp`, `h12_sfp`, `w1_invalidations`, `h12_invalidations` (optional `ETH`/`BTC` + years). Ambiguous or unindexed pattern asks (e.g. M5 OB counts) clarify or refuse instead of inventing numbers.

### Z-Move alerts

When `ZMOVE_ENABLED` (default on), the agent scans ETH-USD H1 price returns and volume every `ZMOVE_INTERVAL_SEC` (300s). Spikes with `|z| ≥ ZMOVE_THRESHOLD` (2.0) against a 168h lookback broadcast to all subscribers, with a 2h per-metric cooldown (`zmove_state` in the ledger DB).

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
- [ ] `.env` has `PAYWALL_ENABLED=false` for beta (or an allowlist when `true`)
- [ ] `.env` has the public HTTPS `DASHBOARD_PUBLIC_URL`
- [ ] `TELEGRAM_CHAT_ID` empty or different from allowlist IDs
- [ ] `systemctl status eth-agent` shows **active (running)**
- [ ] You received an hourly DM on Telegram
- [ ] Beta onboarding tested: share bot link → user sends `/start` → inline keyboard appears
