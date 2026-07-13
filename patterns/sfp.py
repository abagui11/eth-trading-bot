"""Swing Fail Pattern detection and event assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from patterns import config
from patterns.outcomes import (
    OutcomeA,
    score_outcome_a,
    score_outcome_b,
    score_outcome_c,
)
from patterns.swing import Pivot, find_pivots

Direction = Literal["bullish", "bearish"]

# Expected bar spacing per timeframe (hours) for resample sanity checks.
_BAR_SPACING_HOURS: dict[str, float] = {
    "W1": 168.0,
    "H12": 12.0,
    "H4": 4.0,
    "H1": 1.0,
    "M5": 5.0 / 60.0,
}


@dataclass
class SFPEvent:
    ts: str
    bar_idx: int
    timeframe: str
    direction: Direction
    swept_level: float
    sweep_depth_pct: float
    aligns_prior_swing: bool
    volume_spike: bool
    outcome_a: OutcomeA
    outcome_b: bool | None
    outcome_c: bool | None


def _volume_spike(df: pd.DataFrame, idx: int) -> bool:
    lookback = config.VOLUME_AVG_LOOKBACK
    start = max(0, idx - lookback)
    window = df.iloc[start:idx]
    if window.empty:
        return False
    avg_vol = float(window["volume"].mean())
    if avg_vol <= 0:
        return False
    return float(df.iloc[idx]["volume"]) >= avg_vol * config.VOLUME_SPIKE_MULT


def _aligns_prior_swing(pivot: Pivot, prior_pivots: list[Pivot]) -> bool:
    """Pivot price aligns with an earlier same-timeframe swing within tolerance."""
    tol = config.PRIOR_SWING_TOLERANCE_PCT
    for prior in prior_pivots:
        if prior.idx >= pivot.idx:
            continue
        if prior.kind != pivot.kind:
            continue
        diff = abs(prior.price - pivot.price) / pivot.price
        if diff <= tol:
            return True
    return False


def _is_extreme_pivot(df: pd.DataFrame, pivot: Pivot, lookback: int) -> bool:
    """Pivot is the directional extreme over the prior lookback bars."""
    start = max(0, pivot.idx - lookback)
    window = df.iloc[start : pivot.idx + 1]
    if window.empty:
        return False
    if pivot.kind == "high":
        return pivot.price >= float(window["high"].max())
    return pivot.price <= float(window["low"].min())


def _assert_uniform_spacing(df: pd.DataFrame, timeframe: str) -> None:
    """Guard against shifted pivots from irregular resample spacing."""
    expected = _BAR_SPACING_HOURS.get(timeframe)
    if expected is None or len(df) < 2:
        return
    deltas = df.index.to_series().diff().dropna().dt.total_seconds() / 3600.0
    median = float(deltas.median())
    if abs(median - expected) > expected * 0.05:
        raise ValueError(
            f"{timeframe} bar spacing median {median:.1f}h != expected {expected:.1f}h"
        )


def _sweep_depth_pct(
    direction: Direction,
    bar: pd.Series,
    swept_level: float,
) -> float:
    if direction == "bearish":
        if swept_level <= 0:
            return 0.0
        return (float(bar["high"]) - swept_level) / swept_level
    if swept_level <= 0:
        return 0.0
    return (swept_level - float(bar["low"])) / swept_level


def _detection_params(timeframe: str) -> dict[str, int | float | bool]:
    defaults: dict[str, int | float | bool] = {
        "pivot_left": config.PIVOT_LEFT,
        "pivot_right": config.PIVOT_RIGHT,
        "min_sweep_pct": config.MIN_SWEEP_PCT,
        "max_pivot_age": 0,
        "min_bars_since_pivot": config.PIVOT_RIGHT,
        "latest_pivot_only": True,
        "require_extreme_pivot": False,
        "extreme_lookback": 10,
    }
    tf_cfg = config.DETECTION.get(timeframe, {})
    return {**defaults, **tf_cfg}


def _candidate_pivots(
    pivots: list[Pivot],
    bar_idx: int,
    params: dict[str, int | float | bool],
) -> list[tuple[Direction, Pivot]]:
    max_age = int(params["max_pivot_age"])
    min_gap = int(params["min_bars_since_pivot"])
    latest_only = bool(params["latest_pivot_only"])

    latest_high: Pivot | None = None
    latest_low: Pivot | None = None
    for pivot in pivots:
        if pivot.idx >= bar_idx:
            break
        age = bar_idx - pivot.idx
        if max_age and age > max_age:
            continue
        if age < min_gap:
            continue
        if pivot.kind == "high":
            latest_high = pivot
        else:
            latest_low = pivot

    if latest_only:
        candidates: list[tuple[Direction, Pivot]] = []
        if latest_high is not None:
            candidates.append(("bearish", latest_high))
        if latest_low is not None:
            candidates.append(("bullish", latest_low))
        return candidates

    # Window mode: all qualifying pivots in lookback (dedup keeps most recent per bar).
    candidates = []
    for pivot in pivots:
        if pivot.idx >= bar_idx:
            break
        age = bar_idx - pivot.idx
        if max_age and age > max_age:
            continue
        if age < min_gap:
            continue
        direction: Direction = "bearish" if pivot.kind == "high" else "bullish"
        candidates.append((direction, pivot))
    return candidates


def detect_sfps(
    bars: list[dict],
    timeframe: str = "W1",
) -> list[SFPEvent]:
    """Detect SFP events on OHLC bars and score outcomes A/B/C."""
    params = _detection_params(timeframe)
    pivot_left = int(params["pivot_left"])
    pivot_right = int(params["pivot_right"])
    min_sweep = float(params["min_sweep_pct"])
    min_gap = int(params["min_bars_since_pivot"])
    require_extreme = bool(params["require_extreme_pivot"])
    extreme_lookback = int(params["extreme_lookback"])

    df = pd.DataFrame(bars)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float}
    )
    _assert_uniform_spacing(df, timeframe)

    pivots = find_pivots(df, left=pivot_left, right=pivot_right)
    n_bars = config.OUTCOME_N.get(timeframe, config.OUTCOME_N["W1"])
    # Keep the most recent qualifying swept pivot per (bar, direction).
    best_by_bar: dict[tuple[int, str], tuple[int, SFPEvent]] = {}

    for i in range(len(df)):
        bar = df.iloc[i]
        high = float(bar["high"])
        low = float(bar["low"])

        for direction, pivot in _candidate_pivots(pivots, i, params):
            if i - pivot.idx < min_gap:
                continue
            if require_extreme and not _is_extreme_pivot(df, pivot, extreme_lookback):
                continue

            if direction == "bearish":
                min_high = pivot.price * (1 + min_sweep)
                if not (high > min_high and float(bar["close"]) < pivot.price):
                    continue
            else:
                max_low = pivot.price * (1 - min_sweep)
                if not (low < max_low and float(bar["close"]) > pivot.price):
                    continue

            key = (i, direction)
            # Prefer the nearest (most recent) swept swing for this bar.
            if key in best_by_bar and pivot.idx <= best_by_bar[key][0]:
                continue

            depth = _sweep_depth_pct(direction, bar, pivot.price)
            outcome_a = score_outcome_a(
                df, i, direction, pivot.price, n_bars, timeframe=timeframe
            )
            earlier = [p for p in pivots if p.idx < pivot.idx]
            event = SFPEvent(
                ts=df.index[i].strftime("%Y-%m-%dT%H:%M:%SZ"),
                bar_idx=i,
                timeframe=timeframe,
                direction=direction,
                swept_level=pivot.price,
                sweep_depth_pct=round(depth * 100, 3),
                aligns_prior_swing=_aligns_prior_swing(pivot, earlier),
                volume_spike=_volume_spike(df, i),
                outcome_a=outcome_a,
                outcome_b=score_outcome_b(df, i, direction, n_bars),
                outcome_c=score_outcome_c(df, i, direction, n_bars, pivots=pivots),
            )
            best_by_bar[key] = (pivot.idx, event)

    return [pair[1] for pair in best_by_bar.values()]


def compute_stats(events: list[SFPEvent]) -> dict:
    """Aggregate headline and secondary stats."""
    reversals = sum(1 for e in events if e.outcome_a == "reversal")
    invalidations = sum(1 for e in events if e.outcome_a == "invalidation")
    neutral = sum(1 for e in events if e.outcome_a == "neutral")
    pending = sum(1 for e in events if e.outcome_a == "pending")
    scored = reversals + invalidations
    reversal_pct = (reversals / scored * 100) if scored > 0 else 0.0

    bc_eligible = sum(1 for e in events if e.outcome_b is not None)
    b_true = sum(1 for e in events if e.outcome_b is True)
    c_true = sum(1 for e in events if e.outcome_c is True)

    return {
        "total_sfps": len(events),
        "reversals": reversals,
        "invalidations": invalidations,
        "neutral": neutral,
        "pending": pending,
        "reversal_pct": round(reversal_pct, 1),
        "outcome_b_pct": round(b_true / bc_eligible * 100, 1) if bc_eligible else 0.0,
        "outcome_c_pct": round(c_true / bc_eligible * 100, 1) if bc_eligible else 0.0,
        "outcome_b_count": b_true,
        "outcome_c_count": c_true,
        "outcome_bc_eligible": bc_eligible,
    }
