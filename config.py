"""Load environment variables and fail loudly if anything required is missing."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)

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

# Yield generation dashboard (yield_gen_bot Next.js app). API base is used by
# the hub's Yield Generation tab; the dashboard URL is the outbound link.
YIELD_GEN_API_URL: str | None = _optional("YIELD_GEN_API_URL")
YIELD_GEN_DASHBOARD_URL: str | None = (
    _optional("YIELD_GEN_DASHBOARD_URL") or _optional("YIELD_GEN_API_URL")
)

# --- Live execution (Coinbase Deribit-powered derivatives gateway) -----------
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
# New Deribit-powered gateway (INTX retires 2026-09-09 — do not point at it).
COINBASE_DERIV_API_URL: str | None = _optional("COINBASE_DERIV_API_URL")

# Critical live-execution alerts (halt / failed stop) also go out by email.
# Same Resend account the yield_gen_bot uses; silently skipped when unset.
RESEND_API_KEY: str | None = _optional("RESEND_API_KEY")
ALERT_EMAIL_TO: str | None = _optional("ALERT_EMAIL_TO")
ALERT_EMAIL_FROM: str = _optional("ALERT_EMAIL_FROM") or "alerts@resend.dev"

# HMAC secret for /me magic links (falls back to bot token if unset).
ME_TOKEN_SECRET: str = _optional("ME_TOKEN_SECRET") or TELEGRAM_BOT_TOKEN
ME_TOKEN_TTL_SEC: int = int(os.getenv("ME_TOKEN_TTL_SEC", "3600") or "3600")
ME_SESSION_TTL_SEC: int = int(os.getenv("ME_SESSION_TTL_SEC", "86400") or "86400")
