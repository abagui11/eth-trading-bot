"""Display helpers for dashboard Jinja templates."""

from __future__ import annotations

from datetime import datetime, timezone

# Hard-coded glossary for setup tags / badges shown in the journal.
# Wording follows Trading Guide/Trading Guide.md (24h range, SFP, OB/fib stack).
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
    # Market context / setup tags (Trading Guide: 24h range + SFP + OB fib)
    "ranging": (
        "Ranging: price is oscillating inside the 24h high–low without a clean "
        "trend or range break (Trading Guide — identify ranging conditions)."
    ),
    "range_24h": "Trade idea references the 24h high–low range envelope.",
    "range_24h_ranging": (
        "Ranging inside the 24h range — chop between the session high and low."
    ),
    "range_24h_new": "A new 24h range window was just established.",
    "range_24h_break_above": "Price broke above the prior 24h range high.",
    "range_24h_break_below": "Price broke below the prior 24h range low.",
    "range_high_expanded": "The 24h range high extended higher this cycle.",
    "h4_sfp_bullish": (
        "H4 bullish swing-failure (SFP): liquidity swept below a swing low, then "
        "reversed up — HTF reversal signal (Trading Guide SFP)."
    ),
    "h4_sfp_bearish": (
        "H4 bearish swing-failure (SFP): liquidity swept above a swing high, then "
        "reversed down — HTF reversal signal (Trading Guide SFP)."
    ),
    "m5_sfp_bullish": (
        "M5 bullish SFP: short-term low sweep that failed and reversed up — "
        "common LTF entry/confirm trigger."
    ),
    "m5_sfp_bearish": (
        "M5 bearish SFP: short-term high sweep that failed and reversed down — "
        "common LTF entry/confirm trigger."
    ),
    "m5_ob_bullish_in_fib": (
        "Bullish M5 order block with price in the 0.25–0.50 fib entry band "
        "(staged watchdog/paper long zone)."
    ),
    "m5_ob_bearish_in_fib": (
        "Bearish M5 order block with price in the 0.25–0.50 fib entry band "
        "(staged watchdog/paper short zone)."
    ),
    "m5_ob_bullish_no_fib": (
        "Bullish M5 order block nearby, but price is outside the 0.25–0.50 fib "
        "entry band — wait for retest (Trading Guide)."
    ),
    "m5_ob_bearish_no_fib": (
        "Bearish M5 order block nearby, but price is outside the 0.25–0.50 fib "
        "entry band — wait for retest (Trading Guide)."
    ),
    "htf_zone_conflict": (
        "M5 trigger conflicts with H4 zone bias — HTF is context only; entries "
        "can still fire on M5 OB/SFP."
    ),
    "retest_already_tagged": (
        "This bearish-retest setup was already tagged earlier — avoid duplicate alerts."
    ),
    "short_trigger_retest": "Bearish retest trigger armed on marked H4/M5 structure.",
    "h4_ob": (
        "H4 order-block context (HTF bias only). Green = bullish OB, pink = bearish OB; "
        "entries still need an M5 OB/SFP fib trigger."
    ),
    "h12_ob": "Legacy/higher-TF order-block context (H12 research stack).",
    "h1_sfp": "Legacy tag: SFP on the old H1 stack (live stack is H4 / M5).",
    "h1_sfp_bullish": "Legacy H1 bullish SFP tag (live stack uses H4 / M5 SFPs).",
    "h1_sfp_bearish": "Legacy H1 bearish SFP tag (live stack uses H4 / M5 SFPs).",
    "bearish_ob": "Bearish order-block context for a short idea.",
    "bullish_ob": "Bullish order-block context for a long idea.",
    "macro_gate_long": (
        "Macro soft-gate: high-severity headlines lean against new longs "
        "(chart structure still primary)."
    ),
    "macro_gate_short": (
        "Macro soft-gate: high-severity headlines lean against new shorts "
        "(chart structure still primary)."
    ),
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
    # Case-insensitive glossary hit (e.g. Live vs LIVE).
    low = key.lower()
    for glossary_key, tip in TAG_GLOSSARY.items():
        if glossary_key.lower() == low:
            return tip
    # Soft fallbacks for dynamic tags.
    if low.startswith("h4_sfp_"):
        direction = low.rsplit("_", 1)[-1]
        return (
            f"H4 {direction} swing-failure (SFP): liquidity sweep of a swing that "
            "failed and reversed (Trading Guide SFP)."
        )
    if low.startswith("m5_sfp_"):
        direction = low.rsplit("_", 1)[-1]
        return (
            f"M5 {direction} swing-failure (SFP): short-term liquidity sweep that "
            "reversed — LTF entry/confirm trigger."
        )
    if low.startswith("h1_sfp_"):
        direction = low.rsplit("_", 1)[-1]
        return f"Legacy H1 {direction} SFP tag (live stack uses H4 / M5 SFPs)."
    if low.startswith("m5_ob_") and low.endswith("_in_fib"):
        return (
            "M5 order block with price inside the 0.25–0.50 fib entry band "
            "(Trading Guide staged entries)."
        )
    if low.startswith("m5_ob_") and low.endswith("_no_fib"):
        return (
            "M5 order block nearby, but price is outside the 0.25–0.50 fib entry "
            "band — wait for retest."
        )
    if low.startswith("macro_gate_"):
        return (
            "Macro soft-gate from high-severity headlines — advisory only; "
            "chart structure remains primary."
        )
    # Never leave a blank/placeholder tip — spell the tag out.
    return f"Setup tag: {key.replace('_', ' ')}."
