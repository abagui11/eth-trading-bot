"""Perp funding-rate regime tracker (medium-term signal).

Reads 8h funding prints for BTC/ETH perps from Binance's public API, persists
the series, and classifies a regime:

- bull_persist / bear_persist — funding held one sign for N consecutive prints
- chop — sign keeps flipping without a sustained run (noise; never mints ideas)
- first_switch_bull / first_switch_bear — the first confirmed sign change after
  a persistence regime (position-switch cue; the only funding state that emits
  a signal event for the trade_ideas mill)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

import bot_config
from intelligence import store

logger = logging.getLogger(__name__)

_BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"

REGIME_BULL = "bull_persist"
REGIME_BEAR = "bear_persist"
REGIME_CHOP = "chop"
REGIME_SWITCH_BULL = "first_switch_bull"
REGIME_SWITCH_BEAR = "first_switch_bear"


@dataclass(frozen=True)
class FundingRegime:
    product_id: str
    regime: str
    streak_periods: int
    as_of_ts: str
    latest_rate: float | None
    is_switch_event: bool


def fetch_funding_history(
    symbol: str,
    *,
    limit: int = 90,
) -> list[dict[str, Any]]:
    """Fetch recent funding prints from Binance. Returns [{ts, rate}] oldest-first."""
    response = requests.get(
        _BINANCE_FUNDING_URL,
        params={"symbol": symbol, "limit": min(limit, 1000)},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    out: list[dict[str, Any]] = []
    for item in payload:
        ts = datetime.fromtimestamp(
            int(item["fundingTime"]) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"ts": ts, "rate": float(item["fundingRate"])})
    out.sort(key=lambda r: r["ts"])
    return out


def _sign(rate: float) -> int:
    if rate > 0:
        return 1
    if rate < 0:
        return -1
    return 0


def _trailing_streak(signs: list[int]) -> tuple[int, int]:
    """(sign, length) of the trailing same-sign run (zeros break the run)."""
    if not signs:
        return 0, 0
    last = signs[-1]
    if last == 0:
        return 0, 0
    n = 0
    for s in reversed(signs):
        if s == last:
            n += 1
        else:
            break
    return last, n


def classify_regime(
    rates: list[float],
    *,
    persist_periods: int | None = None,
    switch_confirm_periods: int | None = None,
) -> tuple[str, int]:
    """Classify the funding regime from an oldest-first rate series.

    Returns (regime, trailing_streak_periods).
    """
    persist = persist_periods or bot_config.FUNDING_PERSIST_PERIODS
    confirm = switch_confirm_periods or bot_config.FUNDING_SWITCH_CONFIRM_PERIODS
    signs = [_sign(r) for r in rates]
    last_sign, streak = _trailing_streak(signs)

    if last_sign == 0 or not signs:
        return REGIME_CHOP, streak

    # Current run long enough to be a persistence regime on its own.
    if streak >= persist:
        return (REGIME_BULL if last_sign > 0 else REGIME_BEAR), streak

    # Confirmed switch: current run is at least the confirm window AND the run
    # immediately before it was a persistence regime of the opposite sign.
    if streak >= confirm:
        prior = signs[: len(signs) - streak]
        prior_sign, prior_streak = _trailing_streak(prior)
        if prior_sign == -last_sign and prior_streak >= persist:
            return (
                REGIME_SWITCH_BULL if last_sign > 0 else REGIME_SWITCH_BEAR
            ), streak

    return REGIME_CHOP, streak


def evaluate_product(
    product_id: str,
    rates_series: list[dict[str, Any]],
) -> FundingRegime:
    """Classify + persist the regime for one product. Emits switch events once."""
    rates = [float(r["rate"]) for r in rates_series]
    as_of = str(rates_series[-1]["ts"]) if rates_series else ""
    regime, streak = classify_regime(rates)

    previous = store.latest_funding_regime(product_id)
    prev_regime = (previous or {}).get("regime")
    prev_as_of = (previous or {}).get("as_of_ts")
    # A switch event fires only when we newly enter a first_switch_* state
    # (regime or bar changed) — repeated scans of the same state stay quiet.
    is_switch = regime in (REGIME_SWITCH_BULL, REGIME_SWITCH_BEAR) and (
        prev_regime != regime or prev_as_of != as_of
    )

    if previous is None or prev_regime != regime or prev_as_of != as_of:
        store.insert_funding_regime(
            product_id,
            regime,
            streak_periods=streak,
            as_of_ts=as_of,
            detail={
                "latest_rate": rates[-1] if rates else None,
                "series_len": len(rates),
                "switch_event": is_switch,
            },
        )

    return FundingRegime(
        product_id=product_id,
        regime=regime,
        streak_periods=streak,
        as_of_ts=as_of,
        latest_rate=rates[-1] if rates else None,
        is_switch_event=is_switch,
    )


def run_funding_scan() -> list[FundingRegime]:
    """Fetch funding for all tracked products, persist prints, classify regimes."""
    if not bot_config.FUNDING_ENABLED:
        return []
    results: list[FundingRegime] = []
    for product_id, symbol in bot_config.FUNDING_PRODUCTS.items():
        try:
            series = fetch_funding_history(symbol)
        except Exception:
            logger.exception("Funding fetch failed for %s (%s)", product_id, symbol)
            continue
        if not series:
            continue
        store.upsert_funding_rates(product_id, series)
        # Classify on the full persisted series (survives restarts/backfill).
        persisted = store.funding_series(product_id, limit=200)
        regime = evaluate_product(product_id, persisted)
        results.append(regime)
        logger.info(
            "Funding %s: regime=%s streak=%s as_of=%s%s",
            product_id,
            regime.regime,
            regime.streak_periods,
            regime.as_of_ts,
            " [SWITCH EVENT]" if regime.is_switch_event else "",
        )
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for r in run_funding_scan():
        print(r)
