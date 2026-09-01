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
# Optional: cheap model for macro classify/pulse, display summary, LLM critic
# ANTHROPIC_MODEL_FAST=claude-haiku-4-5
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

# --- Republic Intelligence layer ---
# HQ (abstention-first ICT) cards DM only these IDs. Ledger + house paper book
# still record every HQ idea, so dashboard quality tracking is unaffected.
INTERNAL_TELEGRAM_IDS=YOUR_TELEGRAM_ID
# Bearer tokens for /api/v1 consumers (yield_gen_bot, trade_ideas mill).
# REQUIRED: every /api/v1 route is token-only and returns 503 when unset.
SERVICE_API_TOKENS=token_for_yield,token_for_mill
# Colocated trade_ideas mill DB — this process records its Accept/Reject.
IDEAS_DB=/opt/trade-ideas/ideas.db
```

**Important:** Leave `TELEGRAM_CHAT_ID` **empty** unless it is a *different* chat from your user ID (avoids duplicate hourly messages).

For the beta, keep `PAYWALL_ENABLED=false`. Anyone with the bot link can send `/start`, use the inline keyboard, and receive bot access without being added to `ALLOWED_TELEGRAM_IDS`. `DASHBOARD_PUBLIC_URL` supplies the Telegram **Agent journal** button and **My book** magic links; use the final public HTTPS URL with no trailing path.

Optional: set `ME_TOKEN_SECRET` in `.env` for `/me` HMAC links (defaults to `TELEGRAM_BOT_TOKEN` if unset).

Live Coinbase (CDE nano futures) needs `EXECUTION_MODE=live` and a CDP key. **Always double-quote** `COINBASE_CDP_PRIVATE_KEY` — systemd `EnvironmentFile` mangles unquoted `\n` in a PEM (it strips the backslash). `config.py` also re-reads that key from `.env` so a mangled process env cannot win. Restart `eth-agent` and `eth-dashboard` after any `.env` edit.

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

To drop v2 fills that opened in July 2026 (experiment start 2026-08-01) without touching v1 archive:

```bash
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \
  /opt/eth-trading-agent/deploy/trim_paper_july.py --dry-run
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \
  /opt/eth-trading-agent/deploy/trim_paper_july.py --yes
sudo systemctl restart eth-dashboard
```

Dry-run first (no writes):

```bash
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \
  /opt/eth-trading-agent/deploy/reset_paper_epoch.py --dry-run
```

**Back up first:** `cp /opt/eth-trading-agent/ledger.db ~/ledger-backup-$(date +%Y%m%d).db`

### Re-score macro headlines after a keyword change

Keyword edits (e.g. promoting CLARITY Act / legislative catalysts in `macro/keywords.py`) only affect headlines ingested **after** the change. Headlines already stored as `ignored` keep their old score and are skipped by the 7-day URL-hash dedup, so they never resurface. After deploying a keyword change, backfill the recent window so already-captured headlines get promoted:

```bash
# Preview (no writes)
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \
  /opt/eth-trading-agent/deploy/rescore_macro_events.py --days 5 --dry-run

# Apply (re-scores + classifies newly-promoted headlines)
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \
  /opt/eth-trading-agent/deploy/rescore_macro_events.py --days 5 --yes
```

Promoted rows are classified via Haiku and flipped to `classified`, so they show up in active posture and `/research macro`. Use `--no-classify` to only refresh keyword scores.

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

The read-only dashboard lives in `dashboard/` and runs as a separate systemd service. It reads the same `ledger.db` and `charts/` as the bot. Eva close charts (`case_study_hq_{id}.png`) land in `charts/` and are served at `/api/live-chart/{id}` (HQ closed trades only).

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

The dashboard is the intelligence hub. Four tabs: **Brain** (vision / tape / news), **Eva Trades** (HQ ICT live + house paper — Eva is the ICT product), **Yield Generation** (stable book mirror), **Trade mill** (consumer idea stream plus the internal nano-ETH live clip that tests those ideas). Telegram **Idea feed** still opens `/feed` for Accept/Reject. Dual ETH/BTC spots, chart-read score tooltips, and a **Macro news monitor** sit on Brain / Eva Trades as before.

### Private investor link (`/investors`)

A read-only page for investors, not linked from the hub and marked `noindex,nofollow`. It shows Eva's portfolio value (week/month/year chart), realized gain for the day and year to date, unrealized, the **health factor** (Eva equity ÷ Eva open position size), every open Eva position with remaining size (ETH and $ notional), liquidation price (or n/a), take-profit ladder and current stop, per-day realized P&L, and — on each **closed** Eva trade — an annotated case-study chart of the fill (entry, stop, take-profits, post-trade note). Mill clips, the raw Coinbase account, and the paper books are not on this page.

Gate it before sharing the URL:

```bash
# on the server
cd /opt/eth-trading-agent
openssl rand -hex 16          # use the output as the token
nano .env                     # INVESTOR_ACCESS_TOKEN=<paste>
sudo systemctl restart eth-dashboard
```

Then share `https://dashboard.yourdomain.com/investors?k=<token>`. The first visit sets an httponly cookie (30 days, `INVESTOR_SESSION_TTL_SEC`), so reloads and in-app navigation work without the query string. Any request without a valid token gets a **404**, not a 401, so a guessed URL never confirms the page exists. To revoke access, change the token and restart the dashboard.

Leaving `INVESTOR_ACCESS_TOKEN` unset keeps the page reachable to anyone who knows the path — the same unlisted-only posture as `/volume`. Set it before sending the link to anyone outside the team.

The page sizes Eva off `LIVE_HQ_EQUITY_USD`. It does not read Coinbase balances; `deploy/diagnose_live.py` is the operator tool for the real futures wallet.

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

Severity ≥ 4 pulses still notify operators; **`tighten_sl` also ratchets house stops** (midpoint of entry↔current SL, never widens). `consider_close` stays advisory-only.

### Watchdog paper-execute toggle (post-audit default: off)

After the Jul-2026 paper audit, watchdog **scans and shadow-logs** by default but does **not** open house paper trades until execute is turned on. Shorts stay shadow-only while `WATCHDOG_ALLOW_SHORTS=False` in `bot_config.py`.

**Dashboard:** house journal → *Watchdog controls* — enter `MACRO_WEBHOOK_SECRET` and click Execute on/off.

**API:**

```bash
# Status
curl "https://dashboard.yourdomain.com/api/ops/watchdog-execute"

# Enable paper fills (Bearer = MACRO_WEBHOOK_SECRET)
curl -X POST "https://dashboard.yourdomain.com/api/ops/watchdog-execute" \
  -H "Authorization: Bearer YOUR_MACRO_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"enabled":true}'
```

**Telegram (admin/monitor only):** `/watchdog status` · `/watchdog on` · `/watchdog off`

Runtime override is stored in SQLite meta (`watchdog_execute_enabled`); config default remains `WATCHDOG_EXECUTE_ENABLED=False`.

**Ops note:** if an oversized watchdog BTC short is still open after deploy, flatten or hard-cap it manually before re-enabling execute.

### Republic Intelligence API (`/api/v1`)

The dashboard also serves the intelligence layer consumed by `yield_gen_bot`
and the `trade_ideas` mill. **Every route is token-only** (`SERVICE_API_TOKENS`,
comma-separated) and returns **503** when no tokens are configured — the public
dashboard routes (`/api/status`, `/api/performance`, `/api/macro`) are
unaffected.

| Route | Purpose |
|---|---|
| `/api/v1/intelligence/latest` · `/history` | H4/H1/M15 BTC/ETH stances, medium summary, funding regimes, long thesis |
| `/api/v1/signals/macro` · `/zmove` · `/funding` | signal feeds for the mill |
| `/api/v1/subscribers` | broadcast recipients (mill fan-out; paywall logic stays here) |
| `/api/v1/ideas/hq` | gated HQ ICT ideas |
| `/api/v1/charts/cycle` | BTC 4-year-cycle PNG |
| `POST /api/v1/execute/mill` | offer a sized mill idea to the live sleeve (auto or manual) |
| `/api/v1/execute/mill/capacity` | mill sleeve occupancy — open count, free slots, halt reason |

```bash
curl -H "Authorization: Bearer $SERVICE_TOKEN" \
  http://127.0.0.1:8080/api/v1/intelligence/latest
```

Artifacts populate fast after a restart: stances ~10s (bootstrap cycle),
funding ~20s, long thesis ~2min; then hourly on the wall clock.

### trade_ideas mill (colocated volume lane)

The mill runs on **this same box** so it reaches the API over localhost (the
service token never crosses the network) and shares one SQLite with the agent.

It uses the **agent's own bot token, send-only**. Telegram allows a single
`getUpdates` consumer per token and that is `eth-agent`; the mill must never
poll, and the agent's dispatcher records the `idea:accept|reject` callbacks via
`trade_ideas_bridge`. Set the **same** `IDEAS_DB` path in both `.env` files.

```bash
sudo git clone <trade_ideas repo> /opt/trade-ideas
sudo bash /opt/trade-ideas/deploy/install.sh
sudo nano /opt/trade-ideas/.env      # SERVICE_TOKEN + the agent's TELEGRAM_BOT_TOKEN
sudo systemctl start trade-ideas
journalctl -u trade-ideas -f
```

Leave `TELEGRAM_CHAT_IDS` empty in production so recipients resolve from
`/api/v1/subscribers`; set it to your own ID for a private dry run.

#### Mill live sleeve — keeping a clip open

The sleeve is `$1,400` with up to **3** open clips. Two paths fill it, both
gated in `execute.execute_mill_idea` (hub-side — the mill never decides):

- **auto (FIFO):** fires only while the sleeve is *empty*, and only for ideas
  at or above `LIVE_MILL_AUTO_MIN_CONFIDENCE`. This is what stops the book
  going idle; it deliberately never takes the 2nd or 3rd slot.
- **manual:** an Accept from `LIVE_MILL_FILL_TELEGRAM_IDS`. Skips the
  conviction floor, still obeys open-count, sleeve, and halt. At max the
  operator gets a "Too many trades open" reply listing the open book instead
  of a fill.

Clip size is **always one CDE nano contract** (0.1 ETH or 0.01 BTC). Notional
is qty × mark; a contract that no longer fits the sleeve is rejected by the
exposure check — expect BTC to fill only when the book is otherwise near-empty.

Requires `EXECUTION_MODE=shadow|live` here **and** `MILL_LIVE_ENABLED=true` in
the mill's `.env`. Check occupancy and auto/manual attribution with:

```bash
curl -H "Authorization: Bearer $SERVICE_TOKEN" \
  http://127.0.0.1:8080/api/v1/execute/mill/capacity
sqlite3 /opt/eth-trading-agent/ledger.db \
  "SELECT id, product_id, side, fill_type, filled_by, status FROM live_trades
   WHERE source='mill' ORDER BY id DESC LIMIT 10;"
```

#### Live fill / close alerts

Both sleeves fill unattended, so every live open and close pushes a Telegram
card to `TELEGRAM_ADMIN_CHAT_ID` **and** every id in `LIVE_ALERT_TELEGRAM_IDS`
(defaults to the two mill fill operators). Halts already used this path. The
list is de-duped, and one unreachable chat no longer stops the others being
notified. Set `LIVE_FILL_ALERTS_ENABLED=False` in `bot_config.py` to silence
fills while keeping halt alerts.

If alerts stop arriving, check the bot token can reach each chat — each
operator must have started a conversation with the bot at least once:

```bash
journalctl -u eth-agent -n 200 | grep 'Ops Telegram notify failed'
```

#### Nothing is filling — run the diagnostic first

`deploy/diagnose_live.py` walks every live gate in the same order
`execute._execute` applies them and names the first one that blocks, for both
the HQ/Eva and mill sleeves. It is read-only: it resolves instruments and reads
balances but never places, cancels, or closes an order.

```bash
cd /opt/eth-trading-agent
sudo -u ethagent .venv/bin/python deploy/diagnose_live.py
```

It must be run **on the server** — run from a laptop it reports that machine's
`.env` and dev ledger, not production. Exit code is `1` when a blocker is found.

The gate worth knowing by heart: `EXECUTION_MODE=off` returns *before* any
logging, so a disabled sleeve produces **no** "Live skip" line at all. Silence
in the log is the signature of the master switch being off, not of a quiet
market. Every other refusal names itself:

```bash
journalctl -u eth-agent -n 500     | grep -E 'Live skip|Vault skip|LIVE FILL'
journalctl -u eth-dashboard -n 500 | grep 'Mill idea'
journalctl -u trade-ideas -n 500   | grep 'Mill live bridge'
```

Sizing is also worth a sanity check: `LIVE_MILL_SLEEVE_USD` and
`LIVE_HQ_EQUITY_USD` are hardcoded constants that are never reconciled against
the real balance. A deposit sitting in the spot/USDC wallet does **not** fund
the sleeve — it has to be moved into the CFM futures wallet. The diagnostic
prints the actual futures cash so you can compare.

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
