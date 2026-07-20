"""Hourly ETH price/volume z-score spike detection and Telegram broadcast."""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import bot_config
import config
import notify
import research

logger = logging.getLogger(__name__)

MetricName = Literal["price", "volume"]

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS zmove_state (
    product_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    last_fire_ts TEXT NOT NULL,
    last_bar_ts TEXT,
    last_z REAL,
    PRIMARY KEY (product_id, metric)
);
"""


@dataclass(frozen=True)
class ZMoveSignal:
    product_id: str
    metric: MetricName
    z: float
    bar_ts: str
    value: float
    mean: float
    std: float
    pct_move: float | None = None  # price only
    volume_multiple: float | None = None  # volume only


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_STATE_SCHEMA)
        conn.commit()


def _z_score(value: float, series: list[float]) -> tuple[float, float, float] | None:
    """Return (z, mean, std) for value vs series; None if std is zero/insufficient."""
    if len(series) < 2:
        return None
    mean = sum(series) / len(series)
    var = sum((x - mean) ** 2 for x in series) / (len(series) - 1)
    std = math.sqrt(var)
    # Flat series → undefined z; ignore microscopic float noise as "no vol".
    if std <= 1e-12 or not math.isfinite(std):
        return None
    z = (value - mean) / std
    if not math.isfinite(z):
        return None
    return z, mean, std


def compute_price_z(
    closes: list[float],
    lookback: int,
) -> tuple[float, float, float, float] | None:
    """
    Hour-to-hour return z for the latest bar.
    Returns (z, latest_return, mean, std) or None.
    """
    if len(closes) < lookback + 2:
        return None
    returns: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev == 0:
            continue
        returns.append((closes[i] - prev) / prev)
    if len(returns) < lookback + 1:
        return None
    latest = returns[-1]
    window = returns[-(lookback + 1) : -1]
    scored = _z_score(latest, window)
    if scored is None:
        return None
    z, mean, std = scored
    return z, latest, mean, std


def compute_volume_z(
    volumes: list[float],
    lookback: int,
) -> tuple[float, float, float, float] | None:
    """
    Volume z for the latest bar vs prior lookback volumes.
    Returns (z, latest_vol, mean, std) or None.
    """
    if len(volumes) < lookback + 1:
        return None
    latest = volumes[-1]
    window = volumes[-(lookback + 1) : -1]
    scored = _z_score(latest, window)
    if scored is None:
        return None
    z, mean, std = scored
    return z, latest, mean, std


def _last_fire_ts(product_id: str, metric: MetricName) -> str | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT last_fire_ts FROM zmove_state
            WHERE product_id = ? AND metric = ?
            """,
            (product_id, metric),
        ).fetchone()
    return str(row["last_fire_ts"]) if row else None


def _record_fire(
    product_id: str,
    metric: MetricName,
    bar_ts: str,
    z: float,
) -> None:
    init_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO zmove_state (product_id, metric, last_fire_ts, last_bar_ts, last_z)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(product_id, metric) DO UPDATE SET
                last_fire_ts = excluded.last_fire_ts,
                last_bar_ts = excluded.last_bar_ts,
                last_z = excluded.last_z
            """,
            (product_id, metric, now, bar_ts, z),
        )
        conn.commit()


def _cooldown_active(product_id: str, metric: MetricName, cooldown_sec: int) -> bool:
    last = _last_fire_ts(product_id, metric)
    if not last:
        return False
    try:
        fired = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - fired).total_seconds()
    return age < cooldown_sec


def evaluate_bars(
    bars: list[dict[str, Any]],
    *,
    product_id: str,
    lookback: int,
    threshold: float,
) -> list[ZMoveSignal]:
    """Return signals for the latest closed bar when |z| >= threshold."""
    if len(bars) < lookback + 2:
        return []
    closes = [float(b["close"]) for b in bars]
    volumes = [float(b["volume"]) for b in bars]
    bar_ts = str(bars[-1]["ts"])
    signals: list[ZMoveSignal] = []

    price = compute_price_z(closes, lookback)
    if price is not None:
        z, ret, mean, std = price
        if abs(z) >= threshold:
            signals.append(
                ZMoveSignal(
                    product_id=product_id,
                    metric="price",
                    z=z,
                    bar_ts=bar_ts,
                    value=ret,
                    mean=mean,
                    std=std,
                    pct_move=ret * 100.0,
                )
            )

    vol = compute_volume_z(volumes, lookback)
    if vol is not None:
        z, latest_vol, mean, std = vol
        if abs(z) >= threshold:
            multiple = (latest_vol / mean) if mean > 0 else None
            signals.append(
                ZMoveSignal(
                    product_id=product_id,
                    metric="volume",
                    z=z,
                    bar_ts=bar_ts,
                    value=latest_vol,
                    mean=mean,
                    std=std,
                    volume_multiple=multiple,
                )
            )

    return signals


def format_signal_message(signal: ZMoveSignal) -> str:
    label = bot_config.product_label(signal.product_id)
    direction = "up" if signal.z > 0 else "down"
    if signal.metric == "price":
        pct = signal.pct_move if signal.pct_move is not None else 0.0
        detail = f"Hourly return {pct:+.2f}% ({direction})"
    else:
        mult = signal.volume_multiple
        mult_s = f"{mult:.2f}× mean" if mult is not None else "elevated"
        detail = f"Hourly volume {mult_s} ({direction} vs mean)"
    return (
        f"Z-MOVE — {label} {signal.metric.upper()}\n"
        f"z = {signal.z:+.2f} (|z| threshold {bot_config.ZMOVE_THRESHOLD})\n"
        f"{detail}\n"
        f"Bar: {signal.bar_ts}\n"
        f"Lookback: {bot_config.ZMOVE_LOOKBACK_H}h\n"
        f"(alert only — not a trade signal)"
    )


def run_zmove_scan() -> list[ZMoveSignal]:
    """Fetch H1 bars, evaluate z-moves, broadcast new fires past cooldown."""
    if not bot_config.ZMOVE_ENABLED:
        return []

    product_id = bot_config.ZMOVE_PRODUCT_ID
    lookback = int(bot_config.ZMOVE_LOOKBACK_H)
    threshold = float(bot_config.ZMOVE_THRESHOLD)
    cooldown = int(bot_config.ZMOVE_COOLDOWN_SEC)
    need = lookback + 8

    bars = research.fetch_h1_bars(need, product_id=product_id)
    # Drop forming hour if present: use closed bars only (all API bars are closed).
    signals = evaluate_bars(
        bars,
        product_id=product_id,
        lookback=lookback,
        threshold=threshold,
    )
    fired: list[ZMoveSignal] = []
    for signal in signals:
        if _cooldown_active(product_id, signal.metric, cooldown):
            logger.info(
                "Z-Move %s %s suppressed (cooldown) z=%.2f",
                product_id,
                signal.metric,
                signal.z,
            )
            continue
        text = format_signal_message(signal)
        try:
            notify.broadcast_plain_text(text)
        except Exception:
            logger.exception("Z-Move broadcast failed for %s %s", product_id, signal.metric)
            continue
        _record_fire(product_id, signal.metric, signal.bar_ts, signal.z)
        fired.append(signal)
        logger.info(
            "Z-Move fired %s %s z=%.2f bar=%s",
            product_id,
            signal.metric,
            signal.z,
            signal.bar_ts,
        )
    return fired
