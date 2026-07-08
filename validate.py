"""Layer 2 trade risk validation — stop distance, recomputed R/R, fixed-fraction sizing."""

from __future__ import annotations

import bot_config
import config
from models import Suggestion

LONG_ACTIONS = frozenset({"spot_buy", "deriv_buy"})
SHORT_ACTIONS = frozenset({"spot_sell", "deriv_sell"})
TRADE_ACTIONS = LONG_ACTIONS | SHORT_ACTIONS

# Trading Guide: SL placed ~0.25% from HTF swing (blocks $1 micro-stops on ~$1600 ETH).
MIN_STOP_DISTANCE_PCT = 0.0025
MIN_RISK_REWARD = 1.0


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


def compute_eth_qty(
    entry: float,
    stop_loss: float,
    *,
    cash: float | None = None,
    portfolio_value: float | None = None,
    equity_usd: float | None = None,
) -> float:
    """Fixed-fraction sizing: deploy ``TRADE_DEPLOY_PCT`` of live equity as notional.

    Stop distance does not affect size (R/R is validated separately). The result
    is capped by available cash and clamped to the MIN/MAX ETH guardrails.
    ``stop_loss`` is accepted for signature stability but is intentionally unused.
    """
    if entry <= 0:
        return 0.0
    equity = equity_usd if equity_usd is not None else portfolio_value
    if equity is None:
        equity = config.PAPER_PORTFOLIO_VALUE
    available = cash if cash is not None else equity
    if equity <= 0 or available <= 0:
        return 0.0

    notional = min(equity * bot_config.TRADE_DEPLOY_PCT, available)
    if notional <= 0:
        return 0.0

    eth_qty = notional / entry
    eth_qty = min(eth_qty, bot_config.MAX_ETH_QTY)
    if eth_qty < bot_config.MIN_ETH_QTY:
        min_notional = bot_config.MIN_ETH_QTY * entry
        if min_notional > available:
            return 0.0
        eth_qty = bot_config.MIN_ETH_QTY
    return eth_qty


def validate_trade_risk(
    suggestion: Suggestion,
    portfolio_value: float | None = None,
    *,
    cash: float | None = None,
    spot_price: float | None = None,
) -> None:
    """Validate stop width, direction, R/R (recomputed), and fixed-fraction sizing.

    Sizing uses live paper equity (``portfolio_value`` / ``equity_usd``) when not
    supplied explicitly. Pass ``spot_price`` so open-position mark-to-market is
    included in the equity calculation.

    Overwrites ``suggestion.risk_reward`` and ``suggestion.size`` on success.
    """
    if suggestion.action not in TRADE_ACTIONS:
        return

    entry = float(suggestion.entry)  # type: ignore[arg-type]
    stop_loss = float(suggestion.stop_loss)  # type: ignore[arg-type]
    take_profits = list(suggestion.take_profits)
    action = suggestion.action
    side = trade_side(action)

    equity = portfolio_value
    available_cash = cash
    if equity is None or available_cash is None:
        import paper

        resolved_equity, resolved_cash = paper.get_sizing_basis(spot_price)
        if equity is None:
            equity = resolved_equity
        if available_cash is None:
            available_cash = resolved_cash

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

    suggestion.risk_reward = round(rr, 4)

    qty = compute_eth_qty(
        entry,
        stop_loss,
        cash=available_cash,
        equity_usd=equity,
    )
    if qty <= 0:
        raise ValueError(
            f"cannot size trade for {bot_config.TRADE_DEPLOY_PCT:.0%} deployment on "
            f"${equity:,.0f} equity (entry {entry:,.2f}, stop {stop_loss:,.2f})"
        )
    suggestion.size = round(qty, 4)
