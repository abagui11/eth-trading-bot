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

### 5. FalconX migration — pending, not scheduled

Tracked only. Venue code stays behind gateway shapes (`DerivGateway`,
`kalshi_gateway`) so FalconX arrives as a third gateway plus a treasury
location, not a rewrite.
