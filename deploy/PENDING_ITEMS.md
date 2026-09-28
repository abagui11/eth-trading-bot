# Phase 1 — pending items (operator to-do)

Everything in Phase 1 is built and config-gated. Each item below is an
external step only you can do; the code path it unlocks is already written
locally. Nothing breaks while an item is pending — the related surface just
stays in its current (feed-only / MoonPay-pending) state.

## Done

### 1. Test wallet address — DONE

- Address: `0xab1b6CC522C3EC7BdEa22598f6e510e7e752479d` (MetaMask, Base)
- Set in local `.env` and on the VPS at `/opt/eth-trading-agent/.env` as:
  `TEST_WALLET_ADDRESS=…` + `TEST_WALLET_CHAIN_ID=8453`
- `ETHERSCAN_API_KEY` already present on the VPS
- Private key stays in MetaMask only — never on the server
- `eth-agent` restarted after the env write

**Still blocked until Phase 1 code is deployed:** the VPS checkout does not
yet include `treasury.py` / `kalshi_gateway.py` / the Fund-surface fallback.
Setting the address alone does not change Telegram Fund until that ships.
See "Next" below.

## Next (in order)

### 0. Deploy Phase 1 code to the VPS — required before Fund / treasury / Kalshi Accepts work

Local branch still has uncommitted Phase 1 changes (`treasury.py`,
`kalshi_gateway.py`, `kalshi_execute.py`, pool/menu/watchdog/chain edits,
`deploy/PENDING_ITEMS.md`, etc.). Until those are committed, pushed, and
pulled via the usual `deploy/update.sh` path, the live bot will ignore
`TEST_WALLET_ADDRESS`.

After deploy, verify:

1. Telegram → Fund shows `0xab1b6…479d`
2. Admin `/treasury` lists the test wallet balance
3. A small Base USDC send from a *registered* `/wallet` credits that user

### 2. Kalshi API credentials — unlocks real Accepts on the Kalshi lanes

- Kalshi account (the account holder KYCs with Kalshi — that is their
  requirement for any trading account, no way around it), then Settings →
  API → create a key. Download the RSA private key PEM.
- Put the PEM on the server (e.g. `/opt/eth-trading-agent/secrets/kalshi.pem`,
  owner `ethagent`, mode 600) and set `KALSHI_API_KEY_ID` +
  `KALSHI_PRIVATE_KEY_PATH` in `.env`. `KALSHI_DB` must also be mounted
  (already is on the VPS).
- **Confirm the funding rail**: how the house Kalshi account gets funded from
  the test wallet (Kalshi supports USDC deposits and ACH/wire — check which
  is available to this account and its minimums). The treasury journal
  handles either; the Kalshi leg just confirms manually
  (`/transfer_confirm`) since arrivals there are not chain-visible to us.
- **Confirm the card token contract with the kalshi_15m_bot repo**: the hub
  resolves `kalshi:accept:<key>:<token>` by treating a numeric token as
  `paper_positions.id`, falling back to the bot's latest open entry within
  10 minutes. If the relay's token is something else, align one side or the
  other before going live.
- Effect: both Kalshi lanes flip from "feed only" to executable automatically
  (`strategy_catalog.is_executable`); no deploy needed once keys are set.
- After the first few live fills: check the booked fee against Kalshi's
  statement — the code uses the published taker formula
  (`ceil(0.07 × C × P × (1−P))`, rounds up) because fills don't itemize fees.

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

### 5. FalconX migration — pending, not scheduled

Tracked only. Venue code stays behind gateway shapes (`DerivGateway`,
`kalshi_gateway`) so FalconX arrives as a third gateway plus a treasury
location, not a rewrite.
