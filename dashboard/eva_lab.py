"""Eva Lab strategy funnel — every book the shop runs, staged by approval.

The Lab tab used to show only the HQ holding-period experiment. This module
widens it into the strategy-development funnel: one row per strategy book
(HQ control + lab variants, the Kalshi sleeves, the altcoin wick clones, the
Trade Mill), each placed in the stage of the approval process it has reached.

The valuation process the stages encode (see ``EVA_VARIANTS_PREREG.md`` and
``dashboard/edge_analytics.py``): a strategy is measured on its *recorded
ledger* — R per trade at equal dollar risk for the HQ/perps books, P&L
against the seed bankroll for the Kalshi books, per-idea percent return for
the Trade Mill — plus the day-clustered bootstrap P(edge>0) on Investor
Analytics. Advancing a stage is an operator decision recorded in
``STRATEGY_STAGE`` with the evidence, never inferred from a leaderboard:
a book can lead every column here and still be Stage 1 until the
pre-registered bar clears.

Mode (LIVE / LIVE* / PAPER) is deliberately independent of stage — control,
the wick book and the two mirrors trade real money today by operator risk
decisions, and the funnel must not present that as approval.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

STAGE_META: tuple[dict[str, Any], ...] = (
    {
        "stage": 1,
        "name": "Incubation",
        "tagline": (
            "Paper and lab books building a recorded ledger. A LIVE / LIVE* "
            "badge here is a bounded operator risk decision running ahead "
            "of the evidence — never an approval."
        ),
        "criteria_label": (
            "Graduation bar → Stage 2 — all five must clear "
            "(EVA_VARIANTS_PREREG.md §4):"
        ),
        "criteria": (
            "Sample — ≥ 60 closed positions in-epoch; below that, mean R "
            "differences are unmeasurable noise.",
            "Edge in R — mean R per trade beats its baseline (control for "
            "the HQ variants; zero net of fees for standalone books) with a "
            "day-clustered bootstrap 95% CI that excludes zero — reads as "
            "≈ P(edge>0) ≥ 0.975 on the analytics tab. Day-clustered "
            "because same-day trades share the tape; total P&L never "
            "qualifies, sizing can fake it.",
            "Placebo — beats random entries under identical bracket "
            "geometry, proving the signal (not the geometry) made the money.",
            "Mechanism — the pre-registered mechanism metric moved as "
            "predicted (e.g. stopped-then-paid rate falls for wider stops).",
            "Structure — bounded risk, honest accounting, no dependence on "
            "a tuned threshold.",
        ),
    },
    {
        "stage": 2,
        "name": "Live validation",
        "tagline": (
            "Cleared the statistical bar; now proving the edge survives "
            "real execution on a small fixed-risk sleeve."
        ),
        "criteria_label": "Graduation bar → Stage 3:",
        "criteria": (
            "Execution — live fee- and slippage-adjusted mean R stays "
            "inside the paper book's bootstrap CI; divergence is an "
            "execution problem to fix, never a reason to re-base.",
            "Size — stepped size-ups (8 → 25 → 100 contracts on Kalshi; "
            "risk-per-trade steps on perps) with a fill audit at each step; "
            "the per-trade edge must survive size.",
            "Controls — stop, daily-loss halt and kill-switch each "
            "exercised on a real fill.",
        ),
    },
    {
        "stage": 3,
        "name": "Approved & scaled",
        "tagline": (
            "Graduated. Runs sleeve capital under the scaling plan, "
            "monitored for regime drift."
        ),
        "criteria_label": "Held to:",
        "criteria": (
            "Scaling — sleeve capital per the scaling study (participation "
            "caps on Kalshi, linear risk until impact on perps).",
            "Review — P(edge>0) re-read on a fixed cadence; a failed review "
            "demotes the book, it does not re-base it.",
        ),
    },
)

# Operator-set stage per book key. Promote by adding e.g. "kalshi:eva_wick": 2
# and citing the evidence in the changelog. Anything unlisted is Stage 1 —
# as of 2026-09-30 no book has cleared its pre-registered bar, so the dict
# starts empty on purpose.
STRATEGY_STAGE: dict[str, int] = {}

_DEFAULT_STAGE = 1

_MIRROR_NOTE = (
    "Trades real money as a live mirror of this paper book (operator "
    "decision, recorded in EVA_VARIANTS_PREREG.md). NOT a promotion — the "
    "paper book stays the measurement instrument and the pre-registered bar "
    "decides."
)

_MILL_NOTE = (
    "Live fills are a capital-limited subset of this book; the sized-idea "
    "paper ledger is the measurement instrument."
)

# Coins each book trades, for the color-coded Coin column. The HQ family and
# the three Kalshi sleeves are BTC/ETH by construction; the altcoin clones
# carry their own single coin via the bridge label.
_HQ_COINS = ("BTC", "ETH")
_KALSHI_SLEEVE_COINS = ("BTC", "ETH")


def _row(
    *,
    key: str,
    family: str,
    label: str,
    blurb: str,
    coins: list[str],
    mode: str,
    mode_note: str | None = None,
    n_closed: int = 0,
    n_open: int = 0,
    win_rate: float | None = None,
    mean_r: float | None = None,
    sum_r: float | None = None,
    pnl_usd: float | None = None,
    pnl_pct: float | None = None,
    median_hold_h: float | None = None,
    stopped_n: int | None = None,
    stopped_then_paid: int | None = None,
    skips: int | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "family": family,
        "label": label,
        "blurb": blurb,
        "coins": coins,
        "mode": mode,
        "mode_note": mode_note,
        "stage": int(STRATEGY_STAGE.get(key, _DEFAULT_STAGE)),
        "n_closed": int(n_closed or 0),
        "n_open": int(n_open or 0),
        "win_rate": win_rate,
        "mean_r": mean_r,
        "sum_r": sum_r,
        "pnl_usd": round(pnl_usd, 2) if pnl_usd is not None else None,
        "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        "median_hold_h": median_hold_h,
        "stopped_n": stopped_n,
        "stopped_then_paid": stopped_then_paid,
        "skips": skips,
    }


def _hq_rows() -> list[dict[str, Any]]:
    """HQ control + the four lab variants, from the variants bridge.

    The bridge already collapses control's ladder legs by position and
    expresses every book in R at equal dollar risk, so its summaries are
    reused verbatim rather than recomputed.
    """
    import eva_variants_bridge

    payload = eva_variants_bridge.performance_payload(limit=1)
    if not payload or not payload.get("available"):
        return []
    rows = []
    for b in payload.get("books", []):
        mode = str(b.get("mode") or "paper")
        rows.append(_row(
            key=f"hq:{b.get('variant')}",
            family="HQ perps",
            label=str(b.get("label") or b.get("variant") or "?"),
            blurb=str(b.get("blurb") or ""),
            coins=list(_HQ_COINS),
            mode=mode,
            mode_note=_MIRROR_NOTE if mode == "live_mirror" else None,
            n_closed=b.get("n_closed") or 0,
            n_open=b.get("n_open") or 0,
            win_rate=b.get("win_rate"),
            mean_r=b.get("mean_r"),
            sum_r=b.get("sum_r"),
            pnl_usd=b.get("pnl_usd"),
            median_hold_h=b.get("median_hold_h"),
            stopped_n=b.get("stopped_n"),
            stopped_then_paid=b.get("stopped_then_paid"),
            skips=b.get("skips"),
        ))
    return rows


def _kalshi_rows() -> list[dict[str, Any]]:
    """The three Kalshi sleeves plus the altcoin wick clones.

    Kalshi books are dollar books against a seed bankroll — no stop-derived
    R exists, so the R columns stay None and the P&L column carries the
    epoch P&L with percent-of-seed alongside.
    """
    import kalshi_bridge

    payload = kalshi_bridge.performance_payload(limit=1)
    if not payload or not payload.get("available"):
        return []

    def convert(b: dict[str, Any], *, family: str, coins: list[str]) -> dict[str, Any]:
        seed = float(b.get("starting_usd") or 0)
        pnl = float(b.get("epoch_pnl_usd") or 0)
        return _row(
            key=f"kalshi:{b.get('bot_id')}",
            family=family,
            label=str(b.get("label") or b.get("bot_id") or "?"),
            blurb=str(b.get("blurb") or b.get("series") or ""),
            coins=coins,
            mode=str(b.get("mode") or "paper"),
            n_closed=b.get("closed") or 0,
            n_open=b.get("open") or 0,
            win_rate=b.get("win_rate"),
            pnl_usd=pnl,
            pnl_pct=(pnl / seed * 100.0) if seed > 0 else None,
        )

    rows = [
        convert(b, family="Kalshi 15m", coins=list(_KALSHI_SLEEVE_COINS))
        for b in payload.get("bots", [])
    ]
    alt = payload.get("altcoins") or {}
    for b in alt.get("bots", []):
        # The bridge labels altcoin clones by their coin ("XRP"/"SOL"/"HYPE").
        coin = str(b.get("label") or "").upper()
        row = convert(
            b,
            family="Kalshi 15m · altcoin wick",
            coins=[coin] if coin else [],
        )
        row["label"] = f"{coin} wick clone" if coin else row["label"]
        rows.append(row)
    # Hourly piggyback books: fixed strike rungs on the top-of-hour BTC/ETH
    # threshold series whenever the live wick rule fires. Both books trade
    # both coins, so the chips are BTC/ETH like the sleeves.
    hourly = payload.get("hourly") or {}
    for b in hourly.get("bots", []):
        rows.append(convert(
            b,
            family="Kalshi 1h · wick piggyback",
            coins=list(_KALSHI_SLEEVE_COINS),
        ))
    # Cross-asset wick books: a BTC/ETH wick fire the EVA board agrees with,
    # bought on an alt's 15m market. The chip is the coin the book actually
    # trades (the target), the label carries the signal ("BTC→XRP").
    cross = payload.get("cross") or {}
    for b in cross.get("bots", []):
        coin = str(b.get("coin") or "").upper()
        rows.append(convert(
            b,
            family="Kalshi 15m · cross wick",
            coins=[coin] if coin else [],
        ))
    return rows


def _mill_row() -> dict[str, Any] | None:
    """The Trade Mill's sized-idea book; P&L in percent-of-notional units."""
    import trade_ideas_bridge

    payload = trade_ideas_bridge.volume_book_payload(limit=50)
    if not payload or not payload.get("available"):
        return None
    summary = payload.get("summary") or {}
    coins = sorted({
        str(t.get("product_id") or "").replace("-USD", "")
        for t in payload.get("trades", [])
        if t.get("product_id")
    }) or ["BTC", "ETH"]
    return _row(
        key="mill:ideas",
        family="Product",
        label="Trade Mill — sized ideas",
        blurb=(
            "Every idea the mill sizes into a card, paper-tracked to TP/SL. "
            "P&L is the sum of per-idea percent returns (the daily digest "
            "unit); live clips fill a capital-limited subset of this book."
        ),
        coins=coins,
        mode="live_mirror",
        mode_note=_MILL_NOTE,
        n_closed=summary.get("closed") or 0,
        n_open=summary.get("open") or 0,
        win_rate=summary.get("win_rate"),
        pnl_pct=summary.get("pnl_pct_sum"),
    )


def funnel_payload() -> dict[str, Any]:
    """All strategy books grouped by stage, for the Eva Lab funnel tab."""
    books: list[dict[str, Any]] = []
    try:
        books.extend(_hq_rows())
    except Exception:
        logger.exception("eva lab funnel: HQ rows unavailable")
    try:
        books.extend(_kalshi_rows())
    except Exception:
        logger.exception("eva lab funnel: kalshi rows unavailable")
    try:
        mill = _mill_row()
        if mill is not None:
            books.append(mill)
    except Exception:
        logger.exception("eva lab funnel: mill row unavailable")

    stages = []
    for meta in STAGE_META:
        stages.append({
            **meta,
            "books": [b for b in books if b["stage"] == meta["stage"]],
        })
    return {
        "available": True,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_books": len(books),
        "stages": stages,
    }
