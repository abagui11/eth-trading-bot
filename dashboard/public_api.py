"""Public API for the eva.finance marketing site — /api/public/*.

Two endpoints, both anonymous:

* ``GET /api/public/strategies`` — aggregates + series for the four books,
  read straight from the SQLite ledgers behind a short in-process TTL cache.
  Read-only by construction: this module never calls the yield proxy
  (``get_yield_payload`` fetches upstream and *writes* a NAV snapshot) and
  opens every side database in ``mode=ro``.
* ``POST /api/public/beta`` — beta signup. The ``beta_signups`` table in
  ledger.db is the source of truth; the Resend notification to the operators
  is fire-and-forget and a failed email never fails the signup.

In production Caddy serves the marketing site at eva.finance and proxies its
``/api/*`` to this process, so requests arrive same-origin. The CORS entry in
``create_app`` exists only for local ``astro dev`` against a local dashboard.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import bot_config
import config
import live_ledger

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/public")

_STRATEGIES_TTL_SEC = 600.0
_TRADE_ACTIONS = ("spot_buy", "spot_sell", "deriv_buy", "deriv_sell")

# Kalshi multi-bot comparison epoch — mirrors kalshi_bridge.experiment_epoch.
_KALSHI_EPOCH_DAY = "2026-09-08"

# Per-strategy "current rules" epochs for the two site cards. Each lane is
# charted from the day its present ruleset/sizing went live; earlier periods
# are disclosed in copy, never blended into the chart.
_KALSHI_STRATEGIES = {
    # site key -> (bot_id, epoch the current rules went live)
    "reversal": ("eva_streak", "2026-09-14"),
    "wick": ("eva_wick", "2026-09-17"),
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------------------------------------------------------------------------
# beta signups
# ---------------------------------------------------------------------------

_BETA_SCHEMA = """
CREATE TABLE IF NOT EXISTS beta_signups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    name TEXT,
    note TEXT,
    ip TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beta_signups_email ON beta_signups(email);
"""

# naive in-process rate limit: per-IP submission timestamps
_RATE_WINDOW_SEC = 3600.0
_RATE_MAX = 5
_rate_hits: dict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()


class BetaSignupBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    name: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=1000)
    # Honeypot — rendered off-screen on the form; humans leave it empty.
    website: str = ""


def init_db() -> None:
    with sqlite3.connect(config.LEDGER_DB) as conn:
        conn.executescript(_BETA_SCHEMA)


def _client_ip(request: Request) -> str:
    # Caddy terminates TLS and proxies locally; the original address rides
    # X-Forwarded-For. Fall back to the socket peer for direct hits.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        hits = _rate_hits[ip]
        hits[:] = [t for t in hits if now - t < _RATE_WINDOW_SEC]
        if len(hits) >= _RATE_MAX:
            return True
        hits.append(now)
    return False


def _notify_signup(email: str, name: str | None, note: str | None) -> None:
    """Email the operators about a new signup. Never raises.

    Sent one recipient per call on purpose. Resend rejects the *whole* request
    when any recipient is undeliverable — while ALERT_EMAIL_FROM is still an
    unverified test sender, a single batched call to both operators 403s and
    nobody is told. Per-recipient sends degrade to "whoever is reachable gets
    it". The signup row in ledger.db remains the source of truth either way,
    so a failure here loses a notification, not a lead.
    """
    try:
        import requests

        recipients = [
            addr.strip()
            for addr in (config.BETA_SIGNUP_EMAIL_TO or "").split(",")
            if addr.strip()
        ]
        if not (config.RESEND_API_KEY and recipients):
            return
        lines = [f"Email: {email}"]
        if name:
            lines.append(f"Name: {name}")
        if note:
            lines.append(f"Note: {note}")

        for addr in recipients:
            try:
                res = requests.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
                    json={
                        "from": config.ALERT_EMAIL_FROM,
                        "to": [addr],
                        "subject": f"[Eva beta] {email}",
                        "text": "\n".join(lines),
                    },
                    timeout=10,
                )
                if res.status_code >= 300:
                    # Loud, because a silent notifier looks identical to no
                    # signups at all.
                    logger.error(
                        "Beta signup notify rejected for %s: HTTP %s %s",
                        addr, res.status_code, res.text[:300],
                    )
            except Exception:  # noqa: BLE001 — one bad recipient is not fatal
                logger.exception("Beta signup notify failed for %s", addr)
    except Exception:  # noqa: BLE001 — notification must never fail the signup
        logger.exception("Beta signup email notify failed")


@router.post("/beta")
async def beta_signup(request: Request, body: BetaSignupBody) -> dict[str, Any]:
    # Honeypot filled → almost certainly a bot. Accept silently, store nothing.
    if body.website.strip():
        return {"ok": True}

    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Invalid email address.")

    ip = _client_ip(request)
    if _rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many requests.")

    with sqlite3.connect(config.LEDGER_DB) as conn:
        conn.executescript(_BETA_SCHEMA)
        conn.execute(
            "INSERT INTO beta_signups (email, name, note, ip, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                email,
                (body.name or "").strip() or None,
                (body.note or "").strip() or None,
                ip,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )

    threading.Thread(
        target=_notify_signup,
        args=(email, body.name, body.note),
        daemon=True,
    ).start()
    return {"ok": True}


# ---------------------------------------------------------------------------
# strategies payload
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_at = 0.0
_cache_payload: dict[str, Any] | None = None


def _ro_connect(path: Path | None) -> sqlite3.Connection | None:
    if path is None or not Path(path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        logger.exception("public_api: cannot open %s read-only", path)
        return None


def _day(ts: Any) -> str:
    return str(ts or "")[:10]


def _parse_ts(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def _pct_series(series: list[list[Any]], base: float | None) -> list[list[Any]]:
    """Dollar equity series -> percent-return series (0 baseline).

    The site shows every book in relative terms; no nominal sleeve size
    reaches a card.
    """
    if not base:
        return []
    return [[d, round((v / base - 1) * 100, 3)] for d, v in series]


def _hq_median_hold(closed: list[dict[str, Any]]) -> float | None:
    holds = []
    for r in closed:
        o, c = _parse_ts(r["opened_at"]), _parse_ts(r["closed_at"])
        if o and c:
            holds.append((c - o).total_seconds() / 3600)
    med = _median(holds)
    return round(med, 1) if med is not None else None


def _hq_trades_per_week(closed: list[dict[str, Any]]) -> float | None:
    if not closed:
        return None
    first = min(_day(r["opened_at"]) for r in closed)
    last = max(_day(r["closed_at"]) for r in closed)
    span_days = max((date.fromisoformat(last) - date.fromisoformat(first)).days, 1)
    return round(len(closed) / (span_days / 7), 1)


def _hq_and_abstention(conn: sqlite3.Connection) -> dict[str, Any]:
    sleeve = float(bot_config.LIVE_HQ_EQUITY_USD)
    closed = [dict(r) for r in conn.execute(
        "SELECT realized_pnl_usd, pnl_usd, opened_at, closed_at FROM live_trades"
        " WHERE source='hq' AND status='closed' ORDER BY closed_at")]
    pnls = [
        float(r["realized_pnl_usd"] if r["realized_pnl_usd"] is not None
              else r["pnl_usd"] or 0)
        for r in closed
    ]
    wins = [p for p in pnls if p > 0]
    by_day: dict[str, float] = defaultdict(float)
    for r, p in zip(closed, pnls):
        by_day[_day(r["closed_at"])] += p
    series: list[list[Any]] = []
    first_open = min((_day(r["opened_at"]) for r in closed), default=None)
    if first_open:
        series.append([first_open, round(sleeve, 2)])
    cum = 0.0
    for day in sorted(by_day):
        cum += by_day[day]
        series.append([day, round(sleeve + cum, 2)])

    paper_days: dict[str, float] = {}
    for r in conn.execute(
            "SELECT ts, equity_usd FROM paper_trades"
            " WHERE equity_usd IS NOT NULL ORDER BY ts"):
        paper_days[_day(r["ts"])] = float(r["equity_usd"])
    paper_series = [[d, round(v, 2)] for d, v in sorted(paper_days.items())]
    pos = conn.execute(
        "SELECT COUNT(*) n,"
        " SUM(CASE WHEN tps_hit >= 1 THEN 1 ELSE 0 END) tp1"
        " FROM paper_positions WHERE status='closed'").fetchone()
    state = conn.execute("SELECT starting_usd FROM paper_state").fetchone()

    rows = conn.execute(
        "SELECT action, COUNT(*) n FROM suggestions"
        " WHERE trigger_name IS NULL OR trigger_name=''"
        " GROUP BY action").fetchall()
    counts = {r["action"]: r["n"] for r in rows}
    total = sum(counts.values())
    trades = sum(n for a, n in counts.items() if a in _TRADE_ACTIONS)
    window = conn.execute("SELECT MIN(ts) FROM suggestions").fetchone()

    return {
        "abstention": {
            "cycles": total,
            "trade_actions": trades,
            "abstain_pct": (
                round((total - trades) / total * 100, 1) if total else None
            ),
            "since": _day(window[0] if window else None) or None,
        },
        "hq": {
            "live": {
                "n_closed": len(pnls),
                "win_rate_pct": (
                    round(len(wins) / len(pnls) * 100, 1) if pnls else None
                ),
                "pnl_usd": round(sum(pnls), 2),
                "pnl_pct": (
                    round(sum(pnls) / sleeve * 100, 2) if sleeve else None
                ),
                "trades_per_week": _hq_trades_per_week(closed),
                "median_hold_hours": _hq_median_hold(closed),
                "sleeve_usd": sleeve,
                "since": first_open,
                "series": series,
                "series_pct": _pct_series(series, sleeve),
            },
            "paper": {
                "n_closed": int(pos["n"] or 0) if pos else 0,
                "tp1_reached": int(pos["tp1"] or 0) if pos else 0,
                "starting_usd": float(state["starting_usd"]) if state else None,
                "equity_usd": paper_series[-1][1] if paper_series else None,
                "since": paper_series[0][0] if paper_series else None,
                "series": paper_series,
            },
        },
    }


def _intelligence_section(conn: sqlite3.Connection) -> dict[str, Any]:
    """The published market read: four-year cycle, stances, macro posture.

    Read straight off the tables rather than through ``intelligence.store`` /
    ``macro.store``, whose helpers call ``init_db()`` and would write to a
    database this endpoint opens read-only.

    The cycle block is deliberately separate from the trading books. The long
    thesis is a posture artifact — it never mints a trade card — and the site
    says so, so it must not be presented here as an input to a fill.
    """
    from intelligence.cycle_phases import HALVINGS, NEXT_HALVING_EST, current_phase

    today = datetime.now(timezone.utc).date()
    phase, days_since = current_phase(today)
    last_halving = max(
        (h for h in HALVINGS if date.fromisoformat(h) <= today),
        default=HALVINGS[0],
    )
    span = (date.fromisoformat(NEXT_HALVING_EST)
            - date.fromisoformat(last_halving)).days
    out: dict[str, Any] = {
        "cycle": {
            "phase": phase,
            "days_since_halving": days_since,
            "last_halving": last_halving,
            "next_halving_est": NEXT_HALVING_EST,
            "progress_pct": round(days_since / span * 100, 1) if span else None,
            "halvings_tracked": len(HALVINGS),
        }
    }

    try:
        row = conn.execute(
            "SELECT as_of_date, cycle_phase, thesis_json FROM intel_long_thesis"
            " ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is not None:
            thesis = json.loads(row["thesis_json"] or "{}")
            out["cycle"]["thesis"] = {
                "as_of": _day(row["as_of_date"]),
                "bias": thesis.get("bias"),
                # The prose stays on the operator's hub. Only the stated bias
                # and its confidence are published here.
                "confidence": thesis.get("confidence"),
            }
    except (sqlite3.Error, json.JSONDecodeError):
        logger.exception("public_api: long thesis read failed")

    try:
        latest = conn.execute(
            "SELECT MAX(cycle_ts) ts FROM intel_stances").fetchone()
        if latest and latest["ts"]:
            # The stance job runs twice inside one cycle_ts bucket (the trade
            # cycle is half-hourly), so take the newest read per cell rather
            # than publishing each product/timeframe twice. The bare columns
            # alongside MAX() resolve to that row in SQLite.
            rows = conn.execute(
                "SELECT product_id, timeframe, stance, confidence,"
                " MAX(created_at) AS created_at FROM intel_stances"
                " WHERE cycle_ts = ? GROUP BY product_id, timeframe",
                (latest["ts"],)).fetchall()
            out["stances"] = {
                "as_of": max((str(r["created_at"]) for r in rows),
                             default=str(latest["ts"])),
                "grid": [
                    [r["product_id"], r["timeframe"], r["stance"],
                     round(float(r["confidence"]), 2)
                     if r["confidence"] is not None else None]
                    for r in rows
                ],
            }
    except sqlite3.Error:
        logger.exception("public_api: stance read failed")

    try:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        total = conn.execute("SELECT COUNT(*) n FROM macro_events").fetchone()
        classified = conn.execute(
            "SELECT COUNT(*) n FROM macro_events WHERE status != 'ignored'"
        ).fetchone()
        active = conn.execute(
            "SELECT COUNT(*) n, MAX(severity) sev FROM macro_events"
            " WHERE status = 'active' AND (expires_at IS NULL OR expires_at > ?)",
            (now_iso,)).fetchone()
        out["macro"] = {
            "headlines_seen": int(total["n"] or 0) if total else 0,
            "classified": int(classified["n"] or 0) if classified else 0,
            "active": int(active["n"] or 0) if active else 0,
            "max_severity": int(active["sev"]) if active and active["sev"] else 0,
        }
    except sqlite3.Error:
        logger.exception("public_api: macro read failed")

    return out


def _yield_section() -> dict[str, Any]:
    # get_yield_nav_series is a pure SQLite read — never the upstream proxy.
    rows = live_ledger.get_yield_nav_series(limit=365)
    series = [
        [str(r["snapshot_date"]), round(float(r["nav_usd"]), 2)] for r in rows
    ]
    first = series[0] if series else None
    last = series[-1] if series else None
    return {
        "n_days": len(series),
        "since": first[0] if first else None,
        "nav_start_usd": first[1] if first else None,
        "nav_usd": last[1] if last else None,
        "pnl_usd": round(last[1] - first[1], 2) if first and last else None,
        "pnl_pct": (
            round((last[1] / first[1] - 1) * 100, 2)
            if first and last and first[1] else None
        ),
        "series": series,
        "series_pct": _pct_series(series, first[1]) if first else [],
    }


def _mill_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [r for r in rows if r["status"] not in (None, "open")]
    wins = [r for r in closed if float(r["pnl_pct"] or 0) > 0]
    return {
        "n_ideas": len(rows),
        "n_closed": len(closed),
        "win_rate_pct": (
            round(len(wins) / len(closed) * 100, 1) if closed else None
        ),
        "since": min((_day(r["opened_at"]) for r in rows), default=None),
        "until": max((_day(r["opened_at"]) for r in rows), default=None),
    }


def _mill_live_section(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The mill's live lane, as percent return on its sleeve since go-live."""
    sleeve = float(getattr(bot_config, "LIVE_MILL_SLEEVE_USD", 0) or 0)
    rows = [dict(r) for r in conn.execute(
        "SELECT COALESCE(realized_pnl_usd, pnl_usd, 0) AS pnl, closed_at"
        " FROM live_trades WHERE source='mill' AND status='closed'"
        " ORDER BY closed_at")]
    if not rows or not sleeve:
        return None
    by_day: dict[str, float] = defaultdict(float)
    for r in rows:
        by_day[_day(r["closed_at"])] += float(r["pnl"] or 0)
    series: list[list[Any]] = []
    cum = 0.0
    for day in sorted(by_day):
        cum += by_day[day]
        series.append([day, round(sleeve + cum, 2)])
    total = round(sum(float(r["pnl"] or 0) for r in rows), 2)
    return {
        "n_closed": len(rows),
        "pnl_usd": total,
        "pnl_pct": round(total / sleeve * 100, 2),
        "sleeve_usd": sleeve,
        "since": _day(rows[0]["closed_at"]),
        "series_pct": _pct_series(series, sleeve),
    }


def _mill_section() -> dict[str, Any] | None:
    """Mill paper book across both bracket epochs.

    The book is re-based when the bracket geometry changes: prior ideas move
    to ``paper_trades_archive`` and the live table restarts. Reporting only
    the live table would erase the whole published record the day a bracket
    ships, and reporting the union as one hit rate would blend two different
    geometries — so both epochs are returned separately and the site labels
    which number came from which.
    """
    import trade_ideas_bridge

    conn = _ro_connect(trade_ideas_bridge.ideas_db_path())
    if conn is None:
        return None
    try:
        current = [dict(r) for r in conn.execute(
            "SELECT status, pnl_pct, opened_at FROM paper_trades")]
        try:
            prior = [dict(r) for r in conn.execute(
                "SELECT status, pnl_pct, opened_at FROM paper_trades_archive")]
        except sqlite3.Error:
            prior = []  # archive table only exists after the first re-base
        epoch_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'mill_paper_epoch_start'"
        ).fetchone()
    except sqlite3.Error:
        logger.exception("public_api: mill read failed")
        return None
    finally:
        conn.close()

    every = prior + current
    daily: dict[str, int] = defaultdict(int)
    for r in every:
        daily[_day(r["opened_at"])] += 1
    days = sorted(daily)
    return {
        "n_ideas": len(every),
        "ideas_per_day": round(len(every) / max(len(days), 1), 1),
        "since": days[0] if days else None,
        "daily_counts": [[d, daily[d]] for d in days],
        "epoch_start": _day(epoch_row["value"]) if epoch_row else None,
        "prior_bracket": _mill_stats(prior),
        "current_bracket": _mill_stats(current),
    }


def _kalshi_section() -> dict[str, Any] | None:
    import kalshi_bridge

    conn = _ro_connect(kalshi_bridge.kalshi_db_path())
    if conn is None:
        return None
    try:
        pos = [dict(r) for r in conn.execute(
            "SELECT bot_id, pnl_usd, closed_at FROM paper_positions"
            " WHERE status != 'open'")]
        banks = {
            str(r["bot_id"]): float(r["starting_usd"])
            for r in conn.execute("SELECT bot_id, starting_usd FROM paper_state")
        }
    except sqlite3.Error:
        logger.exception("public_api: kalshi read failed")
        return None
    finally:
        conn.close()
    by_bot: dict[str, dict[str, Any]] = {}
    for p in pos:
        b = by_bot.setdefault(
            str(p["bot_id"]), {"closed": 0, "wins": 0, "pnl_usd": 0.0}
        )
        b["closed"] += 1
        pnl = float(p["pnl_usd"] or 0)
        b["pnl_usd"] += pnl
        if pnl > 0:
            b["wins"] += 1
    for b in by_bot.values():
        b["pnl_usd"] = round(b["pnl_usd"], 2)
    epoch_daily: dict[str, float] = defaultdict(float)
    for p in pos:
        d = _day(p["closed_at"])
        if d >= _KALSHI_EPOCH_DAY:
            epoch_daily[d] += float(p["pnl_usd"] or 0)
    series: list[list[Any]] = []
    cum = 0.0
    for d in sorted(epoch_daily):
        cum += epoch_daily[d]
        series.append([d, round(cum, 2)])
    strategies: dict[str, dict[str, Any]] = {}
    for key, (bot_id, epoch) in _KALSHI_STRATEGIES.items():
        bank = banks.get(bot_id)
        rows = [p for p in pos
                if str(p["bot_id"]) == bot_id and _day(p["closed_at"]) >= epoch]
        daily: dict[str, float] = defaultdict(float)
        wins = 0
        for p in rows:
            pnl = float(p["pnl_usd"] or 0)
            daily[_day(p["closed_at"])] += pnl
            if pnl > 0:
                wins += 1
        cum_series: list[list[Any]] = []
        run = 0.0
        for d in sorted(daily):
            run += daily[d]
            cum_series.append([d, round(run, 2)])
        pnl_total = round(sum(float(p["pnl_usd"] or 0) for p in rows), 2)
        strategies[key] = {
            "bot_id": bot_id,
            "epoch": epoch,
            "n_closed": len(rows),
            "win_rate_pct": round(wins / len(rows) * 100, 1) if rows else None,
            "pnl_usd": pnl_total,
            "bank_usd": bank,
            "pnl_pct": round(pnl_total / bank * 100, 2) if bank else None,
            "series_pct": (
                [[d, round(v / bank * 100, 3)] for d, v in cum_series]
                if bank else []
            ),
        }

    return {
        "bots": {k: by_bot[k] for k in sorted(by_bot)},
        "epoch": _KALSHI_EPOCH_DAY,
        "epoch_pnl_usd": series[-1][1] if series else 0.0,
        "total_pnl_usd": round(
            sum(float(p["pnl_usd"] or 0) for p in pos), 2
        ),
        "since": min((_day(p["closed_at"]) for p in pos), default=None),
        "epoch_series": series,
        "strategies": strategies,
    }


def build_strategies_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    mill_live: dict[str, Any] | None = None
    conn = _ro_connect(Path(config.LEDGER_DB))
    if conn is not None:
        try:
            payload.update(_hq_and_abstention(conn))
        except sqlite3.Error:
            logger.exception("public_api: ledger read failed")
        try:
            mill_live = _mill_live_section(conn)
        except sqlite3.Error:
            logger.exception("public_api: mill live read failed")
        try:
            payload["intelligence"] = _intelligence_section(conn)
        except Exception:  # noqa: BLE001 — a missing brain must not blank the books
            logger.exception("public_api: intelligence read failed")
        finally:
            conn.close()
    try:
        payload["yield"] = _yield_section()
    except Exception:  # noqa: BLE001
        logger.exception("public_api: yield read failed")
    mill = _mill_section()
    if mill is not None:
        if mill_live is not None:
            mill["live"] = mill_live
        payload["mill"] = mill
    kalshi = _kalshi_section()
    if kalshi is not None:
        payload["kalshi"] = kalshi
    return payload


@router.get("/strategies")
def public_strategies() -> dict[str, Any]:
    global _cache_at, _cache_payload
    now = time.monotonic()
    with _cache_lock:
        if _cache_payload is not None and now - _cache_at < _STRATEGIES_TTL_SEC:
            return _cache_payload
    payload = build_strategies_payload()
    with _cache_lock:
        _cache_at = time.monotonic()
        _cache_payload = payload
    return payload
