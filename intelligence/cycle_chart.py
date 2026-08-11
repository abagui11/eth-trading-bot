"""Interactive BTC 4-year cycle chart (Plotly) with phase overlays.

Produces a Plotly figure spec the hub renders client-side, so the cycle map is
zoomable/hoverable instead of a flat PNG: monthly candles on a log axis,
halving verticals, green expansion / red drawdown phase bands carrying their
% move and duration, realized top/low pivots, and dashed projected pivots.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go

from intelligence.cycle_phases import (
    CYCLE_LOWS,
    CYCLE_TOPS,
    HALVINGS,
    NEXT_HALVING_EST,
    CycleSegment,
    build_segments,
    cycle_position,
    projected_pivots,
)

logger = logging.getLogger(__name__)

_BG = "#0d1117"
_FG = "#c9d1d9"
_GRID = "#21262d"
_EXPANSION = "#238636"
_DRAWDOWN = "#b62324"
_HALVING = "#f7931a"


def to_monthly(bars: list[dict[str, Any]]) -> pd.DataFrame:
    """Resample daily bars to monthly OHLC (the cycle chart's native timeframe).

    Exchange history carries flash-crash prints (Coinbase has a $0.06 low in
    Apr 2017). On a log axis one of those drags the whole scale to zero, so
    wicks are clamped to a range no real monthly bar exceeds.
    """
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    monthly = df.resample("MS").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    monthly = monthly.dropna(subset=["close"])

    body_low = monthly[["open", "close"]].min(axis=1)
    body_high = monthly[["open", "close"]].max(axis=1)
    monthly["low"] = monthly["low"].clip(lower=body_low * 0.5).clip(upper=body_low)
    monthly["high"] = monthly["high"].clip(upper=body_high * 2.5).clip(lower=body_high)
    return monthly


def _band_color(segment: CycleSegment) -> str:
    return _EXPANSION if segment.kind == "expansion" else _DRAWDOWN


def _add_phase_bands(
    fig: go.Figure,
    segments: list[CycleSegment],
    first_bar: pd.Timestamp,
) -> None:
    """Vertical phase bands + caption. Paper-referenced so the log axis is safe."""
    for i, seg in enumerate(segments):
        color = _band_color(seg)
        opacity = 0.10 if seg.projected else (0.16 if seg.in_progress else 0.22)
        fig.add_vrect(
            x0=seg.start_date,
            x1=seg.end_date,
            fillcolor=color,
            opacity=opacity,
            layer="below",
            line_width=1 if seg.projected else 0,
            line=dict(color=color, dash="dot") if seg.projected else None,
        )
        if seg.projected:
            caption = f"projected<br>{seg.months} bars"
        elif seg.change_pct is None:
            caption = f"{seg.months} bars"
        else:
            caption = f"{seg.change_pct:+,.2f}%<br>{seg.months} bars"
        # Stagger captions so neighbouring bands never collide, and keep the
        # caption of a leg that predates our price history on screen.
        caption_x = max(pd.Timestamp(seg.start_date, tz="UTC"), first_bar)
        fig.add_annotation(
            x=caption_x,
            xanchor="left",
            xshift=6,
            y=(0.98, 0.89, 0.80)[i % 3],
            yref="paper",
            text=caption,
            showarrow=False,
            align="left",
            font=dict(size=10, color=_FG),
            bgcolor="rgba(13,17,23,0.72)",
            bordercolor=color,
            borderwidth=1,
            borderpad=3,
        )


def _add_halvings(fig: go.Figure, first: pd.Timestamp, last: pd.Timestamp) -> None:
    for halving in (*HALVINGS, NEXT_HALVING_EST):
        stamp = pd.Timestamp(halving, tz="UTC")
        if stamp < first:
            continue
        projected = halving == NEXT_HALVING_EST
        fig.add_vline(
            x=stamp.to_pydatetime(),
            line=dict(
                color=_HALVING,
                width=1.4,
                dash="dash" if projected else "solid",
            ),
            opacity=0.85,
        )
        fig.add_annotation(
            x=stamp.to_pydatetime(),
            y=0.02,
            yref="paper",
            text=f"HALVING{' (est)' if projected else ''}<br>{halving}",
            showarrow=False,
            textangle=-90,
            xshift=9,
            yanchor="bottom",
            font=dict(size=9, color=_HALVING),
        )


def _add_pivots(
    fig: go.Figure,
    monthly: pd.DataFrame,
    confirmed_top: dict[str, Any] | None = None,
) -> None:
    """Realized tops/lows on price, projected pivots as dashed time markers."""

    def close_near(when: str) -> float | None:
        stamp = pd.Timestamp(when, tz="UTC")
        prior = monthly.loc[monthly.index <= stamp]
        if prior.empty:
            return None
        return float(prior["close"].iloc[-1])

    for kind, dates, symbol, color in (
        ("Cycle top", CYCLE_TOPS, "triangle-down", "#f85149"),
        ("Cycle low", CYCLE_LOWS, "triangle-up", "#3fb950"),
    ):
        xs, ys, texts = [], [], []
        for when in dates:
            price = close_near(when)
            if price is None:
                continue
            xs.append(pd.Timestamp(when, tz="UTC"))
            ys.append(price)
            texts.append(f"{kind} · {when} · ${price:,.0f}")
        if not xs:
            continue
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers",
                name=kind,
                marker=dict(symbol=symbol, size=13, color=color,
                            line=dict(width=1, color="#0d1117")),
                hovertext=texts,
                hoverinfo="text",
            )
        )

    if confirmed_top:
        fig.add_trace(
            go.Scatter(
                x=[pd.Timestamp(confirmed_top["date"], tz="UTC")],
                y=[float(confirmed_top["price"])],
                mode="markers",
                name="Cycle top (confirmed by drawdown)",
                marker=dict(
                    symbol="triangle-down-open",
                    size=16,
                    color="#d29922",
                    line=dict(width=2, color="#d29922"),
                ),
                hovertext=[
                    f"Confirmed top · {confirmed_top['date']} · "
                    f"${float(confirmed_top['price']):,.0f}"
                ],
                hoverinfo="text",
            )
        )

    today = pd.Timestamp.now(tz="UTC")
    # A projected pivot whose date has passed is history, not a forecast.
    future = [p for p in projected_pivots() if pd.Timestamp(p.date, tz="UTC") > today]
    for i, pivot in enumerate(future):
        color = "#f85149" if pivot.kind == "top" else "#3fb950"
        fig.add_vline(
            x=pd.Timestamp(pivot.date, tz="UTC").to_pydatetime(),
            line=dict(color=color, width=1, dash="dot"),
            opacity=0.7,
        )
        fig.add_annotation(
            x=pd.Timestamp(pivot.date, tz="UTC").to_pydatetime(),
            y=0.60 if i % 2 == 0 else 0.44,
            yref="paper",
            text=f"projected {pivot.kind}<br>{pivot.date}",
            showarrow=False,
            font=dict(size=9, color=color),
            xshift=8,
            xanchor="left",
            align="left",
        )


def build_cycle_figure(
    bars: list[dict[str, Any]],
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Plotly figure spec (JSON-safe dict) for the interactive cycle chart."""
    monthly = to_monthly(bars)
    if monthly.empty:
        raise ValueError("No bars for cycle figure")

    segments = build_segments(bars, as_of=as_of)
    position = cycle_position(bars, as_of=as_of)

    fig = go.Figure(
        go.Candlestick(
            x=monthly.index,
            open=monthly["open"],
            high=monthly["high"],
            low=monthly["low"],
            close=monthly["close"],
            name="BTC-USD 1M",
            increasing=dict(line=dict(color="#3fb950"), fillcolor="#238636"),
            decreasing=dict(line=dict(color="#f85149"), fillcolor="#b62324"),
        )
    )

    _add_phase_bands(fig, segments, monthly.index[0])
    _add_halvings(fig, monthly.index[0], monthly.index[-1])
    _add_pivots(fig, monthly, position.get("confirmed_top"))

    last_close = float(monthly["close"].iloc[-1])
    fig.add_hline(
        y=last_close,
        line=dict(color=_FG, width=1, dash="dot"),
        opacity=0.6,
        annotation_text=f"spot ${last_close:,.0f}",
        annotation_position="right",
        annotation_font=dict(size=10, color=_FG),
    )

    months_since = position["months_since_halving"]
    fig.add_annotation(
        x=monthly.index[-1],
        # Annotations on a log axis take log10 coordinates (unlike trace data).
        y=math.log10(last_close),
        text=(
            f"{months_since} months since halving<br>"
            f"{position['phase_label']} · cycle {position['cycle_progress_pct']}%"
        ),
        showarrow=True,
        arrowhead=2,
        arrowcolor=_FG,
        # Point down-right into the empty runway rather than into the bands.
        ax=40,
        ay=150,
        font=dict(size=11, color=_FG),
        bgcolor="rgba(13,17,23,0.8)",
        bordercolor=_FG,
        borderwidth=1,
        borderpad=4,
    )

    fig.update_layout(
        title=dict(
            text=(
                "BTC 4-year cycle — monthly (log) · green expansion / red drawdown · "
                "dotted = projected"
            ),
            font=dict(size=14, color=_FG),
        ),
        paper_bgcolor=_BG,
        plot_bgcolor=_BG,
        font=dict(color=_FG, size=11),
        hovermode="x unified",
        showlegend=True,
        legend=dict(orientation="h", y=1.03, yanchor="bottom", x=0),
        margin=dict(l=60, r=80, t=90, b=40),
        height=620,
        xaxis=dict(
            gridcolor=_GRID,
            rangeslider=dict(visible=False),
            rangeselector=dict(
                x=1,
                xanchor="right",
                y=1.03,
                yanchor="bottom",
                buttons=[
                    dict(count=2, label="2y", step="year", stepmode="backward"),
                    dict(count=4, label="4y", step="year", stepmode="backward"),
                    dict(count=8, label="8y", step="year", stepmode="backward"),
                    dict(step="all", label="all"),
                ],
                bgcolor="#161b22",
                activecolor="#30363d",
                font=dict(color=_FG, size=10),
            ),
            # Runway so projected pivots stay visible.
            range=[
                monthly.index[0].isoformat(),
                (
                    datetime.fromisoformat(NEXT_HALVING_EST).replace(tzinfo=timezone.utc)
                    + timedelta(days=120)
                ).isoformat(),
            ],
        ),
        yaxis=dict(
            type="log",
            gridcolor=_GRID,
            title="USD (log)",
            # Log axes take log10 bounds; pin them so one odd wick can't rescale.
            range=[
                math.log10(float(monthly["low"].min()) * 0.75),
                math.log10(float(monthly["high"].max()) * 1.6),
            ],
        ),
    )

    spec = json.loads(fig.to_json())
    spec["republic"] = {
        "position": position,
        "segments": [s.to_dict() for s in segments],
    }
    return spec


def write_cycle_figure(
    bars: list[dict[str, Any]],
    out_dir: Path,
    *,
    as_of: date | None = None,
) -> str | None:
    """Persist the figure spec next to the PNG. Returns the path, or None."""
    try:
        spec = build_cycle_figure(bars, as_of=as_of)
    except Exception:
        logger.exception("Interactive cycle figure build failed")
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = out_dir / f"{stamp}_btc_cycle_figure.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)
