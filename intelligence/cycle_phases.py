"""Deterministic BTC 4-year cycle anatomy: halvings, pivots, phases, position.

One source of truth for the cycle clock. `cycle_thesis` renders it, the hub
plots it, and the LLM thesis is handed the same numbers, so the static PNG,
the interactive chart, and the written thesis can never disagree.

Cycle top/low pivots and the 1428-day projection spacing follow the public
"BTC 4-Year Cycle Labels + Future Points" mapping. Projections are a time
map, not a forecast — every projected pivot is flagged `projected=True`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

# Bitcoin halving dates (UTC). The next expected halving anchors the cycle clock.
HALVINGS: tuple[str, ...] = (
    "2012-11-28",
    "2016-07-09",
    "2020-05-11",
    "2024-04-20",
)
NEXT_HALVING_EST = "2028-04-15"

# Realized macro pivots. Tops are the euphoric cycle highs, lows the bear bottoms.
CYCLE_TOPS: tuple[str, ...] = ("2013-11-25", "2017-12-11", "2021-11-08")
CYCLE_LOWS: tuple[str, ...] = ("2015-01-12", "2018-12-10", "2022-11-07")

# Recurring spacing between like-for-like pivots, used to project forward.
CYCLE_SPACING_DAYS = 1428

# Rough historical cycle anatomy in days after a halving (advisory analogs).
PHASE_BOUNDS: tuple[tuple[int, str], ...] = (
    (0, "post_halving_accumulation"),      # 0-180d: chop/accumulation
    (180, "bull_expansion"),               # 180-550d: historical bull leg
    (550, "cycle_top_window"),             # 550-750d: prior tops printed here
    (750, "bear_drawdown"),                # 750-1100d: historical bear
    (1100, "pre_halving_accumulation"),    # 1100d+: basing into next halving
)

PHASE_LABELS: dict[str, str] = {
    "post_halving_accumulation": "Post-halving accumulation",
    "bull_expansion": "Bull expansion",
    "cycle_top_window": "Cycle top window",
    "bear_drawdown": "Bear drawdown",
    "pre_halving_accumulation": "Pre-halving accumulation",
}

_DAYS_PER_MONTH = 30.437

# A top is only "confirmed" once price has fallen this far from it and the high
# is old enough to not be ordinary chop. Keeps the live leg honest between the
# hardcoded pivot list and whatever the market actually did.
TOP_CONFIRM_DRAWDOWN_PCT = 20.0
TOP_CONFIRM_MIN_AGE_DAYS = 30


@dataclass
class CyclePivot:
    """A macro turning point — realized or projected forward by cycle spacing."""

    date: str
    kind: str          # top | low
    projected: bool
    price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CycleSegment:
    """A phase leg between two pivots (low→top expansion, top→low drawdown)."""

    kind: str          # expansion | drawdown
    start_date: str
    end_date: str
    projected: bool = False
    in_progress: bool = False
    start_price: float | None = None
    end_price: float | None = None
    change_pct: float | None = None
    days: int = 0

    @property
    def months(self) -> int:
        return int(round(self.days / _DAYS_PER_MONTH))

    def label(self) -> str:
        """Screenshot-style caption: '+1,942.56%' over '18 bars' (monthly bars)."""
        if self.change_pct is None:
            return f"{self.months} bars"
        return f"{self.change_pct:+,.2f}%\n{self.months} bars"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["months"] = self.months
        data["label"] = self.label()
        return data


def _as_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def current_phase(as_of: date | None = None) -> tuple[str, int]:
    """(phase_label, days_since_last_halving) for the current cycle position."""
    today = as_of or datetime.now(timezone.utc).date()
    last_halving = max(
        (date.fromisoformat(h) for h in HALVINGS if date.fromisoformat(h) <= today),
        default=date.fromisoformat(HALVINGS[0]),
    )
    days_since = (today - last_halving).days
    label = PHASE_BOUNDS[0][1]
    for threshold, phase in PHASE_BOUNDS:
        if days_since >= threshold:
            label = phase
    return label, days_since


def projected_pivots(*, count: int = 2) -> list[CyclePivot]:
    """Carry the most recent realized top/low forward by the cycle spacing."""
    out: list[CyclePivot] = []
    for kind, realized in (("top", CYCLE_TOPS), ("low", CYCLE_LOWS)):
        anchor = _as_date(realized[-1])
        for step in range(1, count + 1):
            out.append(
                CyclePivot(
                    date=(anchor + timedelta(days=CYCLE_SPACING_DAYS * step)).isoformat(),
                    kind=kind,
                    projected=True,
                )
            )
    return sorted(out, key=lambda p: p.date)


def all_pivots(*, projected_count: int = 2) -> list[CyclePivot]:
    realized = [CyclePivot(date=d, kind="top", projected=False) for d in CYCLE_TOPS]
    realized += [CyclePivot(date=d, kind="low", projected=False) for d in CYCLE_LOWS]
    realized.sort(key=lambda p: p.date)
    return realized + projected_pivots(count=projected_count)


def _price_index(bars: list[dict[str, Any]]) -> list[tuple[date, float]]:
    series: list[tuple[date, float]] = []
    for bar in bars:
        try:
            series.append((_as_date(str(bar["ts"])), float(bar["close"])))
        except (KeyError, TypeError, ValueError):
            continue
    series.sort(key=lambda row: row[0])
    return series


def _price_on(series: list[tuple[date, float]], when: date) -> float | None:
    """Close on `when`, or the nearest earlier close (pivot dates can be gaps)."""
    if not series or when < series[0][0]:
        return None
    best: float | None = None
    for day, close in series:
        if day > when:
            break
        best = close
    return best


def _segment(
    series: list[tuple[date, float]],
    start: date,
    end: date,
    kind: str,
    *,
    projected: bool = False,
    in_progress: bool = False,
) -> CycleSegment:
    start_price = _price_on(series, start)
    end_price = _price_on(series, end)
    change = None
    if start_price and end_price and start_price > 0 and not projected:
        change = (end_price - start_price) / start_price * 100.0
    return CycleSegment(
        kind=kind,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        projected=projected,
        in_progress=in_progress,
        start_price=round(start_price, 2) if start_price else None,
        end_price=round(end_price, 2) if end_price else None,
        change_pct=round(change, 2) if change is not None else None,
        days=(end - start).days,
    )


def detect_top(
    series: list[tuple[date, float]],
    *,
    after: date,
    as_of: date,
    min_drawdown_pct: float = TOP_CONFIRM_DRAWDOWN_PCT,
    min_age_days: int = TOP_CONFIRM_MIN_AGE_DAYS,
) -> tuple[date, float] | None:
    """The high since `after`, but only once price has confirmed it as a top.

    The realized pivot list stops at the last cycle everyone agrees on. When a
    new high prints and then breaks down, this promotes it to a top from data
    so the live leg is not still labelled an expansion during a drawdown.
    """
    window = [(day, close) for day, close in series if after <= day <= as_of]
    if len(window) < 2:
        return None
    top_day, top_price = max(window, key=lambda row: row[1])
    if top_price <= 0 or (as_of - top_day).days < min_age_days:
        return None
    spot = window[-1][1]
    if (spot - top_price) / top_price * 100.0 > -min_drawdown_pct:
        return None
    return top_day, top_price


def build_segments(
    bars: list[dict[str, Any]],
    *,
    as_of: date | None = None,
    projected_count: int = 2,
) -> list[CycleSegment]:
    """Phase legs across realized pivots, the live leg, then projected legs."""
    today = as_of or datetime.now(timezone.utc).date()
    series = _price_index(bars)

    realized = sorted(
        [CyclePivot(date=d, kind="top", projected=False) for d in CYCLE_TOPS]
        + [CyclePivot(date=d, kind="low", projected=False) for d in CYCLE_LOWS],
        key=lambda p: p.date,
    )
    realized = [p for p in realized if _as_date(p.date) <= today]

    segments: list[CycleSegment] = []
    for left, right in zip(realized, realized[1:]):
        # A leg that ends on a top is expansion; one that ends on a low is drawdown.
        kind = "expansion" if right.kind == "top" else "drawdown"
        segments.append(
            _segment(series, _as_date(left.date), _as_date(right.date), kind)
        )

    if realized:
        last = realized[-1]
        live_start = _as_date(last.date)
        live_kind = "expansion" if last.kind == "low" else "drawdown"
        confirmed = (
            detect_top(series, after=live_start, as_of=today)
            if live_kind == "expansion"
            else None
        )
        if confirmed is not None:
            top_day, _ = confirmed
            segments.append(_segment(series, live_start, top_day, "expansion"))
            segments.append(
                _segment(series, top_day, today, "drawdown", in_progress=True)
            )
        else:
            segments.append(
                _segment(series, live_start, today, live_kind, in_progress=True)
            )

    upcoming = [p for p in projected_pivots(count=projected_count) if _as_date(p.date) > today]
    cursor = today
    for pivot in upcoming:
        kind = "expansion" if pivot.kind == "top" else "drawdown"
        segments.append(
            _segment(series, cursor, _as_date(pivot.date), kind, projected=True)
        )
        cursor = _as_date(pivot.date)

    return segments


def next_pivot(as_of: date | None = None) -> CyclePivot | None:
    today = as_of or datetime.now(timezone.utc).date()
    upcoming = [p for p in projected_pivots() if _as_date(p.date) > today]
    return upcoming[0] if upcoming else None


def cycle_position(
    bars: list[dict[str, Any]],
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Where the cycle clock stands now — the numbers the hub and thesis share."""
    today = as_of or datetime.now(timezone.utc).date()
    phase, days_since_halving = current_phase(today)
    series = _price_index(bars)

    last_halving = max(
        (date.fromisoformat(h) for h in HALVINGS if date.fromisoformat(h) <= today),
        default=date.fromisoformat(HALVINGS[0]),
    )
    nxt = next_pivot(today)

    last_realized = max(
        (_as_date(d) for d in (*CYCLE_TOPS, *CYCLE_LOWS) if _as_date(d) <= today),
        default=None,
    )
    confirmed_top = (
        detect_top(series, after=last_realized, as_of=today) if last_realized else None
    )

    ath_price: float | None = None
    ath_date: str | None = None
    for day, close in series:
        if ath_price is None or close > ath_price:
            ath_price, ath_date = close, day.isoformat()

    spot = series[-1][1] if series else None
    drawdown_pct = None
    if spot and ath_price and ath_price > 0:
        drawdown_pct = round((spot - ath_price) / ath_price * 100.0, 2)

    return {
        "as_of": today.isoformat(),
        "phase": phase,
        "phase_label": PHASE_LABELS.get(phase, phase.replace("_", " ")),
        "last_halving": last_halving.isoformat(),
        "days_since_halving": days_since_halving,
        "months_since_halving": int(round(days_since_halving / _DAYS_PER_MONTH)),
        "next_halving_est": NEXT_HALVING_EST,
        "days_to_next_halving": (_as_date(NEXT_HALVING_EST) - today).days,
        "cycle_progress_pct": round(
            min(days_since_halving / CYCLE_SPACING_DAYS * 100.0, 100.0), 1
        ),
        "confirmed_top": (
            {
                "date": confirmed_top[0].isoformat(),
                "price": round(confirmed_top[1], 2),
                "kind": "top",
                "projected": False,
                "detected": True,
            }
            if confirmed_top
            else None
        ),
        "next_projected_pivot": nxt.to_dict() if nxt else None,
        "days_to_next_pivot": (_as_date(nxt.date) - today).days if nxt else None,
        "spot": round(spot, 2) if spot else None,
        "ath_price": round(ath_price, 2) if ath_price else None,
        "ath_date": ath_date,
        "drawdown_from_ath_pct": drawdown_pct,
        "ath_broken_before_halving": bool(
            ath_date and _as_date(ath_date) < last_halving
        ),
    }
