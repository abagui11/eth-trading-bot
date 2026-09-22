"""SQLite persistence for intelligence artifacts: stances, funding, long thesis."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intel_stances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts TEXT NOT NULL,
    product_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    stance TEXT NOT NULL,
    confidence REAL,
    rationale TEXT,
    source TEXT NOT NULL DEFAULT 'llm',
    created_at TEXT NOT NULL,
    det_stance TEXT,
    llm_stance TEXT,
    override_kind TEXT,
    override_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_intel_stances_cycle ON intel_stances(cycle_ts);
CREATE INDEX IF NOT EXISTS idx_intel_stances_product ON intel_stances(product_id, timeframe);

-- INTEL_BOARD_PLAN Phase 2. Shadow artifact: written every cycle, consumed by
-- nobody until the nightly scorer clears its pre-registered bar. Prices here
-- always come from a detector, never from the model (see conditional.py).
CREATE TABLE IF NOT EXISTS intel_reads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts TEXT NOT NULL,
    product_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bias TEXT,
    attracting_kind TEXT,
    attracting_lo REAL,
    attracting_hi REAL,
    repelling_kind TEXT,
    repelling_side TEXT,
    repelling_lo REAL,
    repelling_hi REAL,
    repelling_state TEXT,
    location TEXT,
    invalidation_price REAL,
    invalidation_trigger TEXT,
    spot REAL,
    rationale TEXT,
    source TEXT NOT NULL DEFAULT 'llm',
    stale_invalidation INTEGER NOT NULL DEFAULT 0,
    dropped_reason TEXT,
    dedup_key TEXT,
    armed_bias TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_reads_cycle ON intel_reads(cycle_ts);
CREATE INDEX IF NOT EXISTS idx_intel_reads_product ON intel_reads(product_id, timeframe);

-- Per-consumer counterfactual: what each consumer DID, next to what it would
-- have done on the conditional read. Written by hub-side consumers only; the
-- mill and the Kalshi bot record theirs in their own books (different hosts),
-- and analysis joins the three.
CREATE TABLE IF NOT EXISTS intel_read_counterfactuals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    consumer TEXT NOT NULL,
    ref TEXT,
    product_id TEXT NOT NULL,
    timeframe TEXT,
    actual TEXT,
    counterfactual TEXT,
    agreed INTEGER,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_read_cf_consumer
    ON intel_read_counterfactuals(consumer, created_at);

CREATE TABLE IF NOT EXISTS intel_medium (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts TEXT NOT NULL,
    summary TEXT NOT NULL,
    btc_eth_note TEXT,
    funding_note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_medium_cycle ON intel_medium(cycle_ts);

CREATE TABLE IF NOT EXISTS funding_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    funding_ts TEXT NOT NULL,
    rate REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (product_id, funding_ts)
);

CREATE INDEX IF NOT EXISTS idx_funding_rates_product ON funding_rates(product_id, funding_ts);

CREATE TABLE IF NOT EXISTS funding_regimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    regime TEXT NOT NULL,
    streak_periods INTEGER NOT NULL DEFAULT 0,
    as_of_ts TEXT NOT NULL,
    detail_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_funding_regimes_product ON funding_regimes(product_id, created_at);

CREATE TABLE IF NOT EXISTS funding_health (
    product_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    source TEXT,
    last_ok_at TEXT,
    last_ok_funding_ts TEXT,
    last_error TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intel_long_thesis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of_date TEXT NOT NULL,
    cycle_phase TEXT NOT NULL,
    thesis_json TEXT NOT NULL,
    chart_path TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intel_long_thesis_date ON intel_long_thesis(as_of_date);

CREATE TABLE IF NOT EXISTS zmove_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    z REAL NOT NULL,
    bar_ts TEXT NOT NULL,
    detail_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_zmove_events_created ON zmove_events(created_at);
"""

VALID_STANCES = ("bullish", "neutral", "bearish")
STANCE_TIMEFRAMES = ("H4", "H1", "M15")

# How the published stance departed from the deterministic score. The three
# kinds are separated because the recorded book shows them costing differently
# and they imply different fixes.
OVERRIDE_MUTED = "muted_to_neutral"
OVERRIDE_INVENTED = "invented_direction"
OVERRIDE_FLIPPED = "sign_flip"


def classify_override(det_stance: str | None, llm_stance: str | None) -> str | None:
    """Classify what the model *attempted*, not what was published.

    Keyed on the LLM's stance rather than the published one so the record
    survives a Phase 1 revert: with `STANCE_PUBLISH_DETERMINISTIC` on, the
    published stance equals the deterministic score and the attempt would
    otherwise vanish from the row.
    """
    if not det_stance or not llm_stance or det_stance == llm_stance:
        return None
    if det_stance in ("bullish", "bearish") and llm_stance == "neutral":
        return OVERRIDE_MUTED
    if det_stance == "neutral" and llm_stance in ("bullish", "bearish"):
        return OVERRIDE_INVENTED
    return OVERRIDE_FLIPPED


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.LEDGER_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_stance_columns(conn: sqlite3.Connection) -> None:
    """Counterfactual columns on books created before INTEL_BOARD_PLAN Phase 0."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(intel_stances)")}
    for name in ("det_stance", "llm_stance", "override_kind", "override_reason"):
        if name not in cols:
            conn.execute(f"ALTER TABLE intel_stances ADD COLUMN {name} TEXT")
    read_cols = {row[1] for row in conn.execute("PRAGMA table_info(intel_reads)")}
    for name in ("dedup_key", "armed_bias"):
        if read_cols and name not in read_cols:
            conn.execute(f"ALTER TABLE intel_reads ADD COLUMN {name} TEXT")


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _ensure_stance_columns(conn)
        conn.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Stances

def insert_stances(
    cycle_ts: str,
    stances: list[dict[str, Any]],
    *,
    source: str = "llm",
) -> int:
    """Persist one hourly batch of per-product/per-timeframe stances.

    Three stances go on every row and all of them are always recorded:
    `det_stance` (the deterministic score), `llm_stance` (what the model
    wanted) and `stance` (what was published). That makes every policy
    measurable whatever the Phase 1 flags are set to, so the Phase 0 ledger
    and the Phase 1 intervention can run at the same time.
    """
    init_db()
    created = _now_iso()
    count = 0
    with _connect() as conn:
        for s in stances:
            stance = str(s.get("stance") or "neutral").lower()
            if stance not in VALID_STANCES:
                stance = "neutral"

            def _norm(value: Any) -> str | None:
                value = str(value).lower() if value else None
                return value if value in VALID_STANCES else None

            det = _norm(s.get("det_stance"))
            # Absent when nothing was reverted: the model's stance *is* the
            # published one, so record it rather than leaving a hole.
            llm = _norm(s.get("llm_stance")) or stance
            conn.execute(
                """
                INSERT INTO intel_stances
                    (cycle_ts, product_id, timeframe, stance, confidence,
                     rationale, source, created_at, det_stance, llm_stance,
                     override_kind, override_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_ts,
                    str(s["product_id"]),
                    str(s["timeframe"]).upper(),
                    stance,
                    float(s["confidence"]) if s.get("confidence") is not None else None,
                    str(s.get("rationale") or ""),
                    source,
                    created,
                    det,
                    llm,
                    classify_override(det, llm),
                    str(s.get("override_reason") or "") or None,
                ),
            )
            count += 1
        conn.commit()
    return count


def latest_stances() -> list[dict[str, Any]]:
    """Most recent stance batch (one row per product/timeframe for latest cycle_ts).

    ``cycle_ts`` is bucketed to the hour, so a restart or a manual re-run inside
    the same hour appends a second batch under the same key. Collapse those to
    the newest row per product/timeframe so callers never see duplicates.
    """
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT cycle_ts FROM intel_stances ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return []
        cycle_ts = str(row["cycle_ts"])
        rows = conn.execute(
            """
            SELECT * FROM intel_stances WHERE id IN (
                SELECT MAX(id) FROM intel_stances
                WHERE cycle_ts = ?
                GROUP BY product_id, timeframe
            )
            ORDER BY product_id, timeframe
            """,
            (cycle_ts,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Conditional reads (Phase 2 shadow artifact)

_READ_FIELDS = (
    "product_id", "timeframe", "bias", "attracting_kind", "attracting_lo",
    "attracting_hi", "repelling_kind", "repelling_side", "repelling_lo",
    "repelling_hi", "repelling_state", "location", "invalidation_price",
    "invalidation_trigger", "spot", "rationale", "stale_invalidation",
    "dropped_reason", "dedup_key", "armed_bias",
)


def latest_read_keys() -> dict[tuple[str, str], str | None]:
    """{(product, timeframe): dedup_key of the newest stored read}."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT product_id, timeframe, dedup_key FROM intel_reads
            WHERE id IN (
                SELECT MAX(id) FROM intel_reads GROUP BY product_id, timeframe
            )
            """
        ).fetchall()
    return {(str(r["product_id"]), str(r["timeframe"])): r["dedup_key"]
            for r in rows}


def insert_reads(
    cycle_ts: str,
    reads: list[dict[str, Any]],
    *,
    source: str = "llm",
) -> int:
    init_db()
    created = _now_iso()
    columns = ", ".join(_READ_FIELDS)
    placeholders = ", ".join("?" for _ in _READ_FIELDS)
    count = 0
    with _connect() as conn:
        for r in reads:
            values = [r.get(f) for f in _READ_FIELDS]
            # SQLite has no bool; the scorer aggregates this column.
            values[_READ_FIELDS.index("stale_invalidation")] = int(
                bool(r.get("stale_invalidation"))
            )
            conn.execute(
                f"INSERT INTO intel_reads (cycle_ts, {columns}, source, created_at) "
                f"VALUES (?, {placeholders}, ?, ?)",
                [cycle_ts, *values, source, created],
            )
            count += 1
        conn.commit()
    return count


def latest_reads() -> list[dict[str, Any]]:
    """Newest read per (product, timeframe) from the most recent cycle."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT cycle_ts FROM intel_reads ORDER BY created_at DESC, id DESC "
            "LIMIT 1"
        ).fetchone()
        if row is None:
            return []
        rows = conn.execute(
            """
            SELECT * FROM intel_reads WHERE id IN (
                SELECT MAX(id) FROM intel_reads WHERE cycle_ts = ?
                GROUP BY product_id, timeframe
            )
            ORDER BY product_id, timeframe
            """,
            (str(row["cycle_ts"]),),
        ).fetchall()
    return [dict(r) for r in rows]


def read_history(*, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM intel_reads ORDER BY created_at DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def insert_read_counterfactual(
    consumer: str,
    *,
    product_id: str,
    actual: str | None,
    counterfactual: str | None,
    ref: str | None = None,
    timeframe: str | None = None,
    note: str | None = None,
) -> None:
    """Record one consumer decision beside its conditional-read counterfactual.

    `agreed` is derived, not passed: a caller computing it would eventually
    disagree with the column it wrote. None on either side means there is
    nothing to compare — "the desk declined to call" is not a disagreement.
    """
    init_db()
    agreed = (
        None if actual is None or counterfactual is None
        else int(actual == counterfactual)
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO intel_read_counterfactuals
                (consumer, ref, product_id, timeframe, actual, counterfactual,
                 agreed, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (consumer, ref, product_id, timeframe, actual, counterfactual,
             agreed, note, _now_iso()),
        )
        conn.commit()


def read_counterfactuals(
    *, consumer: str | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        if consumer:
            rows = conn.execute(
                "SELECT * FROM intel_read_counterfactuals WHERE consumer = ? "
                "ORDER BY created_at DESC LIMIT ?", (consumer, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM intel_read_counterfactuals "
                "ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def stance_history(*, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM intel_stances
            ORDER BY created_at DESC, product_id, timeframe
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Medium summary

def insert_medium_summary(
    cycle_ts: str,
    summary: str,
    *,
    btc_eth_note: str | None = None,
    funding_note: str | None = None,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO intel_medium
                (cycle_ts, summary, btc_eth_note, funding_note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (cycle_ts, summary, btc_eth_note, funding_note, _now_iso()),
        )
        conn.commit()


def latest_medium_summary() -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM intel_medium ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Funding

def upsert_funding_rates(
    product_id: str,
    rates: list[dict[str, Any]],
) -> int:
    """Insert funding prints, skipping duplicates. rates: [{ts, rate}]."""
    init_db()
    fetched = _now_iso()
    inserted = 0
    with _connect() as conn:
        for r in rates:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO funding_rates
                    (product_id, funding_ts, rate, fetched_at)
                VALUES (?, ?, ?, ?)
                """,
                (product_id, str(r["ts"]), float(r["rate"]), fetched),
            )
            inserted += int(cur.rowcount or 0)
        conn.commit()
    return inserted


def funding_series(product_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Newest-last funding prints for a product."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT funding_ts AS ts, rate FROM funding_rates
            WHERE product_id = ?
            ORDER BY funding_ts DESC LIMIT ?
            """,
            (product_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def insert_funding_regime(
    product_id: str,
    regime: str,
    *,
    streak_periods: int,
    as_of_ts: str,
    detail: dict[str, Any] | None = None,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO funding_regimes
                (product_id, regime, streak_periods, as_of_ts, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                product_id,
                regime,
                streak_periods,
                as_of_ts,
                json.dumps(detail or {}),
                _now_iso(),
            ),
        )
        conn.commit()


def latest_funding_regime(product_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM funding_regimes WHERE product_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (product_id,),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    if data.get("detail_json"):
        try:
            data["detail"] = json.loads(str(data["detail_json"]))
        except json.JSONDecodeError:
            data["detail"] = {}
    data.pop("detail_json", None)
    return data


def record_funding_health(
    product_id: str,
    *,
    status: str,
    source: str | None = None,
    funding_ts: str | None = None,
    error: str | None = None,
) -> None:
    """Record the outcome of a funding fetch. 'ok' refreshes the success marks;
    'error' keeps the previous success marks so staleness stays measurable."""
    init_db()
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO funding_health
                (product_id, status, source, last_ok_at, last_ok_funding_ts,
                 last_error, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                status = excluded.status,
                source = COALESCE(excluded.source, funding_health.source),
                last_ok_at = COALESCE(excluded.last_ok_at, funding_health.last_ok_at),
                last_ok_funding_ts = COALESCE(
                    excluded.last_ok_funding_ts, funding_health.last_ok_funding_ts
                ),
                last_error = excluded.last_error,
                checked_at = excluded.checked_at
            """,
            (
                product_id,
                status,
                source,
                now if status == "ok" else None,
                funding_ts if status == "ok" else None,
                error,
                now,
            ),
        )
        conn.commit()


def funding_health(product_id: str) -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM funding_health WHERE product_id = ?",
            (product_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def funding_regime_history(
    product_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM funding_regimes WHERE product_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (product_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        data = dict(r)
        data.pop("detail_json", None)
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Long thesis

def insert_long_thesis(
    as_of_date: str,
    cycle_phase: str,
    thesis: dict[str, Any],
    *,
    chart_path: str | None = None,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO intel_long_thesis
                (as_of_date, cycle_phase, thesis_json, chart_path, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (as_of_date, cycle_phase, json.dumps(thesis), chart_path, _now_iso()),
        )
        conn.commit()


def latest_long_thesis() -> dict[str, Any] | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM intel_long_thesis ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    try:
        data["thesis"] = json.loads(str(data.pop("thesis_json")))
    except json.JSONDecodeError:
        data["thesis"] = {}
    return data


# ---------------------------------------------------------------------------
# Z-move events (persisted signal feed for API consumers)

def insert_zmove_event(
    product_id: str,
    metric: str,
    z: float,
    bar_ts: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO zmove_events
                (product_id, metric, z, bar_ts, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (product_id, metric, z, bar_ts, json.dumps(detail or {}), _now_iso()),
        )
        conn.commit()


def recent_zmove_events(*, limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM zmove_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        data = dict(r)
        if data.get("detail_json"):
            try:
                data["detail"] = json.loads(str(data["detail_json"]))
            except json.JSONDecodeError:
                data["detail"] = {}
        data.pop("detail_json", None)
        out.append(data)
    return out
