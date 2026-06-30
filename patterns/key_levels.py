"""SpacemanBTC-style calendar key levels from daily OHLC (UTC / Coinbase)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

# Pine default colors (SpacemanBTC IDWM)
COLOR_DAILY = "#08bcd4"
COLOR_MONDAY = "#ffffff"
COLOR_WEEKLY = "#D4AF37"
COLOR_MONTHLY = "#08d48c"
COLOR_QUARTERLY = "#ff0000"
COLOR_YEARLY = "#ff0000"


@dataclass
class KeyLevel:
    price: float
    label: str
    color: str


def _bars_to_df(daily_bars: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(daily_bars)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").sort_index().astype(
        {"open": float, "high": float, "low": float, "close": float}
    )


def _quarter_start(ts: pd.Timestamp) -> pd.Timestamp:
    month = ((ts.month - 1) // 3) * 3 + 1
    return pd.Timestamp(year=ts.year, month=month, day=1, tz="UTC")


def merge_levels(levels: list[KeyLevel], tolerance_pct: float = 0.0001) -> list[KeyLevel]:
    """Merge levels at the same price (Pine merge logic)."""
    if not levels:
        return []
    sorted_levels = sorted(levels, key=lambda lv: lv.price)
    merged: list[KeyLevel] = []
    for lv in sorted_levels:
        if not merged:
            merged.append(KeyLevel(lv.price, lv.label, lv.color))
            continue
        prev = merged[-1]
        tol = max(abs(prev.price) * tolerance_pct, 0.01)
        if abs(lv.price - prev.price) <= tol:
            merged[-1] = KeyLevel(
                prev.price,
                f"{lv.label} / {prev.label}",
                lv.color,
            )
        else:
            merged.append(KeyLevel(lv.price, lv.label, lv.color))
    return merged


def compute_key_levels(
    daily_bars: list[dict],
    now: datetime | None = None,
) -> list[KeyLevel]:
    """
    Compute SpacemanBTC Pine-default key levels from UTC daily candles.

    Enabled: daily open; Monday H/L/Mid; weekly open + prev week H/L/Mid;
    monthly open + prev month H/L/Mid; quarterly open + prev quarter mid;
    yearly open + current year mid.
    """
    df = _bars_to_df(daily_bars)
    if df.empty:
        return []

    now_ts = pd.Timestamp(now or datetime.now(timezone.utc)).tz_convert("UTC")
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")

    # Use bars up to "now" for current-period calculations.
    df = df[df.index <= now_ts.normalize() + pd.Timedelta("1D")]
    if df.empty:
        return []

    levels: list[KeyLevel] = []

    # --- Daily open (current UTC day) ---
    today = now_ts.normalize()
    today_bars = df[df.index >= today]
    if not today_bars.empty:
        levels.append(KeyLevel(float(today_bars.iloc[0]["open"]), "Daily Open", COLOR_DAILY))
    elif len(df) > 0:
        levels.append(KeyLevel(float(df.iloc[-1]["open"]), "Daily Open", COLOR_DAILY))

    # --- Previous day H/L/Mid (off in Pine defaults; skip) ---

    # --- Monday range (current ISO week Monday daily bar) ---
    week_start = today - pd.Timedelta(f"{int(today.weekday())}D")
    monday_bars = df[(df.index >= week_start) & (df.index < week_start + pd.Timedelta("1D"))]
    if not monday_bars.empty:
        m = monday_bars.iloc[0]
        m_high, m_low = float(m["high"]), float(m["low"])
        levels.append(KeyLevel(m_high, "Monday High", COLOR_MONDAY))
        levels.append(KeyLevel(m_low, "Monday Low", COLOR_MONDAY))
        levels.append(KeyLevel((m_high + m_low) / 2, "Monday Mid", COLOR_MONDAY))

    # --- Weekly (Monday-start) ---
    weekly = df.resample("W-MON", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()

    if len(weekly) >= 1:
        cur_w = weekly.iloc[-1]
        levels.append(KeyLevel(float(cur_w["open"]), "Weekly Open", COLOR_WEEKLY))
    if len(weekly) >= 2:
        prev_w = weekly.iloc[-2]
        pwh, pwl = float(prev_w["high"]), float(prev_w["low"])
        levels.append(KeyLevel(pwh, "Prev Week High", COLOR_WEEKLY))
        levels.append(KeyLevel(pwl, "Prev Week Low", COLOR_WEEKLY))
        levels.append(KeyLevel((pwh + pwl) / 2, "Prev Week Mid", COLOR_WEEKLY))

    # --- Monthly ---
    monthly = df.resample("MS").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()

    if len(monthly) >= 1:
        cur_m = monthly.iloc[-1]
        levels.append(KeyLevel(float(cur_m["open"]), "Monthly Open", COLOR_MONTHLY))
    if len(monthly) >= 2:
        prev_m = monthly.iloc[-2]
        pmh, pml = float(prev_m["high"]), float(prev_m["low"])
        levels.append(KeyLevel(pmh, "Prev Month High", COLOR_MONTHLY))
        levels.append(KeyLevel(pml, "Prev Month Low", COLOR_MONTHLY))
        levels.append(KeyLevel((pmh + pml) / 2, "Prev Month Mid", COLOR_MONTHLY))

    # --- Quarterly ---
    q_df = df.copy()
    q_df["q"] = [ _quarter_start(ts) for ts in q_df.index ]
    quarterly = q_df.groupby("q").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).sort_index()

    if len(quarterly) >= 1:
        cur_q = quarterly.iloc[-1]
        levels.append(KeyLevel(float(cur_q["open"]), "Quarterly Open", COLOR_QUARTERLY))
    if len(quarterly) >= 2:
        prev_q = quarterly.iloc[-2]
        pqh, pql = float(prev_q["high"]), float(prev_q["low"])
        levels.append(KeyLevel((pqh + pql) / 2, "Prev Quarter Mid", COLOR_QUARTERLY))

    # --- Yearly ---
    year_start = pd.Timestamp(year=now_ts.year, month=1, day=1, tz="UTC")
    ytd = df[df.index >= year_start]
    if not ytd.empty:
        levels.append(KeyLevel(float(ytd.iloc[0]["open"]), "Yearly Open", COLOR_YEARLY))
        cyh, cyl = float(ytd["high"].max()), float(ytd["low"].min())
        levels.append(KeyLevel((cyh + cyl) / 2, "Current Year Mid", COLOR_YEARLY))

    return merge_levels(levels)


def nearest_levels(levels: list[KeyLevel], spot: float, n: int = 3) -> list[KeyLevel]:
    """Return the n key levels closest to spot price."""
    if not levels:
        return []
    return sorted(levels, key=lambda lv: abs(lv.price - spot))[:n]
