"""Eva Lab strategy funnel — every book the shop runs, staged by approval.

The Lab tab used to show only the HQ holding-period experiment. This module
widens it into the strategy-development funnel: one row per strategy book
(HQ control + lab variants, the Kalshi sleeves, the altcoin wick clones, the
Trade Mill), each placed in the stage of the approval process it has reached.

The valuation process the stages encode (see ``EVA_VARIANTS_PREREG.md`` and
``dashboard/edge_analytics.py``): a strategy is measured on its *recorded
ledger* — R per trade at equal dollar risk for the HQ/perps books, P&L
against the seed bankroll for the Kalshi books, per-idea percent return for
the Trade Mill — plus the day-clustered bootstrap P(edge>0), shown inline
in the P(>0) column (same method as Investor Analytics: daily sums in the
book's native unit, resampled with replacement; the unit cancels out of a
sign probability, so R / $ / % books stay honest side by side). Advancing a
stage is an operator decision recorded in ``STRATEGY_STAGE`` with the
evidence, never inferred from a leaderboard: a book can lead every column
here and still be Stage 1 until the pre-registered bar clears.

Mode (LIVE / LIVE* / PAPER) is deliberately independent of stage — control,
the wick book and the two mirrors trade real money today by operator risk
decisions, and the funnel must not present that as approval.
"""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Matches dashboard.edge_analytics.BOOT_DRAWS — same method, same resolution.
_BOOT_DRAWS = 20_000

STAGE_META: tuple[dict[str, Any], ...] = (
    {
        "stage": 1,
        "name": "Incubation",
        "tagline": (
            "Paper and lab books building a recorded ledger. A LIVE / LIVE* "
            "badge here is a bounded operator risk decision running ahead "
            "of the evidence — never an approval."
        ),
        # Amended 2026-10-01 (operator): Stage 2 is entered on a positive
        # live-eligible record; the full statistical bar that used to gate
        # it now gates Stage 3, so nothing gets approved on less evidence —
        # Stage 2 just admits books into measured live validation sooner.
        "criteria_label": (
            "Graduation bar → Stage 2 — either path, plus the structure "
            "requirement:"
        ),
        "criteria": (
            "Recorded edge — ≥ 500 closed positions over ≥ 10 trading days "
            "with day-clustered bootstrap P(edge>0) ≥ 0.85 in the book's "
            "native unit. Total P&L never qualifies, sizing can fake it.",
            "Operator sponsorship — OR an operator-sponsored live sleeve at "
            "bounded size: positive recorded P&L, live fills recorded as "
            "real fills, and an explicit size-step plan. Recorded as a "
            "decision, not evidence; a P(edge>0) on fewer than ~5 trading "
            "days is not read as evidence either way.",
            "Structure — bounded risk (per-bot contract cap), daily-loss "
            "halt armed on the live sleeve, honest accounting.",
        ),
    },
    {
        "stage": 2,
        "name": "Live validation",
        "tagline": (
            "Trading real money at stepped size while the full statistical "
            "bar is earned on the live record."
        ),
        "criteria_label": "Graduation bar → Stage 3 — all must clear:",
        "criteria": (
            "Edge — mean per-trade edge beats zero net of fees with a "
            "day-clustered bootstrap 95% CI that excludes zero (≈ "
            "P(edge>0) ≥ 0.975), on ≥ 60 closed live positions "
            "(EVA_VARIANTS_PREREG.md §4).",
            "Placebo & mechanism — beats random entries under identical "
            "geometry, and the pre-registered mechanism metric moved as "
            "predicted.",
            "Execution — live fee- and slippage-adjusted edge stays "
            "inside the paper book's bootstrap CI; divergence is an "
            "execution problem to fix, never a reason to re-base.",
            "Size — stepped size-ups with a fill audit at each step; "
            "the per-trade edge must survive size.",
            "Controls — stop, daily-loss halt and kill-switch each "
            "exercised on a real fill.",
            "Demotion — P(edge>0) below 0.50 over ≥ 5 live trading days "
            "returns the book to Stage 1 and paper.",
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
# and citing the evidence in the changelog. Anything unlisted is Stage 1.
STRATEGY_STAGE: dict[str, int] = {
    # 2026-10-01, recorded-edge path: 1,468 closed / 15 days / P(>0) 0.89.
    "kalshi:eva_wick": 2,
    # 2026-10-01, sponsorship path: 58 closed / 2 days / +$148 on $225 seed;
    # live at 25 ct, step to 50 staged for 10-02.
    "kalshi:eva_wick_sol": 2,
}

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


# ---------------------------------------------------------------------------
# P(edge>0) — the same day-clustered bootstrap Investor Analytics runs
# (edge_analytics._stats), computed here per funnel row from each book's own
# recorded series in its native unit (R for HQ, $ for Kalshi, % for the
# mill). A sign probability is unit-invariant, so the funnel's "units are
# never blended" rule survives. Cached per (book, n_closed): the 60s poller
# must not re-run 20k draws on books that have not closed a trade since.
# ---------------------------------------------------------------------------

_pedge_cache: dict[str, tuple[int, float | None]] = {}


def _bootstrap_p_edge(
    rows: list[tuple[str, float]], *, key: str
) -> float | None:
    """Day-clustered bootstrap P(sum>0) over daily sums of ``rows``.

    ``rows`` = [(closed_at, value)]. Same-day trades share the tape, so days
    (not trades) are the resampling unit — identical to edge_analytics.
    Needs >= 2 trading days; below that the answer is "no information" (None),
    never a number. Seeded per (key, state) so the figure is stable between
    polls instead of flickering by resampling noise.
    """
    by_day: dict[str, float] = defaultdict(float)
    for ts, value in rows:
        if ts:
            by_day[str(ts)[:10]] += float(value)
    daily = list(by_day.values())
    k = len(daily)
    if k < 2:
        return None
    rng = random.Random(f"{key}:{k}:{len(rows)}")
    pos = 0
    for _ in range(_BOOT_DRAWS):
        s = 0.0
        for _ in range(k):
            s += daily[rng.randrange(k)]
        if s > 0:
            pos += 1
    return round(pos / _BOOT_DRAWS, 2)


def _p_edge_cached(
    key: str, n_closed: int, series_fn: Callable[[], list[tuple[str, float]]]
) -> float | None:
    """Bootstrap for ``key``, recomputed only when its closed count moves.

    A failed series read keeps the last computed figure (or None) — a flaky
    ledger must degrade the column, never the whole funnel.
    """
    cached = _pedge_cache.get(key)
    if cached is not None and cached[0] == int(n_closed or 0):
        return cached[1]
    try:
        p_edge = _bootstrap_p_edge(series_fn(), key=key)
    except Exception:
        logger.exception("eva lab funnel: P(edge>0) failed for %s", key)
        return cached[1] if cached else None
    _pedge_cache[key] = (int(n_closed or 0), p_edge)
    return p_edge


def _hq_series(variant: str, epoch: str) -> list[tuple[str, float]]:
    """(closed_at, realized_r) for one HQ book since the experiment epoch."""
    import eva_variants

    import eva_variants_bridge

    if variant == eva_variants.CONTROL:
        rows = eva_variants_bridge.control_positions(epoch or None)
    else:
        rows = [
            r for r in eva_variants.closed_positions(variant, limit=10_000)
            if not epoch or str(r.get("closed_at") or "") >= epoch
        ]
    return [
        (str(r.get("closed_at") or ""), float(r["realized_r"]))
        for r in rows
        if r.get("realized_r") is not None and r.get("closed_at")
    ]


def _kalshi_series(bot_id: str, since: str) -> list[tuple[str, float]]:
    """(closed_at, pnl_usd) for one Kalshi book since its family epoch."""
    import kalshi_bridge

    conn = kalshi_bridge._connect(kalshi_bridge.kalshi_db_path())
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT closed_at, pnl_usd FROM paper_positions"
            " WHERE bot_id = ? AND status != 'open'"
            " AND pnl_usd IS NOT NULL AND closed_at >= ?",
            (str(bot_id), since or ""),
        ).fetchall()
        return [(str(r["closed_at"]), float(r["pnl_usd"])) for r in rows]
    finally:
        conn.close()


def _mill_series() -> list[tuple[str, float]]:
    """(closed_at, pnl_pct) for every resolved mill idea — the series behind
    the summary's pnl_pct_sum, in the same per-idea percent unit."""
    import trade_ideas_bridge

    conn = trade_ideas_bridge._connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT closed_at, pnl_pct FROM paper_trades"
            " WHERE closed_at IS NOT NULL AND pnl_pct IS NOT NULL"
        ).fetchall()
        return [(str(r["closed_at"]), float(r["pnl_pct"])) for r in rows]
    finally:
        conn.close()


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
    p_edge: float | None = None,
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
        "p_edge": round(p_edge, 2) if p_edge is not None else None,
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
    epoch = str(payload.get("epoch") or "")
    rows = []
    for b in payload.get("books", []):
        mode = str(b.get("mode") or "paper")
        variant = str(b.get("variant") or "")
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
            p_edge=_p_edge_cached(
                f"hq:{variant}", b.get("n_closed") or 0,
                lambda v=variant: _hq_series(v, epoch),
            ),
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

    def convert(
        b: dict[str, Any], *, family: str, coins: list[str], epoch: str
    ) -> dict[str, Any]:
        seed = float(b.get("starting_usd") or 0)
        pnl = float(b.get("epoch_pnl_usd") or 0)
        bot_id = str(b.get("bot_id") or "")
        closed = b.get("closed") or 0
        return _row(
            key=f"kalshi:{b.get('bot_id')}",
            family=family,
            label=str(b.get("label") or b.get("bot_id") or "?"),
            blurb=str(b.get("blurb") or b.get("series") or ""),
            coins=coins,
            mode=str(b.get("mode") or "paper"),
            n_closed=closed,
            n_open=b.get("open") or 0,
            win_rate=b.get("win_rate"),
            p_edge=_p_edge_cached(
                f"kalshi:{bot_id}", closed,
                lambda: _kalshi_series(bot_id, epoch),
            ),
            pnl_usd=pnl,
            pnl_pct=(pnl / seed * 100.0) if seed > 0 else None,
        )

    # Each family's series is epoch-filtered with the same epoch the bridge
    # used for the row's own n / P&L, so the column never mixes windows.
    rows = [
        convert(b, family="Kalshi 15m", coins=list(_KALSHI_SLEEVE_COINS),
                epoch=kalshi_bridge.experiment_epoch())
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
            epoch=kalshi_bridge.alt_epoch(),
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
            epoch=kalshi_bridge.hourly_epoch(),
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
            epoch=kalshi_bridge.cross_epoch(),
        ))
    return rows


def _mill_row() -> dict[str, Any] | None:
    """The Trade Mill's sized-idea book; P&L in percent-of-notional units."""
    import trade_ideas_bridge

    import bot_config

    payload = trade_ideas_bridge.volume_book_payload(limit=50)
    if not payload or not payload.get("available"):
        return None
    summary = payload.get("summary") or {}
    # 2026-10-01: mode follows the fill switches instead of being hardcoded —
    # with auto-fill, any-accept, and the operator id list all off, no mill
    # idea can reach real money and the row must say paper.
    mill_live = bool(
        getattr(bot_config, "LIVE_MILL_AUTO_FILL_ENABLED", False)
        or getattr(bot_config, "LIVE_MILL_ANY_ACCEPT_FILLS", False)
        or getattr(bot_config, "LIVE_MILL_FILL_TELEGRAM_IDS", ())
    )
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
        mode="live_mirror" if mill_live else "paper",
        mode_note=_MILL_NOTE if mill_live else None,
        n_closed=summary.get("closed") or 0,
        n_open=summary.get("open") or 0,
        win_rate=summary.get("win_rate"),
        p_edge=_p_edge_cached(
            "mill:ideas", summary.get("closed") or 0, _mill_series,
        ),
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
