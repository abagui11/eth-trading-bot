"""Tunable parameters for pattern detection and outcome scoring."""

from __future__ import annotations

PIVOT_LEFT = 2
PIVOT_RIGHT = 2
MIN_SWEEP_PCT = 0.001  # 0.1%

# Timeframe-specific SFP detection (conservative / ICT-style liquidity sweeps).
# Weekly: meaningful swing pivots swept within a recent window (~5 months).
# H12/H1: only the latest confirmed swing within a short lookback.
DETECTION: dict[str, dict[str, int | float | bool]] = {
    "W1": {
        "pivot_left": 3,
        "pivot_right": 3,
        "min_sweep_pct": 0.002,  # 0.2% wick past level
        "max_pivot_age": 20,  # bars (~20 weeks)
        "min_bars_since_pivot": 3,
        "latest_pivot_only": False,
        "require_extreme_pivot": False,
        "extreme_lookback": 8,
    },
    "H12": {
        "pivot_left": 4,
        "pivot_right": 4,
        "min_sweep_pct": 0.003,  # 0.3% wick past level
        "max_pivot_age": 42,  # bars (~3 weeks on H12)
        "min_bars_since_pivot": 4,
        "latest_pivot_only": True,
        "require_extreme_pivot": True,
        "extreme_lookback": 20,
    },
    "H1": {
        "pivot_left": 3,
        "pivot_right": 3,
        "min_sweep_pct": 0.0015,
        "max_pivot_age": 48,  # bars (~2 days on H1)
        "min_bars_since_pivot": 3,
        "latest_pivot_only": True,
    },
}

OUTCOME_N: dict[str, int] = {
    "W1": 8,
    "D1": 10,
    "H12": 14,
    "H4": 14,
    "H1": 24,
}

MOVE_PCT_B = 0.05  # 5%

# Outcome A follow-through: min move from event close to count as reversal.
REVERSAL_MIN_MOVE: dict[str, float] = {
    "W1": 0.02,   # 2%
    "H12": 0.015,  # 1.5%
    "H1": 0.01,   # 1%
}

VOLUME_SPIKE_MULT = 1.5
VOLUME_AVG_LOOKBACK = 20

# Prior swing alignment: pivot within this % of an earlier same-TF swing.
PRIOR_SWING_TOLERANCE_PCT = 0.005  # 0.5%
