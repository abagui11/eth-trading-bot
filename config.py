"""Load environment variables and fail loudly if anything required is missing."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)


def overlay_dotenv_keys(env_path: Path, environ: dict[str, str], *keys: str) -> None:
    """Copy selected keys from a .env file over the process environment.

    systemd EnvironmentFile mangles unquoted PEM ``\\n`` sequences (it strips
    the backslash). python-dotenv will not override variables systemd already
    injected, so live JWT signing must re-read ``COINBASE_CDP_PRIVATE_KEY``
    from the file.
    """
    if not env_path.is_file():
        return
    file_vals = dotenv_values(env_path)
    for key in keys:
        raw = file_vals.get(key)
        if raw:
            environ[key] = raw


overlay_dotenv_keys(
    _ENV_PATH, os.environ,
    "COINBASE_CDP_PRIVATE_KEY", "COINBASE_TRANSFER_PRIVATE_KEY",
)

_REQUIRED_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "TELEGRAM_BOT_TOKEN",
    "MARKET_DATA_API",
    "PORTFOLIO_VALUE",
    "PAPER_PORTFOLIO_VALUE",
)


def _require(key: str) -> str:
    value = os.getenv(key)
    if value is None or value.strip() == "":
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Copy .env.example to .env and fill in all values."
        )
    return value.strip()


def _optional(key: str) -> str | None:
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _optional_int(key: str) -> int | None:
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value.strip())
    except ValueError:
        raise RuntimeError(f"{key} must be an integer, got {value!r}")


def _optional_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes")


ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL: str = _require("ANTHROPIC_MODEL")
# Cheap model for macro classify/pulse, display summary, and LLM critic.
ANTHROPIC_MODEL_FAST: str = (
    _optional("ANTHROPIC_MODEL_FAST") or "claude-haiku-4-5"
)
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
MARKET_DATA_API: str = _require("MARKET_DATA_API").rstrip("/")
PORTFOLIO_VALUE: float = float(_require("PORTFOLIO_VALUE"))
PAPER_PORTFOLIO_VALUE: float = float(_require("PAPER_PORTFOLIO_VALUE"))

# Set PAYWALL_ENABLED=true to restrict chat + hourly DMs to ALLOWED_TELEGRAM_IDS only.
PAYWALL_ENABLED: bool = _optional_bool("PAYWALL_ENABLED", default=False)

# Comma-separated Telegram user IDs (required when PAYWALL_ENABLED=true).
_allowed_raw = os.getenv("ALLOWED_TELEGRAM_IDS", "")
ALLOWED_TELEGRAM_IDS: list[int] = [
    int(x.strip()) for x in _allowed_raw.split(",") if x.strip()
]
if PAYWALL_ENABLED and not ALLOWED_TELEGRAM_IDS:
    raise RuntimeError(
        "PAYWALL_ENABLED=true requires ALLOWED_TELEGRAM_IDS in .env"
    )

# Optional legacy admin / monitoring channel.
TELEGRAM_CHAT_ID: str | None = _optional("TELEGRAM_CHAT_ID")
TELEGRAM_ADMIN_CHAT_ID: str | None = _optional("TELEGRAM_ADMIN_CHAT_ID")

# Audit / hallucination alerts (separate group or channel).
MONITOR_CHAT_ID: str | None = _optional("MONITOR_CHAT_ID")

ROOT_DIR: Path = Path(__file__).resolve().parent
CHARTS_DIR: Path = ROOT_DIR / "charts"
LEDGER_DB: Path = ROOT_DIR / "ledger.db"
OHLC_DB: Path = ROOT_DIR / "ohlc.db"
TRADING_GUIDE_DIR: Path = ROOT_DIR / "Trading Guide"

_DEFAULT_MACRO_FEEDS = ",".join(
    [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ]
)
_macro_feeds_raw = os.getenv("MACRO_FEED_URLS", _DEFAULT_MACRO_FEEDS)
MACRO_FEED_URLS: list[str] = [u.strip() for u in _macro_feeds_raw.split(",") if u.strip()]

_macro_extra_raw = os.getenv("MACRO_KEYWORD_EXTRA", "")
MACRO_KEYWORD_EXTRA: list[str] = [k.strip().lower() for k in _macro_extra_raw.split(",") if k.strip()]

MACRO_WEBHOOK_SECRET: str | None = _optional("MACRO_WEBHOOK_SECRET")

# Internal ops allowlist: Telegram IDs that receive gated HQ trade cards when
# bot_config.HQ_IDEAS_INTERNAL_ONLY is on. Falls back to ALLOWED_TELEGRAM_IDS,
# then the admin chat.
_internal_raw = os.getenv("INTERNAL_TELEGRAM_IDS", "")
INTERNAL_TELEGRAM_IDS: list[int] = [
    int(x.strip()) for x in _internal_raw.split(",") if x.strip()
]

# Bearer tokens for service consumers (yield_gen_bot, trade_ideas) hitting the
# authed /api/v1 endpoints. Comma-separated. MACRO_WEBHOOK_SECRET also works.
_service_tokens_raw = os.getenv("SERVICE_API_TOKENS", "")
SERVICE_API_TOKENS: list[str] = [
    t.strip() for t in _service_tokens_raw.split(",") if t.strip()
]

# Twitter/X announcement posting (pay-per-use API v2, OAuth 1.0a user context).
# All optional: posting silently no-ops until TWITTER_ENABLED=true and all
# four keys are present.
TWITTER_ENABLED: bool = _optional_bool("TWITTER_ENABLED", default=False)
TWITTER_API_KEY: str | None = _optional("TWITTER_API_KEY")
TWITTER_API_SECRET: str | None = _optional("TWITTER_API_SECRET")
TWITTER_ACCESS_TOKEN: str | None = _optional("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET: str | None = _optional("TWITTER_ACCESS_TOKEN_SECRET")
# OAuth 2.0 Client ID/Secret (User authentication settings). Stored for
# future use; current poster uses OAuth 1.0a keys above.
TWITTER_CLIENT_ID: str | None = _optional("TWITTER_CLIENT_ID")
TWITTER_CLIENT_SECRET: str | None = _optional("TWITTER_CLIENT_SECRET")

# Public dashboard URL shown in Telegram (Portfolio button / welcome copy).
DASHBOARD_PUBLIC_URL: str | None = _optional("DASHBOARD_PUBLIC_URL")
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8080") or "8080")

# --- MoonPay Commerce (hel.io) deposits — USDC on Base ---------------------
# Public + secret API keys from moonpay.hel.io Settings. Deposit id is the
# Helio Deposit product that provisions per-user addresses. Recipient is the
# merchant EVM wallet Helio sweeps into. Webhook shared token verifies HMAC.
MOONPAY_PUBLIC_KEY: str | None = _optional("MOONPAY_PUBLIC_KEY")
MOONPAY_SECRET_KEY: str | None = _optional("MOONPAY_SECRET_KEY")
MOONPAY_DEPOSIT_ID: str | None = _optional("MOONPAY_DEPOSIT_ID")
MOONPAY_RECIPIENT_PUBLIC_KEY: str | None = _optional("MOONPAY_RECIPIENT_PUBLIC_KEY")
MOONPAY_WEBHOOK_SECRET: str | None = _optional("MOONPAY_WEBHOOK_SECRET")
# Production api.hel.io; set MOONPAY_API_BASE=https://api.dev.hel.io for devnet.
MOONPAY_API_BASE: str = (
    _optional("MOONPAY_API_BASE") or "https://api.hel.io"
).rstrip("/")
# Optional hosted deposit / on-ramp URL template. {deposit_id}, {customer_token},
# {customer_id} are substituted when present.
MOONPAY_WIDGET_URL_TEMPLATE: str | None = _optional("MOONPAY_WIDGET_URL_TEMPLATE")

# --- Phase 1 test wallet — shared deposit/routing wallet, pre-MoonPay-approval.
# One operator-controlled EOA that (a) receives user USDC deposits directly,
# attributed by sender address, and (b) funds the venues (Coinbase / Kalshi)
# through operator-approved treasury transfers.
TEST_WALLET_ADDRESS: str | None = _optional("TEST_WALLET_ADDRESS")
# Optional hot-wallet signer for that EOA. Unset = the bot only *reads* the
# address and every treasury leg is sent by hand. Set = an admin can tap
# "Send" on a journaled test_wallet → venue leg and the bot signs the USDC
# transfer itself. Guardrails live in signer.py / treasury.execute_transfer:
# the key must derive to TEST_WALLET_ADDRESS, the destination must be one of
# the two venue deposit addresses below (nothing else is ever a valid `to`),
# and both caps must clear. Keep this key funded only with tester capital.
TEST_WALLET_PRIVATE_KEY: str | None = _optional("TEST_WALLET_PRIVATE_KEY")
# Numeric chain each venue deposit address lives on (8453 Base, 1 Ethereum).
# No default: USDC sent on the wrong chain to an exchange address is gone.
POOL_DEPOSIT_CHAIN_ID: int | None = _optional_int("POOL_DEPOSIT_CHAIN_ID")
KALSHI_DEPOSIT_ADDRESS: str | None = _optional("KALSHI_DEPOSIT_ADDRESS")
KALSHI_DEPOSIT_CHAIN_ID: int | None = _optional_int("KALSHI_DEPOSIT_CHAIN_ID")
# Signer caps: per leg, and rolling 24h across every signer-sent leg.
TREASURY_SEND_MAX_USD: float = float(_optional("TREASURY_SEND_MAX_USD") or "2000")
TREASURY_SEND_DAILY_MAX_USD: float = float(
    _optional("TREASURY_SEND_DAILY_MAX_USD") or "5000"
)
# Send deploy legs the moment they are journaled, no admin tap. Only
# test_wallet → venue legs created by a user's deploy; the same allowlist and
# caps apply, and anything the signer refuses falls back to the Send card.
# Off = every leg waits for an admin tap.
TREASURY_AUTO_SEND_DEPLOYS: bool = _optional_bool("TREASURY_AUTO_SEND_DEPLOYS", True)
# When a venue send is short of native gas, swap USDC → ETH via Uniswap on
# that chain (from the test wallet's USDC float) up to these bounds, then
# retry. Needs a dust of ETH already present to pay for the approve+swap
# itself — a completely empty wallet still needs a one-time bootstrap.
GAS_TOPUP_ENABLED: bool = _optional_bool("GAS_TOPUP_ENABLED", True)
GAS_TOPUP_TARGET_ETH: float = float(_optional("GAS_TOPUP_TARGET_ETH") or "0.015")
GAS_TOPUP_MAX_USD: float = float(_optional("GAS_TOPUP_MAX_USD") or "25")
# Assumed ETH ceiling for minOut floor (slippage safety, not a price feed).
GAS_TOPUP_ETH_PRICE_CEILING_USD: float = float(
    _optional("GAS_TOPUP_ETH_PRICE_CEILING_USD") or "6000"
)
# Optional extra USDC kept out of deployable (on top of undeployed claims).
# Default 0 — gas top-ups already refuse to spend into undeployed+leg reserves.
TREASURY_GAS_RESERVE_USD: float = float(
    _optional("TREASURY_GAS_RESERVE_USD") or "0"
)
# Primary chain for the test wallet. 8453 = Base (default), 1 = Ethereum mainnet.
TEST_WALLET_CHAIN_ID: int = int(_optional("TEST_WALLET_CHAIN_ID") or "8453")
# Every chain deposits are watched on. An EOA is the same address on every
# EVM chain, so USDC sent on Base or Ethereum lands in the same wallet; the
# sweep and the treasury balance read cover each listed chain. The primary is
# always included.
_chain_ids = [
    int(x) for x in (_optional("TEST_WALLET_CHAIN_IDS") or "8453,1").split(",")
    if x.strip()
]
TEST_WALLET_CHAIN_IDS: tuple[int, ...] = tuple(
    dict.fromkeys([TEST_WALLET_CHAIN_ID, *_chain_ids])
)
# JSON-RPC endpoints per chain, read-only. Used wherever Etherscan does not
# cover a chain — its free plan stopped serving Base in late Sept 2026 — so
# the deposit watcher keeps working without a paid indexer. Public endpoints
# cap eth_getLogs ranges (Base's is 500 blocks); the scan chunks to fit.
BASE_RPC_URL: str | None = _optional("BASE_RPC_URL") or "https://mainnet.base.org"
ETH_RPC_URL: str | None = _optional("ETH_RPC_URL") or "https://ethereum-rpc.publicnode.com"
RPC_LOG_RANGE: int = int(_optional("RPC_LOG_RANGE") or "500")
# Card on-ramp widget for the test wallet (any provider that can pin the
# destination address and echo an external customer id back on its webhook).
# Unset until a provider account exists — the Fund surface then offers the
# direct USDC transfer path only. {telegram_id} is substituted when present.
ONRAMP_WIDGET_URL_TEMPLATE: str | None = _optional("ONRAMP_WIDGET_URL_TEMPLATE")

# --- Kalshi execution (hub-side gateway for the two Kalshi lanes) -----------
# API key id + RSA private key from the Kalshi account settings page. Unset
# means the Kalshi lanes stay feed-only, exactly as before.
KALSHI_API_KEY_ID: str | None = _optional("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PATH: str | None = _optional("KALSHI_PRIVATE_KEY_PATH")
KALSHI_API_BASE: str = (
    _optional("KALSHI_API_BASE") or "https://api.elections.kalshi.com/trade-api/v2"
).rstrip("/")

EVA_WEBSITE_URL: str = _optional("EVA_WEBSITE_URL") or "https://eva.finance/"
EVA_SUPPORT_EMAIL: str = _optional("EVA_SUPPORT_EMAIL") or "info@republictech.io"

# Yield generation dashboard (yield_gen_bot Next.js app). API base is used by
# the hub's Yield Generation tab; the dashboard URL is the outbound link.
YIELD_GEN_API_URL: str | None = _optional("YIELD_GEN_API_URL")
YIELD_GEN_DASHBOARD_URL: str | None = (
    _optional("YIELD_GEN_DASHBOARD_URL") or _optional("YIELD_GEN_API_URL")
)

# --- Live execution (Coinbase US futures via Advanced Trade REST) ------------
# off    = paper only (default, safe)
# shadow = log the exact live order we WOULD send, place nothing
# live   = place real orders (requires CDP key below)
EXECUTION_MODE: str = (_optional("EXECUTION_MODE") or "off").lower()
if EXECUTION_MODE not in ("off", "shadow", "live"):
    raise RuntimeError(
        f"EXECUTION_MODE must be off|shadow|live, got {EXECUTION_MODE!r}"
    )
# CDP API key: View + Trade permissions ONLY — never Transfer.
COINBASE_CDP_API_KEY_NAME: str | None = _optional("COINBASE_CDP_API_KEY_NAME")
COINBASE_CDP_PRIVATE_KEY: str | None = _optional("COINBASE_CDP_PRIVATE_KEY")

# A SECOND key, holding transfer rights and nothing else. Kept apart from the
# trading key on purpose: the trading key can trade but not withdraw, this one
# can withdraw but not trade, so neither credential alone can both lose money
# in the market and move it off the venue. Only the payout path reads these.
COINBASE_TRANSFER_KEY_NAME: str | None = _optional("COINBASE_TRANSFER_KEY_NAME")
COINBASE_TRANSFER_PRIVATE_KEY: str | None = _optional("COINBASE_TRANSFER_PRIVATE_KEY")
# Unused since the US-futures rework (kept so old .env files still load).
COINBASE_DERIV_API_URL: str | None = _optional("COINBASE_DERIV_API_URL")

# Critical live-execution alerts (halt / failed stop) also go out by email.
# Same Resend account the yield_gen_bot uses; silently skipped when unset.
RESEND_API_KEY: str | None = _optional("RESEND_API_KEY")
ALERT_EMAIL_TO: str | None = _optional("ALERT_EMAIL_TO")
ALERT_EMAIL_FROM: str = _optional("ALERT_EMAIL_FROM") or "alerts@resend.dev"

# Beta signups from the eva.finance marketing site (POST /api/public/beta).
# The signup row in ledger.db is the source of truth; this is only who gets
# the notification email. Requires RESEND_API_KEY and a verified-domain
# ALERT_EMAIL_FROM to deliver to external addresses — the alerts@resend.dev
# default only delivers to the Resend account owner.
BETA_SIGNUP_EMAIL_TO: str = (
    _optional("BETA_SIGNUP_EMAIL_TO")
    or "abagui@republictech.io,daniel@republictech.io"
)

# Private investor view (/investors). When set, the page and its API require
# ?k=<token> and then ride a cookie; anything else 404s so the URL gives away
# nothing about what is behind it. Unset leaves the page unlisted-only, the
# same posture as /volume.
INVESTOR_ACCESS_TOKEN: str | None = _optional("INVESTOR_ACCESS_TOKEN")
INVESTOR_SESSION_TTL_SEC: int = int(
    os.getenv("INVESTOR_SESSION_TTL_SEC", "2592000") or "2592000"
)

# Password for the hub's Investor Analytics tab (equity curves, edge table,
# scaling). Shared with investors by hand; env-overridable for rotation.
ANALYTICS_PASSWORD: str = _optional("ANALYTICS_PASSWORD") or "evatradesforyou"

# --- Tester pool (hybrid Telegram UX) ----------------------------------------
# Private forum supergroup that carries the Trades and Research topics. When
# set, HQ/mill trade cards and research pushes post ONCE into the topic instead
# of DM-per-subscriber; Account traffic (portfolio, deposits, personal fill
# notices) stays in DMs. Unset = today's DM broadcast, so dev boxes without a
# forum keep working.

# Telegram ids allowed to Admit users and credit deposits. Env rather than
# code so an operator can be added without a deploy; merged with
# bot_config.POOL_ADMIN_TELEGRAM_IDS. Without at least one of these the Admit
# cards have nowhere to go, so pool.admin_ids() falls back to
# INTERNAL_TELEGRAM_IDS and then the admin chat.
_pool_admin_raw = os.getenv("POOL_ADMIN_TELEGRAM_IDS", "")
POOL_ADMIN_TELEGRAM_IDS: list[int] = [
    int(x.strip()) for x in _pool_admin_raw.split(",") if x.strip()
]

# Network the deposit address expects, e.g. "Ethereum mainnet", "Base",
# "Arbitrum One". No default on purpose: USDC sent on the wrong chain to an
# address you do not control there is gone, so the instructions say "confirm
# the network with the admin" rather than name a guess.
POOL_DEPOSIT_CHAIN: str | None = _optional("POOL_DEPOSIT_CHAIN")

POOL_FORUM_CHAT_ID: int | None = _optional_int("POOL_FORUM_CHAT_ID")
POOL_FORUM_TRADES_THREAD_ID: int | None = _optional_int("POOL_FORUM_TRADES_THREAD_ID")
POOL_FORUM_RESEARCH_THREAD_ID: int | None = _optional_int(
    "POOL_FORUM_RESEARCH_THREAD_ID"
)
# Where testers send funds (USDC address or short ops instruction). Shown in
# the /deposit flow; the admin still credits manually once it lands.
POOL_DEPOSIT_ADDRESS: str | None = _optional("POOL_DEPOSIT_ADDRESS")

# Read-only chain access, for the two things Coinbase will not report: who
# sent a deposit, and whether a payout landed. Unset means wallets can never
# reach `verified`, which leaves withdrawals refused — safe, but stuck.
ETHERSCAN_API_KEY: str | None = _optional("ETHERSCAN_API_KEY")

# HMAC secret for /me magic links (falls back to bot token if unset).
ME_TOKEN_SECRET: str = _optional("ME_TOKEN_SECRET") or TELEGRAM_BOT_TOKEN
ME_TOKEN_TTL_SEC: int = int(os.getenv("ME_TOKEN_TTL_SEC", "3600") or "3600")
ME_SESSION_TTL_SEC: int = int(os.getenv("ME_SESSION_TTL_SEC", "86400") or "86400")
