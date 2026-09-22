"""Score the Phase 2 conditional reads on M5 candles. Read-side only.

The metric a conditional read deserves
--------------------------------------
A scalar stance was scored on "was price higher later", which is the wrong
question for a conditional thesis. A conditional read says: *while this array
holds, price is drawn to that level*. So it resolves as a race, on the M5
path, from the bar after publication:

  resolved_target      attracting level printed before invalidation  -> hit
  resolved_invalidated invalidation printed first                    -> miss
  unresolved           neither inside the horizon                    -> excluded
                       from accuracy, counted separately

Ties inside one bar resolve **invalidation first** — the conservative choice,
matching the barrier convention used everywhere else in the evidence pack.
Invalidation is an M5 *close* through the boundary (our stated convention);
`wick_hit` records what the wick-through rule would have said, so the
convention can be swept rather than argued about.

Two things this deliberately does not do
----------------------------------------
* It does not score `bias IS NULL` reads for direction — there is no
  directional claim to score. They feed the null-calibration metric instead
  (is realized range genuinely quieter when the desk declines to call?).
* It does not compute a day-clustered CI over a handful of days. With five
  days of data there are five clusters, which cannot support one. The scorer
  reports counts and rates; inference waits for enough days to carry it, and
  `summarize` says so in its output rather than printing a false interval.

Armed reads (2026-09-22)
------------------------
A read on an untested array stores its direction as `armed_bias` and is
scored in two stages by `resolve_armed`:

  1. arming, over `ARM_WINDOW_H` from publication: the first M5 bar that
     trades into the array triggers it. If the draw prints first the read is
     `armed_ran_to_draw` (price left without the retrace — not a hit, since
     the claim was never put at risk); if neither, `armed_not_triggered`.
  2. from the trigger, the ordinary race over `horizon_h`, with the trigger
     bar itself checked for a close-through first (same tie rule).

The geometry baseline is taken from the touched edge, not the publish-time
spot, because that is where the thesis is actually entered.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import research
from intelligence import store

logger = logging.getLogger(__name__)

M5 = 300
DEFAULT_HORIZON_H = 24
ARM_WINDOW_H = 24


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def barrier_implied_hit_prob(read: dict[str, Any]) -> float | None:
    """Chance of printing the target before invalidation with no skill.

    "Target before invalidation" is uninterpretable on its own: a draw 0.5%
    away against an invalidation 2% away wins that race most of the time by
    geometry alone, exactly as a 0.3R target beats a 1R stop most of the time.
    For a driftless walk between two absorbing barriers, the probability of
    reaching the target first, *given* that one of them was reached, is the
    opposite barrier's share of the total distance (gambler's ruin). Actual
    accuracy minus the mean of this is the read's skill; the raw rate is not.

    Conditioning on "one barrier was reached" is why this is compared against
    decided reads only, and why the formula needs no horizon term.
    """
    spot = read.get("spot")
    inval = read.get("invalidation_price")
    band = _band(read.get("attracting_lo"), read.get("attracting_hi"))
    if not spot or not inval or not band:
        return None
    spot, inval = float(spot), float(inval)
    if spot <= 0:
        return None
    target = (band[0] + band[1]) / 2
    d_target = abs(target - spot)
    d_inval = abs(spot - inval)
    total = d_target + d_inval
    if total <= 0:
        return None
    return d_inval / total


def _band(lo: Any, hi: Any) -> tuple[float, float] | None:
    try:
        lo_f, hi_f = float(lo), float(hi)
    except (TypeError, ValueError):
        return None
    if lo_f <= 0 or hi_f <= 0:
        return None
    return min(lo_f, hi_f), max(lo_f, hi_f)


def resolve_read(
    read: dict[str, Any],
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    """Walk the M5 path for one read. `bars` must start after publication."""
    out = {
        "id": read.get("id"),
        "product_id": read.get("product_id"),
        "timeframe": read.get("timeframe"),
        "bias": read.get("bias"),
        "outcome": "unresolved",
        "bars_to_outcome": None,
        "wick_hit": None,
        "mfe_pct": None,
        "mae_pct": None,
    }
    band = _band(read.get("attracting_lo"), read.get("attracting_hi"))
    invalidation = read.get("invalidation_price")
    bias = read.get("bias")
    spot = read.get("spot")
    if not bars or spot in (None, 0):
        return out

    spot = float(spot)
    hi_seen, lo_seen = spot, spot
    target_bar = invalid_bar = wick_bar = None

    for i, bar in enumerate(bars):
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        hi_seen, lo_seen = max(hi_seen, high), min(lo_seen, low)

        if band and target_bar is None and low <= band[1] and high >= band[0]:
            target_bar = i
        if invalidation is not None and bias:
            inv = float(invalidation)
            through_close = close < inv if bias == "bullish" else close > inv
            through_wick = low < inv if bias == "bullish" else high > inv
            if invalid_bar is None and through_close:
                invalid_bar = i
            if wick_bar is None and through_wick:
                wick_bar = i
        if target_bar is not None or invalid_bar is not None:
            break

    out["mfe_pct"] = (hi_seen / spot - 1) * 100
    out["mae_pct"] = (lo_seen / spot - 1) * 100
    if invalid_bar is not None and (
        target_bar is None or invalid_bar <= target_bar
    ):
        # `<=` is the tie rule: same bar resolves against the thesis.
        out["outcome"] = "resolved_invalidated"
        out["bars_to_outcome"] = invalid_bar
    elif target_bar is not None:
        out["outcome"] = "resolved_target"
        out["bars_to_outcome"] = target_bar
    out["wick_hit"] = wick_bar if wick_bar is not None else None
    return out


def resolve_armed(
    read: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    arm_window_h: int = ARM_WINDOW_H,
    horizon_h: int = DEFAULT_HORIZON_H,
) -> dict[str, Any]:
    """Two-stage resolution for an armed read. `bars` start after publication."""
    armed = read.get("armed_bias")
    out = {
        "id": read.get("id"),
        "product_id": read.get("product_id"),
        "timeframe": read.get("timeframe"),
        "armed_bias": armed,
        "outcome": "armed_not_triggered",
        "trigger_bar": None,
        "bars_to_outcome": None,
        "implied_prob": None,
    }
    array = _band(read.get("repelling_lo"), read.get("repelling_hi"))
    draw = _band(read.get("attracting_lo"), read.get("attracting_hi"))
    inval = read.get("invalidation_price")
    if armed not in ("bullish", "bearish") or not array or not draw or inval is None:
        out["outcome"] = "unscoreable"
        return out

    per_hour = 3600 // M5
    trigger = None
    for i, bar in enumerate(bars[: arm_window_h * per_hour]):
        high, low = float(bar["high"]), float(bar["low"])
        if low <= array[1] and high >= array[0]:
            trigger = i
            break
        if low <= draw[1] and high >= draw[0]:
            out["outcome"] = "armed_ran_to_draw"
            out["bars_to_outcome"] = i
            return out
    if trigger is None:
        return out

    entry = array[1] if armed == "bullish" else array[0]
    staged = {**read, "bias": armed, "spot": entry}
    out["trigger_bar"] = trigger
    out["implied_prob"] = barrier_implied_hit_prob(staged)

    close = float(bars[trigger]["close"])
    inv = float(inval)
    if (close < inv) if armed == "bullish" else (close > inv):
        out["outcome"] = "resolved_invalidated"
        out["bars_to_outcome"] = trigger
        return out

    tail = bars[trigger + 1: trigger + 1 + horizon_h * per_hour]
    raced = resolve_read(staged, tail)
    out["outcome"] = raced["outcome"]
    if raced["bars_to_outcome"] is not None:
        out["bars_to_outcome"] = trigger + 1 + raced["bars_to_outcome"]
    return out


def summarize_armed(resolved: list[dict[str, Any]]) -> dict[str, Any]:
    """Trigger rate, then accuracy vs geometry on the triggered-and-decided."""
    n = len(resolved)
    counts: dict[str, int] = {}
    for r in resolved:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    triggered = [r for r in resolved if r.get("trigger_bar") is not None]
    decided = [r for r in triggered if r["outcome"] in
               ("resolved_target", "resolved_invalidated")]
    hits = sum(1 for r in decided if r["outcome"] == "resolved_target")
    implied = [r["implied_prob"] for r in decided if r.get("implied_prob") is not None]
    accuracy = hits / len(decided) if decided else None
    implied_mean = sum(implied) / len(implied) if implied else None
    days = sorted({str(r.get("created_at"))[:10] for r in resolved})
    return {
        "n_armed": n,
        "outcomes": counts,
        "trigger_rate": len(triggered) / n if n else None,
        "n_decided": len(decided),
        "accuracy": accuracy,
        "barrier_implied_accuracy": implied_mean,
        "skill_over_geometry": (
            accuracy - implied_mean
            if accuracy is not None and implied_mean is not None else None
        ),
        "inference": (
            f"NOT INFERENTIAL: {len(decided)} decided armed reads over "
            f"{len(days)} day(s)."
            if len(days) < 10 or len(decided) < 60 else
            f"{len(decided)} decided armed reads over {len(days)} days — "
            "compute a day-clustered CI before quoting."
        ),
    }


def score_window(
    *,
    hours_back: int = 120,
    horizon_h: int = DEFAULT_HORIZON_H,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve every read old enough to have had its full horizon."""
    now = now or datetime.now(timezone.utc)
    cutoff_new = now - timedelta(hours=horizon_h)
    cutoff_old = now - timedelta(hours=hours_back)

    armed_span_h = ARM_WINDOW_H + horizon_h
    cutoff_armed = now - timedelta(hours=armed_span_h)

    rows = store.read_history(limit=500)
    eligible, armed = [], []
    for r in rows:
        ts = _parse_ts(r.get("created_at"))
        if ts and cutoff_old <= ts <= cutoff_new:
            eligible.append((ts, r))
        if ts and r.get("armed_bias") and cutoff_old <= ts <= cutoff_armed:
            armed.append((ts, r))
    if not eligible:
        return {"n_eligible": 0, "note": "no reads have completed their horizon yet"}

    # One candle fetch per product covers every read in the window.
    span_start = int(min(ts for ts, _ in eligible).timestamp())
    span_end = int(max(ts for ts, _ in eligible).timestamp()) + horizon_h * 3600
    if armed:
        span_end = max(span_end, int(max(ts for ts, _ in armed).timestamp())
                       + armed_span_h * 3600)
    candles: dict[str, list[dict[str, Any]]] = {}
    for product_id in {str(r.get("product_id")) for _, r in eligible}:
        try:
            candles[product_id] = research.fetch_coinbase_candles_range(
                "M5", span_start, span_end, product_id=product_id
            )
        except Exception:
            logger.exception("Candle fetch failed for %s", product_id)
            candles[product_id] = []

    resolved = []
    for ts, read in eligible:
        product_id = str(read.get("product_id"))
        start = int(ts.timestamp())
        end = start + horizon_h * 3600
        # Strictly after publication: no bar the read could have seen.
        bars = [
            b for b in candles.get(product_id) or []
            if start < _bar_epoch(b) <= end
        ]
        resolved.append(
            resolve_read(read, bars)
            | {
                "created_at": read["created_at"],
                "implied_prob": barrier_implied_hit_prob(read),
            }
        )
    summary = summarize(resolved, eligible)

    armed_resolved = []
    for ts, read in armed:
        start = int(ts.timestamp())
        bars = [
            b for b in candles.get(str(read.get("product_id"))) or []
            if start < _bar_epoch(b) <= start + armed_span_h * 3600
        ]
        armed_resolved.append(
            resolve_armed(read, bars, horizon_h=horizon_h)
            | {"created_at": read["created_at"]}
        )
    summary["armed"] = summarize_armed(armed_resolved)
    return summary


def _bar_epoch(bar: dict[str, Any]) -> int:
    ts = _parse_ts(str(bar.get("ts")))
    return int(ts.timestamp()) if ts else 0


def summarize(
    resolved: list[dict[str, Any]],
    eligible: list[tuple[datetime, dict[str, Any]]],
) -> dict[str, Any]:
    directional = [r for r in resolved if r["bias"]]
    decided = [r for r in directional if r["outcome"] != "unresolved"]
    hits = [r for r in decided if r["outcome"] == "resolved_target"]
    null_reads = [r for r in resolved if not r["bias"]]
    days = sorted({str(r["created_at"])[:10] for r in resolved})
    reads = [r for _, r in eligible]

    def _mean_range(group: list[dict[str, Any]]) -> float | None:
        spans = [
            r["mfe_pct"] - r["mae_pct"]
            for r in group
            if r["mfe_pct"] is not None and r["mae_pct"] is not None
        ]
        return sum(spans) / len(spans) if spans else None

    # Skill = accuracy minus the geometry it was handed. Averaged over the
    # same decided reads the accuracy is computed on, or the two are not
    # comparable.
    implied = [
        r["implied_prob"] for r in decided if r.get("implied_prob") is not None
    ]
    implied_mean = sum(implied) / len(implied) if implied else None
    accuracy = (len(hits) / len(decided)) if decided else None

    out: dict[str, Any] = {
        "n_eligible": len(resolved),
        "n_days": len(days),
        "days": days,
        "n_directional": len(directional),
        "n_decided": len(decided),
        "n_unresolved": len(directional) - len(decided),
        "conditional_accuracy": accuracy,
        "barrier_implied_accuracy": implied_mean,
        "skill_over_geometry": (
            accuracy - implied_mean
            if accuracy is not None and implied_mean is not None
            else None
        ),
        "n_with_implied": len(implied),
        "n_null_bias": len(null_reads),
        "null_bias_rate": len(null_reads) / len(resolved) if resolved else None,
        "stale_invalidation_rate": (
            sum(1 for r in reads if r.get("stale_invalidation")) / len(reads)
            if reads else None
        ),
        # Null calibration: the desk declining to call should coincide with a
        # quieter tape. If these two are equal, "null" is not informative.
        "range_when_bias_pct": _mean_range(directional),
        "range_when_null_pct": _mean_range(null_reads),
        "by_timeframe": {},
    }
    for tf in sorted({r["timeframe"] for r in resolved if r["timeframe"]}):
        tf_decided = [r for r in decided if r["timeframe"] == tf]
        tf_implied = [
            r["implied_prob"] for r in tf_decided
            if r.get("implied_prob") is not None
        ]
        tf_acc = (
            sum(1 for r in tf_decided if r["outcome"] == "resolved_target")
            / len(tf_decided)
        ) if tf_decided else None
        tf_imp = sum(tf_implied) / len(tf_implied) if tf_implied else None
        out["by_timeframe"][tf] = {
            "n_decided": len(tf_decided),
            "accuracy": tf_acc,
            "barrier_implied_accuracy": tf_imp,
            "skill_over_geometry": (
                tf_acc - tf_imp
                if tf_acc is not None and tf_imp is not None else None
            ),
        }
    # The honest-uncertainty note travels with the numbers, so a small sample
    # cannot be quoted as a result by someone reading only the JSON.
    if len(days) < 10 or len(decided) < 60:
        out["inference"] = (
            f"NOT INFERENTIAL: {len(decided)} decided reads over {len(days)} "
            "day-cluster(s). Day-clustered CIs need ~10+ days; overlapping "
            "hourly reads make trade-level resampling overstate confidence. "
            "Read these as operational counts, not as an accuracy estimate."
        )
    else:
        out["inference"] = (
            f"{len(decided)} decided reads over {len(days)} days — enough for "
            "a day-clustered CI; compute it before quoting."
        )
    return out


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)
    print(json.dumps(score_window(), indent=2, default=str))
