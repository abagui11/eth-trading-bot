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

# Public dashboard URL shown in Telegram (Portfolio button / welcome copy).
DASHBOARD_PUBLIC_URL: str | None = _optional("DASHBOARD_PUBLIC_URL")
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8080") or "8080")

# HMAC secret for /me magic links (falls back to bot token if unset).
ME_TOKEN_SECRET: str = _optional("ME_TOKEN_SECRET") or TELEGRAM_BOT_TOKEN
ME_TOKEN_TTL_SEC: int = int(os.getenv("ME_TOKEN_TTL_SEC", "3600") or "3600")
ME_SESSION_TTL_SEC: int = int(os.getenv("ME_SESSION_TTL_SEC", "86400") or "86400")
