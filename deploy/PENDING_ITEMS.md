# Phase 1 — pending items (operator to-do)

Everything in Phase 1 is built and config-gated. Each item below is an
external step only you can do; the code path it unlocks is already written
locally. Nothing breaks while an item is pending — the related surface just
stays in its current (feed-only / MoonPay-pending) state.

## Done

### 1. Test wallet address — DONE

- Address: `0xab1b6CC522C3EC7BdEa22598f6e510e7e752479d` (MetaMask EOA —
  same address on Base and Ethereum; **not** the Coinbase deposit address)
- Set in local `.env` and on the VPS at `/opt/eth-trading-agent/.env` as:
  `TEST_WALLET_ADDRESS=…` + `TEST_WALLET_CHAIN_ID=8453`; both chains are
  swept by default (`TEST_WALLET_CHAIN_IDS=8453,1`)
- `ETHERSCAN_API_KEY` present on the VPS, but its **free plan does not serve
  Base** — Base reads fall back to the public RPC (`BASE_RPC_URL`). If the
  public node starts rate-limiting, either upgrade Etherscan or point
  `BASE_RPC_URL` at a paid endpoint (Alchemy/Infura/QuickNode free tiers work).
- Private key stays in MetaMask only — never on the server

### 0. Deploy Phase 1 code — DONE

- Pushed `3009b33` to `origin/main` and ran `deploy/update.sh` on the VPS
- `treasury.py` / Kalshi gateway / Fund fallback are live; `eth-agent` active

**Quick verify (you):**

1. Telegram → Fund shows `0xab1b6…479d`
2. Admin `/treasury` lists the test wallet balance
3. A small Base USDC send from a *registered* `/wallet` credits that user

### 2. Kalshi API credentials — DONE (reused existing bot key)

- The colocated `/opt/kalshi-15m-bot` already had full Trade API access
  (`KALSHI_API_KEY_ID` + `secrets/kalshi_prod.key`).
- Hub now points at a copy at `/opt/eth-trading-agent/secrets/kalshi.pem`
  with matching `KALSHI_API_KEY_ID` / `KALSHI_API_BASE` in the hub `.env`.
- Verified live: gateway configured, balance readable (~$229), both Kalshi
  lanes `is_executable=True`.

**Still confirm before treating Accepts as production-ready:**

- **Funding rail**: how the house Kalshi account gets topped up from the test
  wallet (USDC vs ACH/wire). Treasury journals either; Kalshi arrivals need
  `/transfer_confirm` because they are not chain-visible to us.
- **Deploy routing signer is LIVE (2026-10-06).** `.env` on the hub has
  `TEST_WALLET_PRIVATE_KEY` (derives to `0xab1b…479d`, verified),
  `POOL_DEPOSIT_CHAIN_ID=1`, `KALSHI_DEPOSIT_ADDRESS=0x8a5c…281a`,
  `KALSHI_DEPOSIT_CHAIN_ID=1`. Deploys auto-send (`TREASURY_AUTO_SEND_DEPLOYS`),
  withdrawals pay from the test wallet when it holds the balance on a chain
  the tester deposited from, Coinbase otherwise. **Gas:** send ~0.01 ETH once
  as bootstrap; after that `GAS_TOPUP_ENABLED` buys ETH from free USDC on
  deploy (Uniswap), and pending deploy legs retry on the watchdog without a
  Send tap. **Still needed before the first real send:** confirm mainnet ETH
  is visible on the test wallet (operator sent ~$30 on 2026-10-06).
- **Base-deposited capital cannot reach either venue without a bridge.** Both
  venue addresses are Ethereum-only; the signer sends from the wallet's
  balance on the destination chain and will refuse a Base-funded deploy with
  "wallet holds $0 on Ethereum". Options: say Ethereum-only in the Fund copy
  (drop 8453 from `TEST_WALLET_CHAIN_IDS`), or bridge by hand when it happens.
  Not decided.
- **Kalshi-deployed capital is not withdrawable by either rail** until it is
  back in the test wallet (`/transfer kalshi test_wallet`, withdraw on
  Kalshi's side, `/transfer_sent`). A Kalshi-heavy tester asking for more
  than the Coinbase float will see the payout fail-and-refund.
- **Card token contract** with the kalshi_15m_bot relay: hub resolves
  `kalshi:accept:<key>:<token>` as `paper_positions.id` (numeric) with a
  10-minute freshness fallback to the latest open entry. Align if the relay
  token is something else.
- After first live fills: compare booked fees to Kalshi statements (code uses
  the published taker formula, rounded up).

## Next (in order)

### 3. Card on-ramp provider — unlocks credit-card deposits

Every fiat→crypto provider KYCs the *buyer* (their regulatory burden, not
ours), and every one requires a business/partner account to issue widget
keys — there is no card rail without some onboarding. Direct USDC transfers
(item 1, once code is deployed) work with zero provider. When ready, pick
one that can:

1. pin the destination wallet address (the test wallet above),
2. carry an external customer id (`tg_<telegram_id>`) through to its
   webhook/receipt, and
3. deliver USDC on Base.

Candidates: MoonPay standard buy widget (existing relationship), Coinbase
Onramp, Transak, Ramp Network. Then set `ONRAMP_WIDGET_URL_TEMPLATE` in
`.env` — the Fund surface picks it up and the `onramp_sessions` table is
already in place for webhook attribution (webhook handler to be added when
the provider's payload shape is known).

### 4. MoonPay Commerce approval — Phase 2 cutover

Still pending with MoonPay. When it lands: production `MOONPAY_*` keys,
webhook registration, per-user deposit addresses take over the Fund surface
automatically (the code prefers MoonPay whenever it is configured), and the
test wallet becomes fallback-only. Runbook in the roadmap plan, Phase 2.

### 5. FalconX migration — new key probed 2026-10-05; v3 quotes blocked on FalconX side

Venue code stays behind gateway shapes (`DerivGateway`, `kalshi_gateway`)
so FalconX arrives as a third gateway plus a treasury location, not a
rewrite.

**Probe findings** (`deploy/_probe_falconx.py`, run from the VPS — key is
IP-locked to `45.33.97.27`; creds in root-only
`/opt/eth-trading-agent/secrets/falconx.env`, never in the repo):

- Auth works (Coinbase-Pro-style HMAC: `FX-ACCESS-KEY/SIGN/TIMESTAMP/
  PASSPHRASE`). Account: *Republic Technologies Inc. — Bravo Swaps*,
  `subaccount_name: "Main Account"` — note the key drawer said *Spot Sub
  Account*; confirm scoping with FalconX before funding.
- 63 spot pairs incl. BTC/ETH/SOL/XRP/DOGE vs USD — covers HQ and the
  Mill majors. Mill ideas on long-tail alts need a per-symbol pair check.
- Balances/portfolio/transfers endpoints live (all empty — unfunded).
  Trade limits: $1M gross line available, $0 net.
- USDC deposit address exists (`0x124404…d602`, Ethereum network) —
  future treasury intake leg; `/v1/transfers` maps onto the treasury
  journal for reconciliation.
- **Order model is the big constraint:** REST supports RFQ
  (quote→execute), `market`, and `limit` **fill-or-kill only**. No resting
  limits, no stops, no TPs, no brackets. Eva's bracket logic would have to
  be synthesized bot-side (watch price, fire FOK/market at levels) — a
  real engine change vs Coinbase where brackets rest on the venue.
- Kalshi lanes do **not** map — FalconX has no event contracts; Kalshi
  stays on the Kalshi API regardless.
- Current key is read-only: a test RFQ returned `ACCOUNT_READ_ONLY`
  ("does not have permission to trade"; FalconX support is auto-notified
  on such attempts). To execute: issue a key with *Execute Trades*, have
  FalconX enable trading on the account, and fund it.

**Second key probed 2026-10-05 / re-scoped 2026-10-08:**

- **IPv6 still not whitelisted.** Forced egress from
  `2600:3c02::2000:87ff:fe27:13b8` still returns `REQUEST_IP_RESTRICTED`
  on `/v1/account_info`. Default Python/`requests` prefers AAAA, so any
  FalconX client on this VPS must force IPv4 (`deploy/_probe_falconx_v4.py`
  / `socket.getaddrinfo` → `AF_INET`) until FalconX adds the IPv6.
- **IPv4 v3 is unlocked (2026-10-08).** Over `45.33.97.27`,
  `POST /v3/quotes` and `POST /v3/order` now reach the trade service
  (no longer IP-restricted). Quotes return live `buy_price`/`sell_price`
  for ETH/BTC/SOL with ~2s expiry; status is `failure` with
  `MAX_EQUITY_BREACH` because the account is unfunded (`net_limits`
  available = 0). Market + limit-FOK orders accept the same way — not
  `ACCOUNT_READ_ONLY`, so Execute Trades looks enabled; funding / equity
  line is the remaining FalconX-side gate.
- Usable now for this project (read + quote plumbing): account_info,
  pairs (54 USD/USDC bases incl. BTC/ETH/SOL), balances, trade limits,
  transfers + USDC deposit address (`0x124404…d602`, ETH), RFQ quotes,
  market / limit-FOK order shapes. Still **not** usable as a drop-in for
  Eva HQ/Mill brackets (no resting stop/TP on REST) or for Kalshi event
  lanes. Repro: `deploy/_probe_falconx_scope.py`.
