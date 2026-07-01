"""Persist market-context snapshots and audit verdicts for the monitor agent."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import config
from models import Suggestion
from patterns.htf_structure import HTFZone
from patterns.key_levels import KeyLevel
from patterns.market_context import MarketContext
from patterns.order_block import OrderBlock
from patterns.range_24h import Range24h
from patterns.setup_state import SetupState
from patterns.sfp import SFPEvent
from patterns.zone_resolver import ZoneSnapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cycle_id TEXT NOT NULL UNIQUE,
    spot REAL,
    snapshot_json TEXT NOT NULL,
    suggestion_json TEXT NOT NULL,
    marked_chart_paths TEXT,
    market_context_summary TEXT
);

CREATE TABLE IF NOT EXISTS audit_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cycle_id TEXT,
    source TEXT NOT NULL,
    user_id INTEGER,
    deterministic_json TEXT NOT NULL,
    llm_json TEXT,
    has_issues INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    reply TEXT NOT NULL,
    cycle_id TEXT,
    verdict_id INTEGER
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def _htf_zone_to_dict(zone: HTFZone) -> dict[str, Any]:
    return {
        "zone_type": zone.zone_type,
        "direction": zone.direction,
        "low": zone.low,
        "high": zone.high,
        "start_ts": zone.start_ts,
        "end_ts": zone.end_ts,
        "mitigated": zone.mitigated,
        "msb_ts": zone.msb_ts,
    }


def _htf_zone_from_dict(data: dict[str, Any]) -> HTFZone:
    return HTFZone(
        zone_type=data["zone_type"],  # type: ignore[arg-type]
        direction=data["direction"],  # type: ignore[arg-type]
        low=float(data["low"]),
        high=float(data["high"]),
        start_ts=str(data["start_ts"]),
        end_ts=data.get("end_ts"),
        mitigated=bool(data.get("mitigated", False)),
        msb_ts=str(data.get("msb_ts", "")),
    )


def _order_block_to_dict(ob: OrderBlock) -> dict[str, Any]:
    return asdict(ob)


def _order_block_from_dict(data: dict[str, Any]) -> OrderBlock:
    return OrderBlock(
        direction=data["direction"],  # type: ignore[arg-type]
        low=float(data["low"]),
        high=float(data["high"]),
        start_ts=str(data["start_ts"]),
        end_ts=str(data["end_ts"]),
        displacement_ts=str(data["displacement_ts"]),
    )


def _sfp_to_dict(event: SFPEvent) -> dict[str, Any]:
    return asdict(event)


def _sfp_from_dict(data: dict[str, Any]) -> SFPEvent:
    return SFPEvent(
        ts=str(data["ts"]),
        bar_idx=int(data["bar_idx"]),
        timeframe=str(data["timeframe"]),
        direction=data["direction"],  # type: ignore[arg-type]
        swept_level=float(data["swept_level"]),
        sweep_depth_pct=float(data["sweep_depth_pct"]),
        aligns_prior_swing=bool(data["aligns_prior_swing"]),
        volume_spike=bool(data["volume_spike"]),
        outcome_a=data["outcome_a"],  # type: ignore[arg-type]
        outcome_b=data.get("outcome_b"),
        outcome_c=data.get("outcome_c"),
    )


def _key_level_to_dict(level: KeyLevel) -> dict[str, Any]:
    return asdict(level)


def _key_level_from_dict(data: dict[str, Any]) -> KeyLevel:
    return KeyLevel(
        price=float(data["price"]),
        label=str(data["label"]),
        color=str(data["color"]),
    )


def _range_to_dict(range_24h: Range24h | None) -> dict[str, Any] | None:
    if range_24h is None:
        return None
    return asdict(range_24h)


def _range_from_dict(data: dict[str, Any] | None) -> Range24h | None:
    if not data:
        return None
    return Range24h(
        high=float(data["high"]),
        low=float(data["low"]),
        mid=float(data["mid"]),
        width_pct=float(data["width_pct"]),
        is_ranging=bool(data["is_ranging"]),
        bars_in_range=int(data["bars_in_range"]),
        start_ts=str(data["start_ts"]),
        end_ts=str(data["end_ts"]),
    )


def _zone_snapshot_to_dict(zone_snap: ZoneSnapshot | None) -> dict[str, Any] | None:
    if zone_snap is None:
        return None
    return {
        "spot": zone_snap.spot,
        "zones_containing_price": [_htf_zone_to_dict(z) for z in zone_snap.zones_containing_price],
        "primary_bullish": (
            _htf_zone_to_dict(zone_snap.primary_bullish) if zone_snap.primary_bullish else None
        ),
        "primary_bearish": (
            _htf_zone_to_dict(zone_snap.primary_bearish) if zone_snap.primary_bearish else None
        ),
        "nearest_bearish_above": (
            _htf_zone_to_dict(zone_snap.nearest_bearish_above)
            if zone_snap.nearest_bearish_above
            else None
        ),
        "nearest_bullish_below": (
            _htf_zone_to_dict(zone_snap.nearest_bullish_below)
            if zone_snap.nearest_bullish_below
            else None
        ),
        "bearish_retest_low": zone_snap.bearish_retest_low,
        "bearish_retest_high": zone_snap.bearish_retest_high,
    }


def _zone_snapshot_from_dict(data: dict[str, Any] | None) -> ZoneSnapshot | None:
    if not data:
        return None
    return ZoneSnapshot(
        spot=float(data["spot"]),
        zones_containing_price=[
            _htf_zone_from_dict(z) for z in data.get("zones_containing_price", [])
        ],
        primary_bullish=(
            _htf_zone_from_dict(data["primary_bullish"]) if data.get("primary_bullish") else None
        ),
        primary_bearish=(
            _htf_zone_from_dict(data["primary_bearish"]) if data.get("primary_bearish") else None
        ),
        nearest_bearish_above=(
            _htf_zone_from_dict(data["nearest_bearish_above"])
            if data.get("nearest_bearish_above")
            else None
        ),
        nearest_bullish_below=(
            _htf_zone_from_dict(data["nearest_bullish_below"])
            if data.get("nearest_bullish_below")
            else None
        ),
        bearish_retest_low=data.get("bearish_retest_low"),
        bearish_retest_high=data.get("bearish_retest_high"),
    )


def market_context_to_dict(
    ctx: MarketContext,
    *,
    live_invalidated_sfps: list[SFPEvent] | None = None,
) -> dict[str, Any]:
    invalidated = live_invalidated_sfps if live_invalidated_sfps is not None else ctx.live_invalidated_sfps
    return {
        "range_24h": _range_to_dict(ctx.range_24h),
        "is_ranging": ctx.is_ranging,
        "range_break": ctx.range_break,
        "spot": ctx.spot,
        "zone_snapshot": _zone_snapshot_to_dict(ctx.zone_snapshot),
        "setup_state": ctx.setup_state.to_dict() if ctx.setup_state else None,
        "alerts": list(ctx.alerts),
        "h12_sfps": [_sfp_to_dict(e) for e in ctx.h12_sfps],
        "h1_sfps": [_sfp_to_dict(e) for e in ctx.h1_sfps],
        "live_invalidated_sfps": [
            _sfp_to_dict(e) for e in invalidated
        ],
        "order_blocks": [_order_block_to_dict(ob) for ob in ctx.order_blocks],
        "htf_zones": [_htf_zone_to_dict(z) for z in ctx.htf_zones],
        "key_levels_near": [_key_level_to_dict(lv) for lv in ctx.key_levels_near],
        "setup_tags": list(ctx.setup_tags),
        "summary_text": ctx.summary_text,
    }


def market_context_from_dict(data: dict[str, Any]) -> MarketContext:
    setup_raw = data.get("setup_state")
    return MarketContext(
        range_24h=_range_from_dict(data.get("range_24h")),
        is_ranging=bool(data.get("is_ranging", False)),
        range_break=data.get("range_break"),
        spot=float(data.get("spot", 0.0)),
        zone_snapshot=_zone_snapshot_from_dict(data.get("zone_snapshot")),
        setup_state=SetupState.from_dict(setup_raw) if setup_raw else None,
        alerts=list(data.get("alerts", [])),
        h12_sfps=[_sfp_from_dict(e) for e in data.get("h12_sfps", [])],
        h1_sfps=[_sfp_from_dict(e) for e in data.get("h1_sfps", [])],
        live_invalidated_sfps=[
            _sfp_from_dict(e) for e in data.get("live_invalidated_sfps", [])
        ],
        order_blocks=[_order_block_from_dict(ob) for ob in data.get("order_blocks", [])],
        htf_zones=[_htf_zone_from_dict(z) for z in data.get("htf_zones", [])],
        key_levels_near=[_key_level_from_dict(lv) for lv in data.get("key_levels_near", [])],
        setup_tags=list(data.get("setup_tags", [])),
        summary_text=str(data.get("summary_text", "")),
    )


def live_invalidated_from_snapshot(data: dict[str, Any]) -> list[SFPEvent]:
    return [_sfp_from_dict(e) for e in data.get("live_invalidated_sfps", [])]


def suggestion_to_dict(suggestion: Suggestion) -> dict[str, Any]:
    return {
        "action": suggestion.action,
        "size": suggestion.size,
        "entry": suggestion.entry,
        "stop_loss": suggestion.stop_loss,
        "take_profits": list(suggestion.take_profits),
        "risk_reward": suggestion.risk_reward,
        "rationale": suggestion.rationale,
        "order_block": suggestion.order_block,
        "decision_charts": list(suggestion.decision_charts),
        "structure_chart": suggestion.structure_chart,
        "entry_chart": suggestion.entry_chart,
    }


def suggestion_from_dict(data: dict[str, Any]) -> Suggestion:
    return Suggestion.from_dict(data)


def save_snapshot(
    cycle_id: str,
    market_context: MarketContext,
    suggestion: Suggestion,
    marked_chart_paths: dict[str, str],
    *,
    live_invalidated_sfps: list[SFPEvent] | None = None,
    ts: str | None = None,
) -> int:
    """Persist ground-truth snapshot for a cycle."""
    init_db()
    row_ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot = market_context_to_dict(
        market_context,
        live_invalidated_sfps=live_invalidated_sfps,
    )
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO audit_snapshots (
                ts, cycle_id, spot, snapshot_json, suggestion_json,
                marked_chart_paths, market_context_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cycle_id) DO UPDATE SET
                ts = excluded.ts,
                spot = excluded.spot,
                snapshot_json = excluded.snapshot_json,
                suggestion_json = excluded.suggestion_json,
                marked_chart_paths = excluded.marked_chart_paths,
                market_context_summary = excluded.market_context_summary
            """,
            (
                row_ts,
                cycle_id,
                market_context.spot,
                json.dumps(snapshot),
                json.dumps(suggestion_to_dict(suggestion)),
                json.dumps(marked_chart_paths),
                market_context.summary_text,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def get_snapshot(cycle_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM audit_snapshots WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["snapshot"] = json.loads(record["snapshot_json"])
    record["suggestion"] = json.loads(record["suggestion_json"])
    record["marked_chart_paths"] = json.loads(record["marked_chart_paths"] or "{}")
    return record


def get_latest_snapshot() -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM audit_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    record = dict(row)
    record["snapshot"] = json.loads(record["snapshot_json"])
    record["suggestion"] = json.loads(record["suggestion_json"])
    record["marked_chart_paths"] = json.loads(record["marked_chart_paths"] or "{}")
    return record


def save_verdict(
    *,
    source: str,
    deterministic_findings: list[dict[str, Any]],
    llm_findings: list[dict[str, Any]] | None = None,
    cycle_id: str | None = None,
    user_id: int | None = None,
    has_issues: bool = False,
    ts: str | None = None,
) -> int:
    init_db()
    row_ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO audit_verdicts (
                ts, cycle_id, source, user_id, deterministic_json, llm_json, has_issues
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_ts,
                cycle_id,
                source,
                user_id,
                json.dumps(deterministic_findings),
                json.dumps(llm_findings or []),
                1 if has_issues else 0,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def log_chat_audit(
    user_id: int,
    question: str,
    reply: str,
    *,
    cycle_id: str | None = None,
    verdict_id: int | None = None,
    ts: str | None = None,
) -> int:
    init_db()
    row_ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO chat_audits (ts, user_id, question, reply, cycle_id, verdict_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (row_ts, user_id, question, reply, cycle_id, verdict_id),
        )
        conn.commit()
        return int(cursor.lastrowid)
