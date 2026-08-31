"""Coinbase futures account snapshot + margin health for the investor view.

The trading path sizes off the hardcoded ``LIVE_*_SLEEVE`` constants and never
reads the real balance, so equity was only ever visible by running
``deploy/diagnose_live.py`` by hand. Investors need the actual number, so this
reads ``cfm/balance_summary`` directly and degrades to the configured sleeves
when credentials are absent (EXECUTION_MODE=off, local dev) rather than
failing the page.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import bot_config

logger = logging.getLogger(__name__)

_TTL_SEC = 60.0
_cache: tuple[dict[str, Any], float] = ({}, 0.0)

# Configured sleeve capital, used as the denominator when the exchange read is
# unavailable. This is what the bot *thinks* it is trading, not what is really
# on deposit — the payload flags which one the reader is looking at.
CONFIGURED_CAPITAL_USD = float(
    bot_config.LIVE_HQ_EQUITY_USD + bot_config.LIVE_MILL_SLEEVE_USD
)


def reset_cache() -> None:
    """Drop the memoized balance so the next read refetches."""
    global _cache
    _cache = ({}, 0.0)


def _raw_val(raw: dict[str, Any], key: str) -> float | None:
    """Coinbase wraps every money field as {"value": "1.23", "currency": "USD"}."""
    node = raw.get(key)
    if isinstance(node, dict):
        value = node.get("value")
    else:
        value = node
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fallback_snapshot(error: str | None) -> dict[str, Any]:
    return {
        "available": False,
        "source": "config",
        "equity_usd": CONFIGURED_CAPITAL_USD,
        "available_funds_usd": None,
        "margin_balance_usd": None,
        "exchange_unrealized_pnl_usd": None,
        "initial_margin_usd": None,
        "liquidation_threshold_usd": None,
        "liquidation_buffer_usd": None,
        "liquidation_buffer_pct": None,
        "daily_realized_pnl_usd": None,
        "fetched_at": None,
        "error": error,
    }


def get_account_snapshot(*, force: bool = False) -> dict[str, Any]:
    """Live futures balance summary, memoized for 60s.

    Blocking HTTP — call from a sync route handler so FastAPI runs it in a
    threadpool instead of stalling the event loop.
    """
    global _cache
    now = time.time()
    if not force and _cache[0] and now - _cache[1] < _TTL_SEC:
        return dict(_cache[0])

    try:
        from coinbase_deriv import get_gateway

        summary = get_gateway().get_account_summary()
    except Exception as exc:  # gateway missing, no CDP key, transport error
        logger.warning("Investor account snapshot unavailable: %s", exc)
        snapshot = _fallback_snapshot(str(exc)[:200])
        _cache = (snapshot, now)
        return dict(snapshot)

    raw = summary.get("raw") or {}
    snapshot = {
        "available": True,
        "source": "exchange",
        "equity_usd": float(summary.get("equity") or 0.0),
        "available_funds_usd": float(summary.get("available_funds") or 0.0),
        "margin_balance_usd": float(summary.get("margin_balance") or 0.0),
        "exchange_unrealized_pnl_usd": float(summary.get("unrealized_pnl") or 0.0),
        "initial_margin_usd": _raw_val(raw, "initial_margin"),
        "liquidation_threshold_usd": _raw_val(raw, "liquidation_threshold"),
        "liquidation_buffer_usd": _raw_val(raw, "liquidation_buffer_amount"),
        "liquidation_buffer_pct": _raw_val(raw, "liquidation_buffer_percentage"),
        "daily_realized_pnl_usd": _raw_val(raw, "daily_realized_pnl"),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "error": None,
    }
    _cache = (snapshot, now)
    return dict(snapshot)


def health_band(health_pct: float | None) -> str:
    """Badge class for a collateral-to-exposure ratio.

    Bands are set from the ratio's inverse: 40% is 2.5x geared, 20% is 5x.
    Below 20% a routine adverse move starts threatening the whole account.
    """
    if health_pct is None:
        return "none"
    if health_pct >= 40:
        return "good"
    if health_pct >= 20:
        return "warn"
    return "bad"


def build_health(
    *,
    equity_usd: float | None,
    gross_notional_usd: float,
    account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collateral health for the open live book.

    ``health_pct`` is equity over gross notional — the share of the position
    that is actually covered by cash. $1.4k backing $4.2k of exposure is 3x
    geared and reads 33%. ``leverage_x`` is the same fact the other way up.
    """
    equity = float(equity_usd or 0.0)
    notional = float(gross_notional_usd or 0.0)
    has_exposure = notional > 0

    health_pct = (equity / notional * 100.0) if (has_exposure and equity > 0) else None
    leverage_x = (notional / equity) if equity > 0 else None

    return {
        "equity_usd": round(equity, 2),
        "gross_notional_usd": round(notional, 2),
        "health_pct": round(health_pct, 1) if health_pct is not None else None,
        "leverage_x": round(leverage_x, 2) if leverage_x is not None else None,
        "band": health_band(health_pct) if has_exposure else "none",
        "has_exposure": has_exposure,
        # Coinbase's own distance-to-liquidation, when the exchange read
        # worked. Independent of our ratio and the one that actually governs
        # whether the account gets closed out.
        "liquidation_buffer_pct": (account or {}).get("liquidation_buffer_pct"),
        "liquidation_buffer_usd": (account or {}).get("liquidation_buffer_usd"),
        "initial_margin_usd": (account or {}).get("initial_margin_usd"),
        "equity_source": (account or {}).get("source", "config"),
    }
