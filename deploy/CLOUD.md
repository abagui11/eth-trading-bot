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
DASHBOARD_PUBLIC_URL=https://dashboard.eva.finance
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

First trade cycle runs ~10 seconds after start, then on the wall-clock slot set by `CYCLE_INTERVAL_SEC` (1800 = every 30 minutes, on :00 and :30).

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

### Tester pool flow (`bot_config.POOL_ENABLED = True`)

The pool replaces both flows above with an **in-band Admit tap** — no `.env`
edit, no restart:

1. **They** open the bot and send anything. Their request lands in
   `approved_users` and every pool admin gets a DM card with **Admit / Deny**.
2. **You** tap Admit. They get the welcome DM, the Account keyboard
   (Portfolio / Deposit), and a **one-time invite link** to the forum group.
3. **They** register the wallet they'll send from (`/wallet 0x…`), then
   `/deposit` for the address, send USDC, and file `/deposit 1000 <txid>`.
   **Nothing is needed from you** — the watcher credits them within a minute
   of the transfer settling and DMs them their new balance. You get an FYI,
   plus a ping if a transfer arrives that nobody claimed.
4. From then on their Accepts in the Trades topic join live fills with
   pooled sizing; `/portfolio` shows their real book. `/credit <id> <usd>`
   and `/debit <id> <usd>` are the admin escape hatches (a debit can never
   touch margin reserved in open trades).

#### One-time forum setup

With ``POOL_ENABLED``, **trade cards are personal DMs** so each card can show
that tester's Accept risk and position size. Leave `POOL_FORUM_*` / mill
`TELEGRAM_FORUM_*` unset for trades, or use the forum only for Research
alerts. If you still set a Trades forum id, the hub ignores it for HQ cards
while the pool is on.

Optional Research topic (z-moves / digests), if you want a shared channel:

1. Create a private Telegram group → group settings → enable **Topics**.
2. Create a **Research** topic (Trades optional / unused while pool DMs).
3. Add the bot as **admin** with *Manage Topics* and *Invite Users via Link*.
4. Read the ids: post one message in the topic, then
   `curl "https://api.telegram.org/bot<token>/getUpdates"` — `chat.id` is the
   (negative) group id, `message_thread_id` the topic id.
5. In `/opt/eth-trading-agent/.env`:

   ```env
   # Optional — Research pushes only while POOL_ENABLED (trade cards stay DMs)
   POOL_FORUM_CHAT_ID=-1001234567890
   POOL_FORUM_RESEARCH_THREAD_ID=3
   POOL_DEPOSIT_ADDRESS=0x...
   # Required for withdrawals: proves who sent a deposit and that a payout
   # landed. Without it no wallet reaches `verified` and /withdraw refuses.
   ETHERSCAN_API_KEY=...
   ```

6. For the mill, leave `TELEGRAM_FORUM_CHAT_ID` **unset** so idea cards DM
   approved subscribers (same audience as HQ). Set it only if you want mill
   cards in a shared topic *without* per-user size lines.

#### Turning the pool on (done 2026-09-15 — kept as the runbook)

Two things must be in place **before** the flag flips, because both fail
silently and one of them is a lockout you cannot undo from Telegram.

1. **An admin id must resolve.** With `POOL_ADMIN_TELEGRAM_IDS` empty in both
   `bot_config.py` and `.env`, no `INTERNAL_TELEGRAM_IDS`, and no
   `TELEGRAM_ADMIN_CHAT_ID`, `pool.admin_ids()` returns `[]` — the Admit and
   Credit cards go nowhere and nobody can ever be let into the product. Set
   your **Telegram user id** (not a chat id, no minus sign):

   ```env
   POOL_ADMIN_TELEGRAM_IDS=2037245798
   ```

   Check it: `@userinfobot` or `@getidsbot` on Telegram replies with your id.
   Comma-separate for several admins; every one of them gets every card.

2. **Grandfather the existing users in.** Turning the pool on makes
   `access.is_allowed` approval-gated, so anyone already using the bot is
   locked out until Admitted. On this box `ALLOWED_TELEGRAM_IDS` is empty —
   it has run paywall-off since the beta, so the `subscribers` table *is* the
   access list. The bootstrap admits both sources as approved, zero-balance
   accounts. It never moves money and is idempotent:

   ```bash
   cd /opt/eth-trading-agent
   sudo -u ethagent .venv/bin/python deploy/pool_bootstrap.py --dry-run
   sudo -u ethagent .venv/bin/python deploy/pool_bootstrap.py --apply
   ```

Then deploy and restart both services. Verify:

```bash
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python -c \
  "import pool, access; print('admins', pool.admin_ids()); \
   print('recipients', access.broadcast_recipient_ids())"
```

`admins` must be non-empty and `recipients` must still list everyone who was
getting cards yesterday. Trading is unaffected while every account is
unfunded: `extra_contracts_for` returns 0, so house order size, levels and
exits are identical to before the flip.

#### Removing an account — `/unsubscribe`

`/unsubscribe <telegram_id>` (admin-only) deletes an account and every trace
of its onboarding, so the same id can go through `/start` as a brand-new user.
That is what makes an onboarding demo repeatable: the first-contact states are
one-shot, so an approved id sees the welcome instead of "request sent for
review", and a wallet cannot be registered twice as a first registration.

**It takes two taps.** The command only ever shows what would go — rows per
table, the balance, and any write-off — and the **Remove account** button on
that card is what deletes. The id is typed by hand and nothing downstream can
undo a wrong one. The guards are re-run when the button is tapped, so a stake
opened or a deposit that landed in between still stops it.

It refuses, and each refusal is a way the deletion could take money from
somebody:

| Refusal | Why |
|---|---|
| open stake | the account owns a share of a *live* position; deleting it would hand that share to the other holders on the next booked exit |
| pending Accept | resolves within a minute or two into a stake or a refund |
| withdrawal in flight | the row is the only evidence a send may already have happened, and Coinbase cannot be asked |
| pending deposit claim | money is on its way and the sweep is about to credit it |
| withdrawable balance | at or above the $50 minimum it is still theirs, so it leaves as a payout to the address they proved they control — `/withdraw all`, or `/debit` if the money was never real |

What is left after those is a residue **below** the minimum, which no
withdrawal can move — the unused fee reserve coming back from a `/withdraw
all` is exactly this. It is written off so the account is not undeletable, and
the amount, the admin, and the row counts are recorded in `pool_meta` under an
`unsubscribed:<id>:<timestamp>` key. That record outlives the rows it
describes, which is what makes it an accounted write-off rather than a
disappearance:

```bash
sqlite3 /opt/eth-trading-agent/ledger.db \
  "SELECT key, value FROM pool_meta WHERE key LIKE 'unsubscribed:%';"
```

The removed person is DM'd that their account is closed and nothing is being
held for them. Written off cash stays at the venue and becomes house residual,
so `total_tester_cash` drops by that amount and the reconciler sees more
headroom, not less — it cannot trip the shortfall freeze.

`pool_chain_deposits` rows are **detached, not deleted** (`telegram_id` nulled,
status `baseline`). The deposit sweep re-inserts any transfer it cannot find,
and past the first run a re-inserted row lands as `unmatched` — so deleting
the row would make a historical deposit resurface as money that arrived with
nobody to own it, and page you about it at the next sweep.

`deploy/_reset_test_user.py` does the same job from a shell, for when the bot
is down, and is the only path with `--force` (which overrides the balance
guard, but never the in-flight payout one).

#### Adding a test account

Use a **second Telegram account** (a spare number, or Telegram Desktop signed
in as another user) rather than your own — your admin id short-circuits the
approval path, so testing with it never exercises the gate. From the test
account, `/start` the bot, Admit it from your admin account, then
`/deposit` and tap Credit to give it play money. To reset it:

```bash
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python -c \
  "import pool; print(pool.debit(<test_id>, <usd>, admin_id=<your_id>, note='test reset'))"
```

#### Deposits go straight to Coinbase

`POOL_DEPOSIT_ADDRESS` is `0xDdA10FB6e6d726ae1cfB079CD79A4f0Ef7cAF240` — the
**Coinbase deposit address** for the USDC account, on Ethereum mainnet.
Transfers there become venue equity on arrival, which means:

- **No wallet in the middle**, so we never hold client funds in something we
  sign for. There is no sweep step and no hot key to protect.
- **No collision with the yield sleeve.** The earlier address
  (`0x6549…73B1`) is the wallet `yield_gen_bot` on **45.33.101.215** monitors;
  tester money no longer lands there, so that app's planner can never deploy a
  deposit. Nothing about the yield box needed changing to get that property.
- **The reconciler sees it immediately** (`get_cash_assets` counts the spot
  USDC wallet), so a deposit raises covered assets the moment it lands rather
  than at credit time.

If you ever change this value, run the check first — a typo has no undo:

```bash
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \
  deploy/_check_deposit_address.py 0x<new address>
```

It asks Coinbase whether it generated that address, and names the account and
network. A `NO MATCH` is not proof the address is wrong, but it *is* reason to
confirm in the Coinbase UI before anyone sends to it.

#### Withdrawals

`/withdraw 100` debits the tester immediately and — with
`POOL_AUTO_APPROVE_WITHDRAWALS` on, which is the default — **sends without
waiting for you**.

**`/withdraw all`** (also `max`, `everything`) asks for `max_withdrawal_usd`,
which is available cash less the fee reserve and less whatever the daily caps
have already used up. It is one ordinary withdrawal, subject to every check
below — the word only spares the tester doing that arithmetic in their head,
and getting it wrong in the direction that refuses. Two things it must never
do, both pinned by tests: send less than the balance without saying so, and
quote a maximum the request path then refuses. So when part of the balance is
reserved against open trades, or a cap clips the amount, the reply names the
dollars that stayed and why. If the free part is under the $50 minimum nothing
is queued at all, and the reply says the money is in trades rather than
leaving them to wonder. `watchdog._payout_sweep` picks it up on the next 60s pass,
one payout at a time, and the tester has it about a minute later. You still
get the notification, but it carries no buttons: `decide_withdrawal` only acts
on a `requested` row, so an Approve button on an already-approved payout would
do nothing.

You are not skipping a safety check by not being in the loop. Everything that
decides whether a payout may happen runs at request time and is unchanged: the
halt switch, `POOL_PAYOUTS_ENABLED`, the $50 minimum, the per-request and
per-user-daily and global-daily caps, a chain-verified destination, and the
available balance read under the write lock. The approval step was arbitrating
a decision no human was making, and what it added in practice was however long
it took you to see the message.

**If you want the gate back**, set `POOL_AUTO_APPROVE_WITHDRAWALS = False` and
restart: requests land as `requested` again and you get the **Send / Reject**
card, where approving only *queues* and rejecting refunds in full. That is the
switch to reach for if a specific payout needs a human look — `/payouts halt`
is the blunter one that stops the whole queue.

The thing to know before you touch it: **Coinbase offers no idempotency on
sends.** It rejects the `idem` parameter outright, so a resend is a second
real payment and nothing at the far end merges them. That shapes the rules:

- **Never resend a payout by hand.** If a send ends ambiguously the queue
  **halts itself** and the row goes to `unknown`. It is deliberately *not*
  refunded and *not* retried — refunding could hand back money that already
  left, retrying could send it twice, and Coinbase will not tell you which.
  Check the USDC balance and the destination on-chain, then `/payouts resume`.
- `/payouts` shows the queue and the halt state; `/payouts halt` stops it.
- A payout's status **cannot be read back** from the API (single-transaction
  GET 404s), so `watchdog._settle_sweep` confirms it the only independent way
  available: by finding the arrival at the tester's own address on-chain. It
  then marks the withdrawal `settled` and DMs them the transaction hash. Our
  balance dropping only proves the money left, not where it went.

Fees: the network fee is charged **on top** of the send and paid by the
tester, so the pool's books stay level with the venue. A $3 reserve is held at
request time and refunded once the real fee is known (measured at ~$0.148 on
Ethereum). Minimum withdrawal is $50 because the fee is flat.

**Speed, measured rather than assumed:** the $2 test send returned at 21:20:13
UTC and was in the destination wallet at 21:21:11 — **58 seconds**. The
earlier "about 10 minutes" was the interval at which I was checking the
Coinbase balance, not anything about the payout. Tester copy quotes **under
five minutes** against that 58-second measurement: someone told "five" who
waits one is delighted, someone told "one" who waits three starts wondering
where their money went.

Deposit copy quotes **~5 minutes** on the same principle, and that one is a
genuine estimate rather than a padded measurement — the wait is Ethereum
confirmations plus Coinbase crediting, neither of which we control, so the
message says a busy network can make it longer. Our own leg is fast: the
watchdog credits and DMs within 60 seconds of the transfer settling.

`deploy/_show_money_copy.py` prints every deposit and withdrawal message a
tester sees, plus the limits in force. Read-only — it sends nothing.

#### Wallet verification gates every withdrawal

A tester can only be paid at an address they have **proven** they control, and
the proof is that their deposit arrived from it. Coinbase does not report a
sender, so this is settled on-chain: `watchdog._wallet_verify_sweep` looks up
the deposit's transaction, reads the actual sender, and promotes the wallet to
`verified` if it matches what they registered. Needs `ETHERSCAN_API_KEY` —
without it nothing can be proven and every withdrawal refuses as
`unverified`, which is safe but stuck. Check a new key with
`python3 deploy/_verify_chain.py`.

When it does **not** match you get a `WALLET UNPROVEN` alert. This is
usually not fraud — the common cause is a tester funding from a Coinbase or
Binance account instead of their own wallet, so the sender is the exchange's
hot wallet. Their deposit is credited and safe either way; only withdrawals
are affected. Two ways out, and pick based on what you can actually confirm:

- They deposit again from their own wallet, which proves it properly.
- They re-register the address the funds truly came from — only sound if that
  address is genuinely theirs. Never an exchange hot wallet: those funds would
  land in a pooled account that is not theirs and be unrecoverable.

A lookup failure is **not** a mismatch. An Etherscan outage leaves the wallet
`pending` and is retried, rather than being recorded as a failed proof, so a
bad afternoon at Etherscan never refuses an honest tester their own money.

#### Demo cards

HQ runs every 30 minutes and most cycles legitimately find no trade, so a real
trade card cannot be summoned for a recording or a walkthrough. Send one with
the admin command **`/democard`**, or with `deploy/_send_demo_card.py` if you
want it scripted. Either way it is labelled as a demo and built by the **live**
card renderer with the **live** sizing rule, so the size it quotes is the size
a real card would quote for that account.

`/democard` takes its arguments in any order, because it gets typed live:

| Command | Sends |
|---|---|
| `/democard` | synthetic BTC long, to yourself |
| `/democard 8708390551` | same, to that tester |
| `/democard eth short` | synthetic ETH short |
| `/democard live 8708390551` | **mirrors the newest open position** |
| `/democard mill` / `/democard hq` | mirrors the newest open trade in that book |
| `/democard 85` | mirrors live trade #85 specifically |
| `/democard all live` | mirrors it **to every approved account** |
| `/democard real` | **a real, fillable mill card — Accept places a real trade** |
| `/democard real 57` | the same, aimed at mill idea #57 |
| `/democard scan` | fill verdict on each recent idea; sends nothing |

Telegram ids are long and trade ids are short, which is what keeps `85` and
`8708390551` apart. Without `all` it goes to one account — yourself by default,
which is why a bare `/democard mill` only reaches you.

`all` renders a separate card per recipient rather than reusing one, because
the size line is personal; the reply back to you says how many were funded
enough to see a real size and names anyone who could not be reached.

**Mirror mode is the one to use for anything measurement-like.** It copies a
real open position's entry, its *initial* stop, its *original* target ladder,
and its original rationale — the mill idea title, or the HQ `suggestions`
rationale. Nothing is invented, so the quoted size is genuinely the size that
account would have taken on that trade. Two things to know: the stop and
targets are the planned ones rather than the current trailed ones, which is
what the trade was sized against; and a mirrored entry can sit well behind
spot, since the real trade opened earlier. The command replies with that drift
when it exceeds 0.5%, so you find out before filming rather than on playback.

Accept is real: it reserves the tester's actual budget through
`pool.record_intent`, so the reply they see is the genuine one. It cannot
trade, and that is structural rather than careful — every executor resolves
pooled intents *by ref* (`pending_intents(ref)`), and the ref here is
`demo_<token>`, which matches no live pending cycle id and no `mill_<id>`.
There is no code path from a demo ref to a position. About a minute later the
stale-intent sweep sees a ref that is not active, returns the reserve, and
sends the real "that order never fired, your money is back" message — which is
worth showing, since it is how a non-filling Accept always behaves.

Safe to press with real money in the account — including on a mirror of a live
position, since the mirror copies levels but never touches the real trade.

**`/democard real` is the exception, and it is not a demo.** A demo card can
never fill, so if you need a genuine fill on camera this sends an actual mill
card: real levels, the real `idea:accept:<id>` callback, and a real trade with
real money if it is tapped. The banner says LIVE CARD rather than DEMO CARD —
a card that spends money must never be labelled a demo, which is the one
combination worse than either alone.

It picks the newest idea that would fill *right now*, checked by running the
real gates in dry-run mode. Treat that as a strong no and a weak yes: exposure,
contract-floor and dedupe checks only run when an order is genuinely sent, so a
card can still be refused after passing the preview. If nothing is fillable the
command says so instead of sending a card that will bounce — and it names the
ideas it looked at with a plain-language reason for each, because "nothing
right now" cannot tell a market that has moved from a mill that has stopped,
and that is the question being asked ten minutes before a recording.

`/democard scan` asks that question without sending anything: the same verdicts
as `deploy/_show_fillable.py <id>`, in Telegram, ending in the exact
`/democard real <id>` to type next when there is one. A number beside `real`
aims at that mill idea — `/democard real 57` — which still goes through the
gate, so naming an idea asks for it rather than forces it. On that path the
number is an idea id, not a live trade id, since there is nothing to mirror
when the card *is* the idea.

`real` is not a synonym for `live` — `/democard live` still means "mirror an
open position", and that word is already in use.
Pinned by `DemoCardTests` and `SendDemoCardTests`, including that a demo intent
contributes nothing to a real order, and that `/democard` is admin-only (it can
address any telegram id, so a tester must not be able to card another tester).

#### The old demo paper book is off for funded accounts

`user_books` is the pre-pool product: a personal $500/$1,000/$2,500 demo
account with its own Accept, its own ledger, and "missed connection" invites to
join a trade late. It is switched off for anyone the pool has approved, because
showing a real depositor a second imaginary balance is the fastest way to make
them doubt the first one. Concretely, when `POOL_ENABLED`:

- **Open account / My Metrics / My book are gone** from the button menu,
  replaced by **Portfolio / Deposit**. Agent journal stays — it is read-only.
- **Those buttons are also refused if tapped**, not just hidden. Telegram keeps
  old inline keyboards alive forever, so a card from last week is still live in
  someone's scrollback. The refusal points at `/portfolio`, `/deposit`,
  `/withdraw`.
- **Missed-connection DMs are not sent to funded accounts at all.** Join now
  enters at the current mark against the *original* stop, which is the chase
  `LIVE_MAX_CHASE_R` exists to stop. Fine with pretend money; not something to
  put in front of real money.

This is what produced "I clicked a missed connection and it opened a paper book
at $2,500" — Join now refused with `no_account`, and the refusal arrived
carrying the menu whose first button opens a demo account.

Four legacy demo books still exist in `user_accounts` (all opened Jul–Aug,
before the pool). They are unreachable now and are left alone rather than
deleted, since they hold real trade history. To remove one:

```bash
sudo -u ethagent .venv/bin/python deploy/_drop_demo_book.py <telegram_id>
sudo -u ethagent .venv/bin/python deploy/_drop_demo_book.py <telegram_id> --delete
```

It dry-runs by default and touches `user_*` tables only, so it cannot reach
`pool_*` or `live_trades` — real balances are out of its scope by construction.

#### Rehearsing the whole thing

`deploy/_rehearse_tester.py` walks one tester from `/start` to settled
withdrawal — registration, deposit claim, auto-credit, on-chain verification,
the caps, the refund, and settlement — plus the exchange-deposit mismatch. It
calls the shipped sweeps rather than reimplementing them and prints the exact
DMs a tester and an admin would receive, so it doubles as a copy review.

The chain reads are **real**: real Etherscan lookups against the real
transfers on the deposit address and the real $2 payout. The ledger is a
scratch file, so no balance moves and the reconciler never sees a claim
without venue cash behind it. Safe to run any time.

It cannot prove the two things that need money in motion — a brand-new deposit
arriving at Coinbase, and a brand-new payout leaving. Each has been
demonstrated separately with real funds.

#### Credits are automatic

You are no longer in the path. `watchdog._deposit_sweep` runs every 60s, reads
settled inbound transfers on the deposit address, and credits any whose
**transaction hash** matches a filed `/deposit` claim — the tester is DM'd
within a minute of settlement, and you get an FYI. The Credit card still
exists, but tapping it now means "book this *before* it has arrived", which
gives a tester a claim the venue cannot yet cover. Normally, leave it alone.

Coinbase reports **no sender** for an incoming transfer, which is why the hash
is the attribution key and why arrival does not verify a wallet. Two things
follow that are worth knowing before you get an alert about them:

- **An unclaimed transfer is never apportioned.** Money that arrives with no
  matching claim is recorded and you are pinged once. Nobody is credited,
  because guessing an owner from an amount is how one tester ends up with
  another's money. Resolve it with `/assign <coinbase_tx_id> <telegram_id>`
  (bare `/assign` lists what is outstanding).
- **The credited amount is Coinbase's, not the tester's claim.** If they say
  $1,000 and send $900, they get $900 and you get a CHECK THIS alert.

`pool.pending_inbound_usd()` is still the amount claimed but not yet credited,
i.e. the slice of apparent house residual that is really a tester's.

#### Registered wallets and return-to-source

Every tester binds the address they fund from (`/wallet 0x…`) before they can
`/deposit`. It does two jobs: it is how an arriving transfer is attributed to a
person by its **sender** rather than by what they typed, and it is the only
address a withdrawal may ever return to.

| State | Meaning | Payout allowed |
|---|---|---|
| `pending` | registered, but nothing has arrived from it | no |
| `verified` | a deposit arrived from it, proving control | yes |
| cooldown set | an admin approved an address change | no, until it expires |

A wallet is verified only by passing the real sender into
`pool.decide_deposit(..., sender=...)`. Tapping Credit alone does **not** verify
it, deliberately: the tap means you saw funds arrive, not that you saw where
from, and that distinction is the only thing standing between a payout and an
address nobody has proven they control. The chain watcher will supply the
sender automatically; until then wallets stay `pending`, which costs nothing
while there is no withdrawal path.

Changing an address needs an admin tap and then holds payouts for
`POOL_WALLET_COOLDOWN_HOURS` (24h). Re-pointing the payout address is the first
thing an account takeover would do, so confirm out-of-band that a change
request is really the tester before approving. The tester is DM'd on both the
request and the approval, which is what gives them a chance to object.

#### If the reconciler freezes intents

The watchdog checks every ~10 minutes that Coinbase equity covers the sum of
tester cash. On a shortfall past `POOL_RECON_TOLERANCE_USD` it freezes NEW
Accepts (open trades keep booking exits; nothing is auto-adjusted) and DMs
the admins. Audit `pool_events` against the Coinbase ledger, fix the cause
(usually an uncredited deposit or an unrecorded manual withdrawal — use
`/credit` / `/debit` to book it), then:

```bash
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python -c "import pool; pool.unfreeze_intents()"
```

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

### Restart the mill volume paper epoch (daily digest)

The 5pm ET "you'd be up X%" post is the **mill volume paper book** from `MILL_PAPER_EPOCH_START` (default `2026-09-01`), not Eva HQ paper and not the live mill clip. To drop pre-epoch mill paper from `/volume` as well:

```bash
cp /opt/trade-ideas/ideas.db ~/ideas-backup-$(date +%Y%m%d).db
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \
  /opt/eth-trading-agent/deploy/reset_mill_paper_epoch.py --dry-run
sudo -u ethagent /opt/eth-trading-agent/.venv/bin/python \
  /opt/eth-trading-agent/deploy/reset_mill_paper_epoch.py --yes
sudo systemctl restart eth-dashboard
```

Rows opened before the cutoff move to `paper_trades_archive`. Personal `/me` books are unchanged.

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

### Public HTTPS link — live at `https://dashboard.eva.finance`

`dashboard.eva.finance` is an **A record → 45.33.97.27** served by Spaceship DNS (`launch1/launch2.spaceship.net`). Caddy terminates TLS on the VPS and reverse-proxies to `localhost:8080`; the dashboard process itself is unchanged and still binds 8080.

To reproduce on a new box or domain:

1. Add a DNS **A record** pointing to the VPS IP (e.g. `dashboard` → `45.33.97.27`). If the domain was transferred in from another registrar, check the **nameserver** setting as well — records added in the registrar's DNS panel do nothing while the domain is still delegated elsewhere, and the panel will usually flag the record group as inactive.
2. Install Caddy. It is **not** in Ubuntu's default repos (22.04 or 24.04), so add the Cloudsmith repo first:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

3. Write `/etc/caddy/Caddyfile` and reload:

```text
dashboard.eva.finance {
    reverse_proxy localhost:8080
}
```

```bash
sudo systemctl reload caddy
journalctl -u caddy -n 40 --no-pager   # expect "certificate obtained successfully"
```

Ports **80 and 443** must both be reachable from the internet — 80 carries the ACME HTTP-01 challenge, so HTTPS will not issue without it. Check `ufw status` *and* the Linode cloud firewall. A transient `HTTP 404 ... Certificate not found` logged right after a successful challenge is a known Let's Encrypt race; Caddy retries after 60s and obtains the cert.

Caddy redirects plain HTTP to HTTPS automatically (308), so `http://dashboard.eva.finance` also works.

Set `DASHBOARD_PUBLIC_URL=https://dashboard.eva.finance` in `/opt/eth-trading-agent/.env` — full HTTPS URL, no trailing slash or path — then restart `eth-agent` so Telegram's **Agent journal** and **My book** links use it. Telegram requires HTTPS for those link buttons; a raw `http://IP:8080` value will not work.

Once HTTPS is confirmed, close the raw port so nobody can reach the dashboard unencrypted by hitting the IP directly:

```bash
sudo ufw delete allow 8080/tcp
```

### Marketing site — apex `eva.finance` (eva-web)

The marketing site is a separate static build (repo `eva-web`, sibling of this
one) served by the same Caddy at the **apex** domain. It never links to
`dashboard.eva.finance`; its `/api/*` calls are proxied by Caddy to this
dashboard process, which serves them from `dashboard/public_api.py`
(`GET /api/public/strategies`, `POST /api/public/beta`).

**The VPS has no Node.** The site is built on a workstation and only the
static `dist/` is shipped, which is why there is no `eva-web` checkout on the
server. Served from `/var/www/eva-web`, owned by `caddy`.

#### 1. DNS (Spaceship)

**A record** for `@` (apex) *and* `www` → `45.33.97.27`, same pattern as
`dashboard`. Caddy cannot issue a certificate until both resolve; until then
it retries on a backoff for 30 days and logs
`NXDOMAIN looking up A for eva.finance`. Check with:

```bash
dig +short eva.finance A @1.1.1.1
dig +short www.eva.finance A @1.1.1.1
```

#### 2. Build on the workstation, ship `dist/`

The build-time numbers must come from the **production** ledgers, so generate
the snapshot on the VPS and pull it back before building:

```bash
# on the VPS — snapshot the live books
scp eva-web/scripts/build_snapshot.py root@45.33.97.27:/tmp/
ssh root@45.33.97.27 'mkdir -p /tmp/evaweb/scripts /tmp/evaweb/src/data &&
  cp /tmp/build_snapshot.py /tmp/evaweb/scripts/ && cd /tmp/evaweb &&
  python3 scripts/build_snapshot.py /opt/eth-trading-agent/ledger.db \
    /opt/trade-ideas/ideas.db /opt/kalshi-15m-bot/ledger.db'

# back on the workstation
scp root@45.33.97.27:/tmp/evaweb/src/data/strategies.json eva-web/src/data/
for id in 8 18 19 25; do
  scp "root@45.33.97.27:/opt/eth-trading-agent/charts/case_study_hq_$id.png" \
      eva-web/public/case-studies/
done
cd eva-web && npm ci && npm run build
```

Ship it atomically so a half-copied tree is never served:

```bash
ssh root@45.33.97.27 'rm -rf /var/www/eva-web.new && mkdir -p /var/www/eva-web.new'
scp -r dist/* root@45.33.97.27:/var/www/eva-web.new/
ssh root@45.33.97.27 'rm -rf /var/www/eva-web.old &&
  mv /var/www/eva-web /var/www/eva-web.old &&
  mv /var/www/eva-web.new /var/www/eva-web &&
  chown -R caddy:caddy /var/www/eva-web'
```

`/var/www/eva-web.old` is the previous build — roll back by swapping it back.

#### 3. Caddy

The live config is kept in `deploy/caddy_eva_finance.snippet`; install it with
`deploy/_install_caddyfile.py` (backs up, normalises CRLF, validates **as the
caddy user**, then reloads). Two traps worth knowing:

- **Never run `caddy validate` as root.** It creates
  `/var/log/caddy/eva-finance.log` owned by `root:root`, and the service —
  which runs as `caddy` — then fails to reload with `permission denied`.
- **No `/404.html` in `try_files`.** Falling back to it there serves the error
  page with a `200`, making every typo look like a real page to crawlers.
  The miss is left to become a genuine 404 and is caught by `handle_errors`,
  which re-serves `/404.html` with `status {err.status_code}`.

`handle_path` strips the `/api` prefix and re-adds `/api/public`, so the
site's `fetch("/api/…")` reaches the public router and **only** the public
router — no other dashboard route is exposed at the apex.

#### 4. `.env` for the beta form (then restart `eth-dashboard`)

```bash
RESEND_API_KEY=...            # shared with the ops alerts
ALERT_EMAIL_FROM=...          # see the delivery caveat below
# BETA_SIGNUP_EMAIL_TO=a@x,b@y   # default: abagui@ + daniel@ republictech.io
```

> **Delivery caveat (currently live).** `ALERT_EMAIL_FROM=onboarding@resend.dev`
> is a Resend *test* sender: it only delivers to the Resend account owner
> (`abagui@republictech.io`). Sending to `daniel@republictech.io` returns
> `403 validation_error`. The notifier therefore sends **one request per
> recipient** — a single batched call is rejected outright when any recipient
> is undeliverable, which would silently notify nobody. As it stands
> `abagui@` is notified and `daniel@` is not, and each rejection is logged at
> ERROR. To fix properly: verify `republictech.io` at resend.com/domains and
> set `ALERT_EMAIL_FROM` to an address on that domain.

Signups are stored in `ledger.db` table **`beta_signups`** regardless of email
delivery (the email is a notification, not the record). Read them with:

```bash
sqlite3 /opt/eth-trading-agent/ledger.db \
  "SELECT created_at, email, name, note FROM beta_signups ORDER BY id DESC;"
```

#### Verifying a deploy

`deploy/_verify_evaweb.sh` checks services, the dashboard, the public API,
the files on disk, and end-to-end DNS/TLS. `deploy/_routing_check.sh` proves
the routing block (clean URLs, assets, real 404, `/api/*` rewrite, beta POST)
by mounting the same handlers on `http://localhost:8099`, then removing the
temporary block — useful **before** DNS exists.

To refresh the site's built-in numbers later, rerun `build_snapshot.py` and
`npm run build` (the pages also hydrate live from `/api/strategies` on load,
so between rebuilds only the charts and captions trail).

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

A read-only page for investors, not linked from the hub and marked `noindex,nofollow`. It shows Eva's portfolio value (week/month/year chart), realized gain for the day and year to date, unrealized, Coinbase's **CDE Cross Margin ratio** (maintenance ÷ funds for margin; liquidation at 100%), every open Eva position with remaining size (ETH and $ notional), liquidation price (or n/a), take-profit ladder and current stop, per-day realized P&L, and — on each **closed** Eva trade — an annotated case-study chart of the fill (entry, stop, take-profits, post-trade note). Mill clips, the raw Coinbase account, and the paper books are not on this page (the margin pill is whole-account because that is what Coinbase would liquidate).

Gate it before sharing the URL:

```bash
# on the server
cd /opt/eth-trading-agent
openssl rand -hex 16          # use the output as the token
nano .env                     # INVESTOR_ACCESS_TOKEN=<paste>
sudo systemctl restart eth-dashboard
```

Then share `https://dashboard.eva.finance/investors?k=<token>`. The first visit sets an httponly cookie (30 days, `INVESTOR_SESSION_TTL_SEC`), so reloads and in-app navigation work without the query string. Any request without a valid token gets a **404**, not a 401, so a guessed URL never confirms the page exists. To revoke access, change the token and restart the dashboard.

Leaving `INVESTOR_ACCESS_TOKEN` unset keeps the page reachable to anyone who knows the path — the same unlisted-only posture as `/volume`. Set it before sending the link to anyone outside the team.

The page sizes Eva off `LIVE_HQ_EQUITY_USD`. It does not read Coinbase balances; `deploy/diagnose_live.py` is the operator tool for the real futures wallet.

### Macro headline webhook (optional push ingest)

Push headlines into the same pipeline as RSS (keyword score → Haiku classify → pulse if severity ≥ 4).

1. Set `MACRO_WEBHOOK_SECRET` in `/opt/eth-trading-agent/.env`
2. POST to the dashboard (HTTPS via Caddy recommended):

```bash
curl -X POST "https://dashboard.eva.finance/api/macro/ingest" \
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
curl "https://dashboard.eva.finance/api/ops/watchdog-execute"

# Enable paper fills (Bearer = MACRO_WEBHOOK_SECRET)
curl -X POST "https://dashboard.eva.finance/api/ops/watchdog-execute" \
  -H "Authorization: Bearer YOUR_MACRO_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"enabled":true}'
```

**Telegram (admin/monitor only):** `/watchdog status` · `/watchdog on` · `/watchdog off`

Runtime override is stored in SQLite meta (`watchdog_execute_enabled`); config default remains `WATCHDOG_EXECUTE_ENABLED=False`.

**Ops note:** if an oversized watchdog BTC short is still open after deploy, flatten or hard-cap it manually before re-enabling execute.

### Eva variant experiment (Eva Lab tab)

Three extra Eva books run as **paper only** alongside the live bot: `eva_swing_mech`, `eva_swing_llm`, `eva_day`. They write to their own tables in `ledger.db` (`variant_positions`, `variant_trades`, `variant_skips`) and cannot place an order. `paper.py` is untouched — control *is* `paper.py`.

All flags live in `bot_config.py`, not `.env`. No new secrets, no new services, no schema change to any existing table (the tables are created on first use by `eva_variants.init_db()`).

**Two new scheduler jobs** appear in the journal on start:

```bash
journalctl -u eva-bot -n 200 --no-pager | grep -i "eva "
# Eva variants enabled — day scan every 120s, live variant=control
# Eva swing-LLM arm enabled — every 7200s
```

- `eva_day_scan` (every 120s) — also drives **mark-to-market for every variant book**, so it must stay enabled even if `EVA_DAY_M1_TRIGGERS_ENABLED=False`, or the swing books never resolve their exits.
- `eva_swing_llm_cycle` (every 2h) — the only new LLM spend: ~12 vision calls/day. Deliberately not folded into the 30-min cycle, because one Claude call cannot show H12/D1 to the swing mandate while hiding it from control.

**Check the books are recording:**

```bash
curl -s "https://dashboard.eva.finance/api/eva/variants" | python3 -m json.tool | head -40
sqlite3 /opt/eva-bot/ledger.db \
  "SELECT variant, status, COUNT(*) FROM variant_positions GROUP BY 1,2;"
sqlite3 /opt/eva-bot/ledger.db \
  "SELECT variant, reason, COUNT(*) FROM variant_skips GROUP BY 1,2 ORDER BY 3 DESC LIMIT 10;"
```

**Sanity check that catches a broken exit engine:** no position may close before it opens.

```bash
sqlite3 /opt/eth-trading-agent/ledger.db \
  "SELECT COUNT(*) FROM variant_positions
   WHERE closed_at IS NOT NULL AND closed_at <= opened_at;"   # must be 0
```

A non-zero count, or a negative `median_hold_h` on the Eva Lab tab, means the
M5 walk is resolving positions on bars that predate their entry. That shipped
once, on 2026-09-14: Coinbase honours `limit` ahead of `start`, so a
three-minute candle request returned 350 bars covering 29h and positions were
stopped by price action from before they existed. `fetch_coinbase_candles_range`
now filters to the requested window. The same failure is **invisible in
`paper_trades`**, which stamps closes at cycle time rather than bar time — to
check control, ask the function instead of the table:

```bash
cd /opt/eth-trading-agent && .venv/bin/python - <<'PY'
import datetime as dt, paper
since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)
         ).strftime("%Y-%m-%dT%H:%M:%SZ")
bars = paper._m5_path("BTC-USD", since)
print(len(bars), "bars; oldest", bars[0]["ts"] if bars else "-")
PY
# healthy: a couple of bars. broken: 350 bars reaching ~29h back.
```

The skip table is the first place to look if a book is empty — it records *why* each trigger did not open (`no_stance`, `stance_no_trade`, `no_structure_break`, `no_open_fvg`, `price_outside_fvg`, `cooldown`, `rejected:*`). An empty `eva_day` book with thousands of `stance_no_trade` skips is the gate working, not a bug.

**Kill switch** — stops all variant writes immediately; control is unaffected either way:

```bash
# edit bot_config.py: EVA_VARIANTS_ENABLED = False
systemctl restart eva-bot
```

**Promotion is not an ops action.** `EVA_LIVE_VARIANT` is the only book allowed to touch real money and is `control`. Do not change it on the basis of the dashboard leaderboard — the tab labels its front-runner a candidate for a reason. The bar is in `deploy/EVA_VARIANTS_PREREG.md` §4 (≥60 closed positions, day-clustered bootstrap CI excluding zero, placebo beaten, mechanism metric moved, structural review).

**Re-basing the epoch** (only if a registered parameter changes, which invalidates the experiment):

```bash
# 1. edit bot_config.py: EVA_EXPERIMENT_EPOCH = "YYYY-MM-DD"
# 2. archive the old book so the two geometries are never blended
sqlite3 /opt/eva-bot/ledger.db \
  "UPDATE variant_positions SET variant = variant || '_preepoch' WHERE status = 'closed';"
systemctl restart eva-bot
```

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

Then open `https://dashboard.eva.finance`. If Caddy is not up, fall back to `http://YOUR_SERVER_IP:8080` (allow port 8080 in the cloud firewall if needed).

---

## Checklist

- [ ] Local `main.py` stopped before starting cloud
- [ ] `.env` has `PAYWALL_ENABLED=false` for beta (or an allowlist when `true`)
- [ ] `.env` has the public HTTPS `DASHBOARD_PUBLIC_URL`
- [ ] `TELEGRAM_CHAT_ID` empty or different from allowlist IDs
- [ ] `systemctl status eth-agent` shows **active (running)**
- [ ] You received an hourly DM on Telegram
- [ ] Beta onboarding tested: share bot link → user sends `/start` → inline keyboard appears
