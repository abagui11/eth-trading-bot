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
