"""Read-only bridge to the colocated Kalshi 15m bot ledger.

The Kalshi 15m bots (kalshi_15m_bot repo, /opt/kalshi-15m-bot on the VPS)
keep their books in one SQLite ledger. This module mirrors the
trade_ideas_bridge pattern: fail-soft reads over ``KALSHI_DB`` so the hub
dashboard can show bot performance without importing bot code.

Hub .env knobs:

* ``KALSHI_DB=/opt/kalshi-15m-bot/ledger.db`` — required for the tab.
* ``KALSHI_LIVE_BOTS=eva_wick`` — which bot(s) trade the real account;
  everything else shows as PAPER. Mirrors the bot repo's env of the same name.
* ``KALSHI_EXPERIMENT_EPOCH`` — comparison start line (default: the
  2026-09-08 multi-bot flip). All per-bot stats and the closed list count
  from here so live and paper books race from the same start.
* ``KALSHI_LASTMIN_DB=/opt/kalshi-15m-bot/lastmin.db`` — optional; enables
  the Eva #3 last-2-min arb logger card (logging only, no trading).

When ``KALSHI_DB`` is unset or unreadable every payload reports
``{"available": False}`` and the tab shows a mount hint instead of breaking.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Kalshi 15m products trade on ET walls; the bot's boss rules are ET-based.
_ET = timezone(timedelta(hours=-4))

_BOT_LABELS = {
    "control": "Control (conviction ICT)",
    "lottery": "Lottery / hail-mary",
    "adverse": "Adverse / wick-hunt",
    "eva_wick": "EVA wick",
    "eva_streak": "EVA reversal",
    "eva_arb": "EVA arb",
    "eva_wick_xrp": "XRP",
    "eva_wick_sol": "SOL",
    "eva_wick_hype": "HYPE",
    "eva_wick_1h_ladder": "1h ladder · 4/2/1 once per hour",
    "eva_wick_1h_flat": "1h flat · 1/1 every fire",
    "eva_wick_btc_xrp": "BTC→XRP",
    "eva_wick_btc_sol": "BTC→SOL",
    "eva_wick_btc_hype": "BTC→HYPE",
    "eva_wick_eth_xrp": "ETH→XRP",
    "eva_wick_eth_sol": "ETH→SOL",
    "eva_wick_eth_hype": "ETH→HYPE",
}

# Paper clones of the live wick rule on the altcoin 15m series. They get their
# own table rather than a row each in the main comparison, because they are
# not competing with the three sleeves — they are the same sleeve answering a
# different question (does the edge exist outside BTC/ETH?), and mixing them
# in would make the main table look like six strategies instead of three.
_ALT_BOTS: dict[str, str] = {
    "eva_wick_xrp": "KXXRP15M",
    "eva_wick_sol": "KXSOL15M",
    "eva_wick_hype": "KXHYPE15M",
}

# Hourly piggyback books: when the live wick rule fires on a 15m market,
# these buy the same side of the top-of-hour BTC/ETH threshold series at
# fixed strike rungs past spot (bot repo: eva_wick_hourly.py). Same reasoning
# as the altcoin clones — they are wick derivatives answering a different
# question (do wick fires carry to the hourly settle?), not a fourth sleeve.
_HOURLY_BOTS: dict[str, str] = {
    "eva_wick_1h_ladder": "KXBTCD · KXETHD",
    "eva_wick_1h_flat": "KXBTCD · KXETHD",
}

# Cross-asset wick books (bot repo: eva_wick_cross.py): a BTC or ETH wick
# fire that the EVA board's M15 stance agrees with buys the same side on an
# altcoin 15m market. bot_id -> (signal coin, target coin, target series).
_CROSS_BOTS: dict[str, tuple[str, str, str]] = {
    "eva_wick_btc_xrp": ("BTC", "XRP", "KXXRP15M"),
    "eva_wick_btc_sol": ("BTC", "SOL", "KXSOL15M"),
    "eva_wick_btc_hype": ("BTC", "HYPE", "KXHYPE15M"),
    "eva_wick_eth_xrp": ("ETH", "XRP", "KXXRP15M"),
    "eva_wick_eth_sol": ("ETH", "SOL", "KXSOL15M"),
    "eva_wick_eth_hype": ("ETH", "HYPE", "KXHYPE15M"),
}

# Every shadow family that stays out of the sleeves' shared feeds.
_SHADOW_BOTS: tuple[str, ...] = (*_ALT_BOTS, *_HOURLY_BOTS, *_CROSS_BOTS)

# Short grey subtitles under each bot name in the comparison table (≤4 lines).
_BOT_BLURBS = {
    "eva_streak": (
        "After ≥3 same-direction 15m candles with a sweep of the prior extreme, "
        "buy the opposite side at the open mid only when priced 45–65¢. "
        "Cash out at 2× or cut at ½; cool down after consecutive stops."
    ),
    "eva_wick": (
        "With 4–10 minutes left in the window, buy whichever side the book "
        "prices at 67–80¢ and hold to settlement. No directional signal — "
        "the band and the clock are the edge. Weekdays only. Live book "
        "since 2026-09-22."
    ),
    "eva_arb": (
        "Last 2 minutes only. If the favorite touched 90¢ then dips to 75–85¢, "
        "buy the favored side before quotes freeze and hold to settlement. "
        "Paper trading this epoch."
    ),
    "eva_wick_1h_ladder": (
        "When the live wick rule fires, buy the same side of the hourly "
        "BTC/ETH threshold market at the 1st/2nd/3rd strikes past spot "
        "(4/2/1 contracts), at most once per hour. Blind fixed rungs — "
        "this book exists to price the distances before anyone tunes them."
    ),
    "eva_wick_1h_flat": (
        "Every wick fire, whatever the clock: 1 contract at each of the "
        "1st and 2nd hourly strikes past spot. The always-on sibling of "
        "the ladder book."
    ),
    "eva_wick_btc_xrp": (
        "When the wick rule fires on BTC and the EVA board's M15 stance "
        "points with it (≥0.55 conf), buy the same side of the XRP 15m "
        "market at the ask. Tests whether a confirmed major-coin move "
        "carries across assets."
    ),
    "eva_wick_btc_sol": (
        "BTC wick fire + EVA M15 agreement → same side on the SOL 15m "
        "market at the ask, same quarter-hour settle."
    ),
    "eva_wick_btc_hype": (
        "BTC wick fire + EVA M15 agreement → same side on the HYPE 15m "
        "market at the ask, same quarter-hour settle."
    ),
    "eva_wick_eth_xrp": (
        "ETH wick fire + EVA M15 agreement → same side on the XRP 15m "
        "market at the ask, same quarter-hour settle."
    ),
    "eva_wick_eth_sol": (
        "ETH wick fire + EVA M15 agreement → same side on the SOL 15m "
        "market at the ask, same quarter-hour settle."
    ),
    "eva_wick_eth_hype": (
        "ETH wick fire + EVA M15 agreement → same side on the HYPE 15m "
        "market at the ask, same quarter-hour settle."
    ),
}

# Bots always shown in the comparison, even before their first trade.
_EXPERIMENT_BOTS = ("eva_streak", "eva_wick", "eva_arb")
# Retired books stay in the ledger for analysis but off the dashboard.
_RETIRED_BOTS = ("eva_wick_fade_v1",)

# Multi-bot experiment flip: eva_streak went live (mid entry), eva_wick moved
# to paper with the double-down rule. Comparison starts here.
_EXPERIMENT_EPOCH_DEFAULT = "2026-09-08T18:00:00Z"

# The altcoin books were switched on at this instant (kalshi-bot restarted on
# the VPS with the three ids in ENABLED_BOTS). Pinned rather than derived from
# the first trade: a forward test's record has to include the windows it chose
# not to trade, and "running since the first entry" would quietly restate the
# start date every time the book is reset.
_ALT_EPOCH_DEFAULT = "2026-09-30T16:47:19Z"

# Switch-on instant of the hourly piggyback books (kalshi-bot restart with
# eva_wick_1h_* in ENABLED_BOTS). Pinned for the same reason as the altcoin
# epoch: the forward record includes the fires the books chose to skip.
_HOURLY_EPOCH_DEFAULT = "2026-09-30T20:07:51Z"

# Switch-on instant of the cross-asset wick books; same pinning logic.
# Set at the deploy restart that added the six eva_wick_btc_*/eth_* ids.
_CROSS_EPOCH_DEFAULT = "2026-09-30T20:52:36Z"


def experiment_epoch() -> str:
    return (
        os.getenv("KALSHI_EXPERIMENT_EPOCH") or _EXPERIMENT_EPOCH_DEFAULT
    ).strip()


def alt_epoch() -> str:
    return (os.getenv("KALSHI_ALT_EPOCH") or _ALT_EPOCH_DEFAULT).strip()


def hourly_epoch() -> str:
    return (os.getenv("KALSHI_HOURLY_EPOCH") or _HOURLY_EPOCH_DEFAULT).strip()


def cross_epoch() -> str:
    return (os.getenv("KALSHI_CROSS_EPOCH") or _CROSS_EPOCH_DEFAULT).strip()


def live_bots() -> tuple[str, ...]:
    raw = (os.getenv("KALSHI_LIVE_BOTS") or "eva_wick").strip()
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def kalshi_db_path() -> Path | None:
    raw = (os.getenv("KALSHI_DB") or "").strip()
    return Path(raw) if raw else None


def lastmin_db_path() -> Path | None:
    raw = (os.getenv("KALSHI_LASTMIN_DB") or "").strip()
    return Path(raw) if raw else None


def enabled() -> bool:
    path = kalshi_db_path()
    return path is not None and path.exists()


def _connect(path: Path | None) -> sqlite3.Connection | None:
    if path is None or not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        logger.exception("Kalshi ledger unavailable at %s", path)
        return None


def _fmt_ts(value: Any) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:16]
    return dt.astimezone(_ET).strftime("%m/%d %H:%M")


def _position_row(row: sqlite3.Row) -> dict[str, Any]:
    entry = float(row["entry_cents"] or 0)
    pnl = row["pnl_usd"]
    return {
        "id": int(row["id"]),
        "bot_id": str(row["bot_id"] or "control"),
        "opened_at": _fmt_ts(row["opened_at"]),
        "closed_at": _fmt_ts(row["closed_at"]),
        "market_ticker": str(row["market_ticker"] or ""),
        "product_id": str(row["product_id"] or ""),
        "side": str(row["side"] or ""),
        "contracts": int(row["contracts"] or 0),
        "entry_cents": entry,
        "cost_usd": entry / 100.0 * int(row["contracts"] or 0),
        "result": str(row["result"] or ""),
        "pnl_usd": float(pnl) if pnl is not None else None,
        "rationale": str(row["rationale"] or ""),
    }


def lastmin_payload(max_windows: int = 400) -> dict[str, Any] | None:
    """Eva #3 arb logger evidence: dip setups seen vs how they settled.

    A "dip setup" = the favored side touched >=90c inside the final window
    and later printed back inside 75-85c. No trading — this only answers
    "how often would that buy have settled in the money?".
    """
    conn = _connect(lastmin_db_path())
    if conn is None:
        return None
    try:
        results = conn.execute(
            "SELECT ticker, result FROM results ORDER BY settled_ts DESC LIMIT ?",
            (int(max_windows),),
        ).fetchall()
        windows_total = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        quotes_total = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        dips = 0
        dip_wins = 0
        for r in results:
            rows = conn.execute(
                "SELECT yes_mid FROM quotes WHERE ticker = ? AND yes_mid IS NOT NULL"
                " ORDER BY id",
                (str(r["ticker"]),),
            ).fetchall()
            mids = [float(q["yes_mid"]) for q in rows]
            if not mids:
                continue
            for favored, touch, lo, hi in (
                ("yes", lambda m: m >= 90.0, 75.0, 85.0),
                ("no", lambda m: m <= 10.0, 15.0, 25.0),
            ):
                touched = False
                dipped = False
                for m in mids:
                    if touch(m):
                        touched = True
                    elif touched and lo <= m <= hi:
                        dipped = True
                        break
                if dipped:
                    dips += 1
                    if str(r["result"]) == favored:
                        dip_wins += 1
                    break
    except sqlite3.Error:
        logger.exception("lastmin db query failed")
        return None
    finally:
        conn.close()
    return {
        "windows": int(windows_total or 0),
        "quotes": int(quotes_total or 0),
        "dips": dips,
        "dip_wins": dip_wins,
        "dip_win_rate": (dip_wins / dips) if dips else None,
    }


def performance_payload(limit: int = 15) -> dict[str, Any] | None:
    """Kalshi multi-bot snapshot for the hub tab; None when not mounted."""
    conn = _connect(kalshi_db_path())
    if conn is None:
        return None
    epoch = experiment_epoch()
    alt_start = alt_epoch()
    hourly_start = hourly_epoch()
    cross_start = cross_epoch()
    try:
        states = conn.execute(
            "SELECT bot_id, starting_usd, cash_usd, realized_pnl_usd"
            " FROM paper_state ORDER BY bot_id"
        ).fetchall()
        open_rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status = 'open'"
            " ORDER BY opened_at DESC LIMIT 40"
        ).fetchall()
        # The shadow books (altcoin clones + hourly piggybacks) settle on
        # the same clocks as the live book, so leaving them in this window
        # would push the real trades off the feed within the hour. Their
        # records live in their own tables.
        alt_ph = ",".join("?" * len(_ALT_BOTS))
        hourly_ph = ",".join("?" * len(_HOURLY_BOTS))
        cross_ph = ",".join("?" * len(_CROSS_BOTS))
        shadow_ph = ",".join("?" * len(_SHADOW_BOTS))
        closed_rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status != 'open'"
            f" AND opened_at >= ? AND bot_id NOT IN ({shadow_ph})"
            " ORDER BY closed_at DESC LIMIT ?",
            (epoch, *_SHADOW_BOTS, max(1, min(int(limit), 100))),
        ).fetchall()
        alt_first = conn.execute(
            f"SELECT MIN(opened_at) FROM paper_positions"
            f" WHERE bot_id IN ({alt_ph})",
            tuple(_ALT_BOTS),
        ).fetchone()[0]
        hourly_first = conn.execute(
            f"SELECT MIN(opened_at) FROM paper_positions"
            f" WHERE bot_id IN ({hourly_ph})",
            tuple(_HOURLY_BOTS),
        ).fetchone()[0]
        cross_first = conn.execute(
            f"SELECT MIN(opened_at) FROM paper_positions"
            f" WHERE bot_id IN ({cross_ph})",
            tuple(_CROSS_BOTS),
        ).fetchone()[0]
        # Each family counts from its own start. The sleeves race from
        # 09-08, the altcoin clones from their 09-30 switch-on, the hourly
        # piggybacks from theirs; one shared cut-off would either hide
        # sleeve history or let a pre-epoch shadow row into a forward test.
        agg = conn.execute(
            "SELECT bot_id,"
            "  COUNT(*) AS closed,"
            "  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,"
            "  SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) AS losses,"
            "  SUM(COALESCE(pnl_usd, 0)) AS pnl_usd,"
            "  SUM(CASE WHEN result = 'flat' THEN 1 ELSE 0 END) AS early_exits"
            " FROM paper_positions"
            " WHERE status != 'open'"
            f"   AND opened_at >= (CASE WHEN bot_id IN ({alt_ph}) THEN ?"
            f"                          WHEN bot_id IN ({hourly_ph}) THEN ?"
            f"                          WHEN bot_id IN ({cross_ph}) THEN ?"
            "                           ELSE ? END)"
            " GROUP BY bot_id",
            (*_ALT_BOTS, alt_start, *_HOURLY_BOTS, hourly_start,
             *_CROSS_BOTS, cross_start, epoch),
        ).fetchall()
        hidden_n = conn.execute(
            "SELECT COUNT(*) FROM paper_positions"
            " WHERE status != 'open' AND opened_at < ?"
            f" AND bot_id NOT IN ({shadow_ph})",
            (epoch, *_SHADOW_BOTS),
        ).fetchone()[0]
    except sqlite3.Error:
        logger.exception("Kalshi ledger query failed")
        return None
    finally:
        conn.close()

    live_set = set(live_bots())
    open_list = [_position_row(r) for r in open_rows]
    closed_list = [_position_row(r) for r in closed_rows]
    for p in open_list + closed_list:
        p["mode"] = "live" if p["bot_id"] in live_set else "paper"
    agg_by_bot = {str(r["bot_id"]): r for r in agg}
    open_cost_by_bot: dict[str, float] = {}
    for pos in open_list:
        open_cost_by_bot[pos["bot_id"]] = (
            open_cost_by_bot.get(pos["bot_id"], 0.0) + pos["cost_usd"]
        )

    bots: list[dict[str, Any]] = []
    alt_bots: list[dict[str, Any]] = []
    hourly_bots: list[dict[str, Any]] = []
    cross_bots: list[dict[str, Any]] = []
    for st in states:
        bot_id = str(st["bot_id"])
        a = agg_by_bot.get(bot_id)
        closed = int(a["closed"]) if a else 0
        wins = int(a["wins"] or 0) if a else 0
        losses = int(a["losses"] or 0) if a else 0
        n_open = sum(1 for p in open_list if p["bot_id"] == bot_id)
        if bot_id in _RETIRED_BOTS:
            continue
        is_alt = bot_id in _ALT_BOTS
        is_hourly = bot_id in _HOURLY_BOTS
        is_cross = bot_id in _CROSS_BOTS
        # Idle leftover books (old control/lottery rows) stay off the tab.
        if (
            closed == 0
            and n_open == 0
            and bot_id not in _EXPERIMENT_BOTS
            and not is_alt
            and not is_hourly
            and not is_cross
        ):
            continue
        decided = wins + losses
        cash = float(st["cash_usd"] or 0)
        is_shadow = is_alt or is_hourly or is_cross
        row = {
            "bot_id": bot_id,
            "label": _BOT_LABELS.get(bot_id, bot_id),
            "blurb": _BOT_BLURBS.get(bot_id, ""),
            # Hourly and cross books are paper by construction in the bot repo
            # (bot_config.PAPER_ONLY_BOTS), not by env, so their badge cannot
            # go stale if someone edits KALSHI_LIVE_BOTS. Altcoin clones can
            # be released there (ALT_WICK_LIVE_RELEASED — SOL since
            # 2026-10-01), so for them the env whitelist decides.
            "mode": (
                "live"
                if bot_id in live_set and not (is_hourly or is_cross)
                else "paper"
            ),
            "starting_usd": float(st["starting_usd"] or 0),
            "cash_usd": cash,
            "equity_usd": cash + open_cost_by_bot.get(bot_id, 0.0),
            "realized_pnl_usd": float(st["realized_pnl_usd"] or 0),
            "epoch_pnl_usd": float(a["pnl_usd"] or 0) if a else 0.0,
            "open": n_open,
            "closed": closed,
            "wins": wins,
            "losses": losses,
            "early_exits": int(a["early_exits"] or 0) if a else 0,
            "win_rate": (wins / decided) if decided else None,
        }
        if is_alt:
            row["series"] = _ALT_BOTS[bot_id]
            alt_bots.append(row)
        elif is_hourly:
            row["series"] = _HOURLY_BOTS[bot_id]
            hourly_bots.append(row)
        elif is_cross:
            signal, coin, series = _CROSS_BOTS[bot_id]
            row["series"] = series
            row["signal"] = signal
            row["coin"] = coin
            cross_bots.append(row)
        else:
            bots.append(row)
    # Live book first, then paper books alphabetically.
    bots.sort(key=lambda b: (b["mode"] != "live", b["bot_id"]))
    alt_order = list(_ALT_BOTS)
    alt_bots.sort(key=lambda b: alt_order.index(b["bot_id"]))
    hourly_order = list(_HOURLY_BOTS)
    hourly_bots.sort(key=lambda b: hourly_order.index(b["bot_id"]))
    cross_order = list(_CROSS_BOTS)
    cross_bots.sort(key=lambda b: cross_order.index(b["bot_id"]))

    live_list = [b for b in bots if b["mode"] == "live"]
    live_wins = sum(b["wins"] for b in live_list)
    live_losses = sum(b["losses"] for b in live_list)
    decided = live_wins + live_losses
    totals = {
        "label": " + ".join(b["label"] for b in live_list) or "(no live bot)",
        "starting_usd": sum(b["starting_usd"] for b in live_list),
        "equity_usd": sum(b["equity_usd"] for b in live_list),
        "realized_pnl_usd": sum(b["realized_pnl_usd"] for b in live_list),
        "epoch_pnl_usd": sum(b["epoch_pnl_usd"] for b in live_list),
        "open": sum(b["open"] for b in live_list),
        "closed": sum(b["closed"] for b in live_list),
        "wins": live_wins,
        "losses": live_losses,
        "win_rate": (live_wins / decided) if decided else None,
    }
    return {
        "available": True,
        "experiment_epoch": epoch,
        "experiment_epoch_label": _fmt_ts(epoch) + " ET",
        "live_bots": sorted(live_set),
        "hidden_closed": int(hidden_n or 0),
        "totals": totals,
        "bots": bots,
        "open": [p for p in open_list if p["bot_id"] not in _SHADOW_BOTS],
        "closed": closed_list,
        "altcoins": {
            "available": bool(alt_bots),
            "epoch": alt_start,
            "epoch_label": _fmt_ts(alt_start) + " ET",
            # When the books actually first traded, which is not when they
            # were switched on — the gap is part of the record.
            "first_trade": alt_first,
            "first_trade_label": _fmt_ts(alt_first) + " ET" if alt_first else None,
            "bots": alt_bots,
            "open": [p for p in open_list if p["bot_id"] in _ALT_BOTS],
        },
        "hourly": {
            "available": bool(hourly_bots),
            "epoch": hourly_start,
            "epoch_label": _fmt_ts(hourly_start) + " ET",
            "first_trade": hourly_first,
            "first_trade_label": (
                _fmt_ts(hourly_first) + " ET" if hourly_first else None
            ),
            "bots": hourly_bots,
            "open": [p for p in open_list if p["bot_id"] in _HOURLY_BOTS],
        },
        "cross": {
            "available": bool(cross_bots),
            "epoch": cross_start,
            "epoch_label": _fmt_ts(cross_start) + " ET",
            "first_trade": cross_first,
            "first_trade_label": (
                _fmt_ts(cross_first) + " ET" if cross_first else None
            ),
            "bots": cross_bots,
            "open": [p for p in open_list if p["bot_id"] in _CROSS_BOTS],
        },
        "lastmin": lastmin_payload(),
    }
