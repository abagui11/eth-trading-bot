"""Display helpers for dashboard Jinja templates."""

from __future__ import annotations

from datetime import datetime, timezone

# Hard-coded glossary for setup tags / badges shown in the journal.
TAG_GLOSSARY: dict[str, str] = {
    # Side / status / close reasons
    "long": "Long position — profit if ETH rises.",
    "short": "Short position — profit if ETH falls.",
    "LIVE": "Open paper position still being managed.",
    "stop_loss": "Closed because price hit the stop-loss.",
    "take_profit": "Closed because price hit a take-profit level.",
    "signal_net": "Closed or reduced when a newer signal flipped net exposure.",
    "restore_force": "Closed to make room when restoring another position.",
    "fifo_max_positions": "Closed oldest position after hitting the open-trade cap.",
    # Actions
    "spot_buy": "Spot long suggestion (buy ETH).",
    "spot_sell": "Spot short suggestion (sell / short ETH).",
    "deriv_buy": "Derivatives long suggestion.",
    "deriv_sell": "Derivatives short suggestion.",
    "no_trade": "Cycle concluded with no actionable trade.",
    # Market context / setup tags
    "ranging": "Price is chopping inside the 24h high–low range (no clean break).",
    "range_24h_new": "A new 24h range window was just established.",
    "range_24h_break_above": "Price broke above the prior 24h range high.",
    "range_24h_break_below": "Price broke below the prior 24h range low.",
    "range_high_expanded": "The 24h range high extended higher this cycle.",
    "h4_sfp_bullish": "H4 bullish swing-failure pattern — sweep of lows that failed and reversed up.",
    "h4_sfp_bearish": "H4 bearish swing-failure pattern — sweep of highs that failed and reversed down.",
    "m5_sfp_bullish": "M5 bullish swing-failure — short-term low sweep that reversed up.",
    "m5_sfp_bearish": "M5 bearish swing-failure — short-term high sweep that reversed down.",
    "m5_ob_bullish_in_fib": "Price is inside a bullish M5 order block’s 0.25–0.50 fib entry band.",
    "m5_ob_bearish_in_fib": "Price is inside a bearish M5 order block’s 0.25–0.50 fib entry band.",
    "m5_ob_bullish_no_fib": "Bullish M5 order block nearby, but price is not in the fib entry band yet.",
    "m5_ob_bearish_no_fib": "Bearish M5 order block nearby, but price is not in the fib entry band yet.",
    "htf_zone_conflict": "Short-term signal conflicts with the H4 zone bias.",
    "retest_already_tagged": "This bearish-retest setup was already tagged earlier — avoid duplicates.",
    "short_trigger_retest": "Bearish retest trigger armed on a marked H4/M5 structure.",
    "h4_ob": "Higher-timeframe (H4) order-block context in play.",
    "h1_sfp": "Legacy tag: SFP noted on the old H1 stack (now usually M5/H4).",
    "range_24h": "Trade idea references the 24h range envelope.",
    "bearish_ob": "Bearish order-block context for a short idea.",
    "bullish_ob": "Bullish order-block context for a long idea.",
}


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_trade_time(value: str | None) -> str:
    """Short clock time, e.g. ``4:02 PM`` (UTC)."""
    dt = parse_ts(value)
    if dt is None:
        return "—"
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {ampm}"


def format_trade_date(value: str | None) -> str:
    """Short calendar date, e.g. ``Jul 14`` (UTC)."""
    dt = parse_ts(value)
    if dt is None:
        return "—"
    return f"{dt.strftime('%b')} {dt.day}"


def trade_title(opened_at: str | None, side: str | None) -> str:
    """Summary heading: ``Jul 14 [short]``."""
    date = format_trade_date(opened_at)
    trade_type = (side or "trade").strip().lower() or "trade"
    return f"{date} [{trade_type}]"


def tag_tooltip(tag: str | None) -> str:
    if not tag:
        return ""
    key = str(tag).strip()
    if key in TAG_GLOSSARY:
        return TAG_GLOSSARY[key]
    # Soft fallbacks for dynamic tags.
    low = key.lower()
    if low.startswith("h4_sfp_"):
        direction = low.split("_")[-1]
        return f"H4 {direction} swing-failure pattern (liquidity sweep that reversed)."
    if low.startswith("m5_sfp_"):
        direction = low.split("_")[-1]
        return f"M5 {direction} swing-failure pattern (short-term liquidity sweep)."
    if low.startswith("m5_ob_") and low.endswith("_in_fib"):
        return "M5 order block with price inside the 0.25–0.50 fib entry band."
    if low.startswith("m5_ob_") and low.endswith("_no_fib"):
        return "M5 order block nearby, waiting for the fib entry band."
    return key.replace("_", " ")
