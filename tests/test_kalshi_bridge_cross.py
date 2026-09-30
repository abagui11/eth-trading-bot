"""Cross-asset wick books get their own section of the Kalshi payload.

Same separation logic as the other shadow families: six books that settle
on the sleeves' 15m clock would bury the live feed, and they are scored
from their own switch-on epoch, not the sleeves'.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kalshi_bridge

EPOCH = "2026-09-08T18:00:00Z"
ALT_EPOCH = "2026-09-30T09:00:00Z"
HOURLY_EPOCH = "2026-09-30T20:00:00Z"
CROSS_EPOCH = "2026-09-30T21:00:00Z"

_POSITION_COLS = (
    "id, bot_id, opened_at, closed_at, series, market_ticker, product_id, "
    "side, contracts, entry_cents, expiry_ts, rationale, status, result, "
    "payout_usd, pnl_usd"
)


def _mk_ledger(path: Path, states, positions) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE paper_state (bot_id TEXT, starting_usd REAL, "
        "cash_usd REAL, realized_pnl_usd REAL)"
    )
    conn.execute(
        "CREATE TABLE paper_positions (id INTEGER PRIMARY KEY, bot_id TEXT, "
        "opened_at TEXT, closed_at TEXT, series TEXT, market_ticker TEXT, "
        "product_id TEXT, side TEXT, contracts INTEGER, entry_cents REAL, "
        "expiry_ts TEXT, rationale TEXT, status TEXT, result TEXT, "
        "payout_usd REAL, pnl_usd REAL)"
    )
    conn.executemany("INSERT INTO paper_state VALUES (?,?,?,?)", states)
    conn.executemany(
        f"INSERT INTO paper_positions ({_POSITION_COLS}) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        positions,
    )
    conn.commit()
    conn.close()


def _closed(pid, bot, ticker, opened, closed, pnl, result="yes"):
    return (pid, bot, opened, closed, "KXXRP15M", ticker, "XRP", "NO", 2,
            70.0, None, "r", "closed", result, 2.0, pnl)


def _open(pid, bot, ticker, opened):
    return (pid, bot, opened, None, "KXSOL15M", ticker, "SOL", "NO", 2, 70.0,
            None, "r", "open", None, None, None)


class CrossSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "ledger.db"
        states = [
            ("eva_wick", 225.0, 200.0, -25.0),
            ("eva_streak", 225.0, 225.0, 0.0),
            ("eva_arb", 225.0, 225.0, 0.0),
        ] + [(b, 225.0, 225.0, 0.0) for b in kalshi_bridge._CROSS_BOTS]
        _mk_ledger(
            self.db,
            states=states,
            positions=[
                _closed(1, "eva_wick", "KXBTC15M-A",
                        "2026-09-29T10:00:00Z", "2026-09-29T10:15:00Z", 2.0),
                _closed(2, "eva_wick_btc_xrp", "KXXRP15M-A",
                        "2026-09-30T21:20:00Z", "2026-09-30T21:30:00Z", 1.5),
                _closed(3, "eva_wick_btc_xrp", "KXXRP15M-B",
                        "2026-09-30T21:35:00Z", "2026-09-30T21:45:00Z", -1.4,
                        result="no"),
                _open(4, "eva_wick_eth_sol", "KXSOL15M-A",
                      "2026-09-30T22:05:00Z"),
                # Pre-epoch cross row (dry run) must not score.
                _closed(5, "eva_wick_btc_hype", "KXHYPE15M-Z",
                        "2026-09-30T19:00:00Z", "2026-09-30T19:15:00Z", 9.0),
            ],
        )
        self._patches = [
            patch.object(kalshi_bridge, "kalshi_db_path", lambda: self.db),
            patch.object(kalshi_bridge, "lastmin_db_path", lambda: None),
            patch.object(kalshi_bridge, "experiment_epoch", lambda: EPOCH),
            patch.object(kalshi_bridge, "alt_epoch", lambda: ALT_EPOCH),
            patch.object(kalshi_bridge, "hourly_epoch", lambda: HOURLY_EPOCH),
            patch.object(kalshi_bridge, "cross_epoch", lambda: CROSS_EPOCH),
            patch.object(kalshi_bridge, "live_bots", lambda: ("eva_wick",)),
        ]
        for p in self._patches:
            p.start()
        self.payload = kalshi_bridge.performance_payload(limit=15)

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def test_cross_books_get_their_own_section_in_order(self) -> None:
        cross = self.payload["cross"]
        self.assertTrue(cross["available"])
        self.assertEqual(
            [b["bot_id"] for b in cross["bots"]],
            list(kalshi_bridge._CROSS_BOTS),
        )
        by_id = {b["bot_id"]: b for b in cross["bots"]}
        self.assertEqual(by_id["eva_wick_btc_xrp"]["label"], "BTC→XRP")
        self.assertEqual(by_id["eva_wick_btc_xrp"]["coin"], "XRP")
        self.assertEqual(by_id["eva_wick_btc_xrp"]["signal"], "BTC")
        for b in cross["bots"]:
            self.assertEqual(b["mode"], "paper")

    def test_cross_trades_stay_out_of_the_shared_feeds(self) -> None:
        self.assertEqual(
            [b["bot_id"] for b in self.payload["bots"]],
            ["eva_wick", "eva_arb", "eva_streak"],
        )
        self.assertEqual(
            [p["market_ticker"] for p in self.payload["closed"]],
            ["KXBTC15M-A"],
        )
        self.assertEqual(self.payload["open"], [])
        self.assertEqual(
            [p["market_ticker"] for p in self.payload["cross"]["open"]],
            ["KXSOL15M-A"],
        )
        self.assertEqual(self.payload["hidden_closed"], 0)

    def test_scored_from_the_cross_epoch(self) -> None:
        by_id = {b["bot_id"]: b for b in self.payload["cross"]["bots"]}
        xrp = by_id["eva_wick_btc_xrp"]
        self.assertEqual((xrp["closed"], xrp["wins"], xrp["losses"]), (2, 1, 1))
        # The HYPE book's only row predates the cross epoch.
        hype = by_id["eva_wick_btc_hype"]
        self.assertEqual(hype["closed"], 0)


if __name__ == "__main__":
    unittest.main()
