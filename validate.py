"""Layer 2 trade risk validation — stop distance, recomputed R/R, sizing feasibility."""

from __future__ import annotations

import config
from models import Suggestion

LONG_ACTIONS = frozenset({"spot_buy", "deriv_buy"})
SHORT_ACTIONS = frozenset({"spot_sell", "deriv_sell"})
TRADE_ACTIONS = LONG_ACTIONS | SHORT_ACTIONS

# Trading Guide: SL placed ~0.25% from HTF swing (blocks $1 micro-stops on ~$1600 ETH).
MIN_STOP_DISTANCE_PCT = 0.0025
MIN_RISK_REWARD = 1.0
TARGET_RISK_PCT = 0.01
# Reject when unleveraged paper cash cannot fund enough size to risk this fraction of target.
MIN_ACHIEVABLE_RISK_FRACTION = 0.80


def trade_side(action: str) -> str:
    if action in LONG_ACTIONS:
        return "long"
    if action in SHORT_ACTIONS:
        return "short"
    raise ValueError(f"Not a trade action: {action}")


def stop_distance_pct(entry: float, stop_loss: float) -> float:
    if entry <= 0:
        return 0.0
    return abs(entry - stop_loss) / entry


def first_take_profit(action: str, take_profits: list[float]) -> float:
    if not take_profits:
        raise ValueError("take_profits required")
    if action in LONG_ACTIONS:
        return min(take_profits)
    return max(take_profits)


def compute_risk_reward(
    entry: float,
    stop_loss: float,
    take_profits: list[float],
    action: str,
) -> float:
    risk = abs(entry - stop_loss)
    if risk <= 0:
        raise ValueError("stop_loss must differ from entry")
    tp = first_take_profit(action, take_profits)
    if action in LONG_ACTIONS:
        reward = tp - entry
    else:
        reward = entry - tp
    if reward <= 0:
        raise ValueError("first take profit must be on the profit side of entry")
    return reward / risk


def achievable_risk_usd(
    entry: float,
    stop_loss: float,
    portfolio_value: float,
) -> float:
    """USD loss at stop when sized for 1% risk, capped by available unleveraged cash."""
    sl_pct = stop_distance_pct(entry, stop_loss)
    if sl_pct <= 0:
        return 0.0
    target_risk = portfolio_value * TARGET_RISK_PCT
    required_notional = target_risk / sl_pct
    deployable = min(required_notional, portfolio_value)
    return deployable * sl_pct


def validate_trade_risk(
    suggestion: Suggestion,
    portfolio_value: float | None = None,
) -> None:
    """Validate stop width, direction, R/R (recomputed), and 1% sizing feasibility.

    Overwrites ``suggestion.risk_reward`` with the recomputed value on success.
    """
    if suggestion.action not in TRADE_ACTIONS:
        return

    entry = float(suggestion.entry)  # type: ignore[arg-type]
    stop_loss = float(suggestion.stop_loss)  # type: ignore[arg-type]
    take_profits = list(suggestion.take_profits)
    action = suggestion.action
    side = trade_side(action)
    portfolio = portfolio_value if portfolio_value is not None else config.PAPER_PORTFOLIO_VALUE

    if side == "long":
        if stop_loss >= entry:
            raise ValueError(f"long stop_loss {stop_loss:,.2f} must be below entry {entry:,.2f}")
        for tp in take_profits:
            if tp <= entry:
                raise ValueError(
                    f"long take_profit {tp:,.2f} must be above entry {entry:,.2f}"
                )
    else:
        if stop_loss <= entry:
            raise ValueError(f"short stop_loss {stop_loss:,.2f} must be above entry {entry:,.2f}")
        for tp in take_profits:
            if tp >= entry:
                raise ValueError(
                    f"short take_profit {tp:,.2f} must be below entry {entry:,.2f}"
                )

    sl_pct = stop_distance_pct(entry, stop_loss)
    if sl_pct < MIN_STOP_DISTANCE_PCT:
        raise ValueError(
            f"stop distance {sl_pct * 100:.3f}% below minimum "
            f"{MIN_STOP_DISTANCE_PCT * 100:.2f}% "
            f"(entry {entry:,.2f}, stop {stop_loss:,.2f})"
        )

    rr = compute_risk_reward(entry, stop_loss, take_profits, action)
    if rr < MIN_RISK_REWARD:
        tp = first_take_profit(action, take_profits)
        raise ValueError(
            f"recomputed R/R {rr:.2f} below {MIN_RISK_REWARD} minimum "
            f"(entry {entry:,.2f}, stop {stop_loss:,.2f}, first TP {tp:,.2f})"
        )

    target_risk = portfolio * TARGET_RISK_PCT
    achievable = achievable_risk_usd(entry, stop_loss, portfolio)
    min_required = target_risk * MIN_ACHIEVABLE_RISK_FRACTION
    if achievable < min_required:
        raise ValueError(
            f"stop too tight for 1% risk on ${portfolio:,.0f} unleveraged paper "
            f"(achievable risk ${achievable:.2f} < ${min_required:.2f} at "
            f"{sl_pct * 100:.3f}% stop distance)"
        )

    suggestion.risk_reward = round(rr, 4)
