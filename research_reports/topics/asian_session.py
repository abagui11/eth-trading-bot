"""Asian session (9pm–4am ET) net-change windows from Coinbase H1."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import research
from research_reports.format import ResearchReport

_ET = ZoneInfo("America/New_York")

# 21:00 ET inclusive → 04:00 ET exclusive (7 H1 bars: 21,22,23,0,1,2,3)
_SESSION_HOURS = frozenset({21, 22, 23, 0, 1, 2, 3})
_SESSION_OPEN_HOUR = 21
_SESSION_LAST_HOUR = 3

# Lookback windows Dan-style: 2 weeks, 4 weeks, ~2 months
_WINDOWS: tuple[tuple[str, int], ...] = (
    ("2 weeks", 14),
    ("4 weeks", 28),
    ("2 months", 60),
)

_DEFAULT_PRODUCT = "BTC-USD"
_FETCH_BUFFER_HOURS = 48  # DST / incomplete-session padding


@dataclass(frozen=True)
class SessionMove:
    session_date: date  # ET calendar date of the 21:00 open
    open: float
    close: float
    high: float
    low: float
    change: float
    change_pct: float
    bar_count: int


@dataclass(frozen=True)
class WindowStats:
    label: str
    days: int
    sessions: int
    net_change: float
    net_change_pct: float
    avg_change: float
    median_change: float
    up_pct: float
    down_pct: float
    avg_range: float


def _parse_ts(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET)


def _in_asian_session(ts_et: datetime) -> bool:
    return ts_et.hour in _SESSION_HOURS


def _session_date(ts_et: datetime) -> date:
    """Label session by the ET date of its 21:00 open."""
    if ts_et.hour < 4:
        return ts_et.date() - timedelta(days=1)
    return ts_et.date()


def group_asian_sessions(bars: list[dict]) -> list[SessionMove]:
    """Collapse H1 bars into completed Asian sessions (open→close)."""
    buckets: dict[date, list[tuple[datetime, dict]]] = {}
    for bar in bars:
        ts_et = _parse_ts(bar["ts"])
        if not _in_asian_session(ts_et):
            continue
        key = _session_date(ts_et)
        buckets.setdefault(key, []).append((ts_et, bar))

    sessions: list[SessionMove] = []
    for session_date, items in sorted(buckets.items()):
        items.sort(key=lambda x: x[0])
        hours = {ts.hour for ts, _ in items}
        # Require open and last hour so open→close spans the full window.
        if _SESSION_OPEN_HOUR not in hours or _SESSION_LAST_HOUR not in hours:
            continue
        first = items[0][1]
        last = items[-1][1]
        open_px = float(first["open"])
        close_px = float(last["close"])
        high_px = max(float(b["high"]) for _, b in items)
        low_px = min(float(b["low"]) for _, b in items)
        change = close_px - open_px
        change_pct = (change / open_px * 100.0) if open_px else 0.0
        sessions.append(
            SessionMove(
                session_date=session_date,
                open=open_px,
                close=close_px,
                high=high_px,
                low=low_px,
                change=change,
                change_pct=change_pct,
                bar_count=len(items),
            )
        )
    return sessions


def _window_stats(
    sessions: list[SessionMove],
    *,
    label: str,
    days: int,
    as_of: date,
) -> WindowStats | None:
    cutoff = as_of - timedelta(days=days)
    # Exclude today's incomplete session label if somehow present.
    window = [s for s in sessions if cutoff < s.session_date <= as_of]
    if not window:
        return None
    changes = [s.change for s in window]
    net = sum(changes)
    first_open = window[0].open
    net_pct = (net / first_open * 100.0) if first_open else 0.0
    up = sum(1 for c in changes if c > 0)
    down = sum(1 for c in changes if c < 0)
    n = len(changes)
    return WindowStats(
        label=label,
        days=days,
        sessions=n,
        net_change=net,
        net_change_pct=net_pct,
        avg_change=statistics.mean(changes),
        median_change=statistics.median(changes),
        up_pct=100.0 * up / n,
        down_pct=100.0 * down / n,
        avg_range=statistics.mean(s.high - s.low for s in window),
    )


def _fmt_px(value: float, product_id: str) -> str:
    if product_id.startswith("BTC"):
        return f"${value:,.0f}" if abs(value) >= 100 else f"${value:,.2f}"
    return f"${value:,.2f}"


def _fmt_signed(value: float, product_id: str) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{_fmt_px(abs(value), product_id)}"


def _asset_label(product_id: str) -> str:
    return product_id.split("-")[0]


def build_asian_session_report(
    *,
    product_id: str = _DEFAULT_PRODUCT,
    now: datetime | None = None,
) -> ResearchReport:
    asset = _asset_label(product_id)
    max_days = max(d for _, d in _WINDOWS)
    fetch_count = max_days * 24 + _FETCH_BUFFER_HOURS

    try:
        bars = research.fetch_h1_bars(fetch_count, product_id=product_id)
    except Exception as exc:
        return ResearchReport(
            topic="asian_session",
            title=f"{asset} Asian Session",
            headline="Asian session data temporarily unavailable.",
            sections=[("Error", [f"• {exc}"])],
            interpretation=["Retry later — Coinbase H1 fetch failed."],
            sources=["Coinbase H1 candles"],
        )

    sessions = group_asian_sessions(bars)
    now_et = (now or datetime.now(timezone.utc)).astimezone(_ET)
    # Latest completed session date (ET label of the 21:00 open).
    # Before 04:00 ET we are still inside yesterday's session.
    if now_et.hour < 4:
        as_of = now_et.date() - timedelta(days=2)
    else:
        as_of = now_et.date() - timedelta(days=1)

    completed = [s for s in sessions if s.session_date <= as_of]
    stats_rows = [
        _window_stats(completed, label=label, days=days, as_of=as_of)
        for label, days in _WINDOWS
    ]
    stats_rows = [s for s in stats_rows if s is not None]

    if not stats_rows:
        return ResearchReport(
            topic="asian_session",
            title=f"{asset} Asian Session",
            headline=f"No completed Asian sessions found for {product_id}.",
            interpretation=["Need H1 coverage spanning 21:00–04:00 ET."],
            sources=["Coinbase H1 candles"],
        )

    headline_bits = [
        f"{s.label}: {_fmt_signed(s.net_change, product_id)} "
        f"({s.net_change_pct:+.2f}%, n={s.sessions})"
        for s in stats_rows
    ]
    headline = f"{asset} Asian session net (9pm–4am ET) — " + " | ".join(headline_bits)

    sections: list[tuple[str, list[str]]] = []
    for s in stats_rows:
        sections.append(
            (
                f"{s.label} ({s.sessions} sessions)",
                [
                    f"• Net change (sum of session open→close): "
                    f"{_fmt_signed(s.net_change, product_id)} ({s.net_change_pct:+.2f}%)",
                    f"• Avg session: {_fmt_signed(s.avg_change, product_id)}",
                    f"• Median session: {_fmt_signed(s.median_change, product_id)}",
                    f"• Up / down sessions: {s.up_pct:.0f}% / {s.down_pct:.0f}%",
                    f"• Avg session range (H–L): {_fmt_px(s.avg_range, product_id)}",
                ],
            )
        )

    sections.append(
        (
            "Definition",
            [
                "• Asian session = 21:00–04:00 America/New_York (DST-aware)",
                "• Per session: open of 21:00 ET H1 → close of 03:00 ET H1",
                "• Incomplete current session excluded",
                f"• Product: {product_id}",
            ],
        )
    )

    # Short interpretation from the shortest window bias.
    short = stats_rows[0]
    if short.net_change > 0:
        bias = f"Net positive over {short.label} Asian sessions."
    elif short.net_change < 0:
        bias = f"Net negative over {short.label} Asian sessions."
    else:
        bias = f"Flat over {short.label} Asian sessions."

    return ResearchReport(
        topic="asian_session",
        title=f"{asset} Asian Session",
        headline=headline,
        sections=sections,
        interpretation=[
            bias,
            "Session net is the sum of discrete overnight moves — not a continuous hold P&L.",
            "Use as context only; structure still drives entries.",
        ],
        sources=[f"Coinbase {product_id} H1"],
    )
