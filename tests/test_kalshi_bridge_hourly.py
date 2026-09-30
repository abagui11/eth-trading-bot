"""Hourly piggyback books get their own section of the Kalshi payload.

Same separation logic as the altcoin clones: the eva_wick_1h_* books are
wick derivatives, not a fourth sleeve, and their trades (up to seven
contracts per fire) would bury the live feed if they shared it.
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
    return (pid, bot, opened, closed, "KXBTCD", ticker, "BTC", "NO", 4, 40.0,
            None, "r", "closed", result, 4.0, pnl)


def _open(pid, bot, ticker, opened):
    return (pid, bot, opened, None, "KXBTCD", ticker, "BTC", "NO", 2, 30.0,
            None, "r", "open", None, None, None)


class HourlySectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "ledger.db"
        _mk_ledger(
            self.db,
            states=[
                ("eva_wick", 225.0, 200.0, -25.0),
                ("eva_streak", 225.0, 225.0, 0.0),
                ("eva_arb", 225.0, 225.0, 0.0),
                ("eva_wick_1h_ladder", 225.0, 226.0, 1.0),
                ("eva_wick_1h_flat", 225.0, 225.0, 0.0),
            ],
            positions=[
                _closed(1, "eva_wick", "KXBTC15M-A",
                        "2026-09-29T10:00:00Z", "2026-09-29T10:15:00Z", 2.0),
                # Hourly rows are newest; an unfiltered feed would show only
                # these.
                _closed(2, "eva_wick_1h_ladder", "KXBTCD-A-T84299.99",
                        "2026-09-30T21:13:00Z", "2026-09-30T22:05:00Z", 2.4),
                _closed(3, "eva_wick_1h_ladder", "KXBTCD-A-T84199.99",
                        "2026-09-30T21:13:00Z", "2026-09-30T22:05:00Z", -1.4,
                        result="no"),
                _open(4, "eva_wick_1h_flat", "KXBTCD-B-T84299.99",
                      "2026-09-30T22:20:00Z"),
                _open(5, "eva_wick", "KXBTC15M-B", "2026-09-30T22:30:00Z"),
                # Pre-epoch hourly row (e.g. from a dry run) must not score.
                _closed(6, "eva_wick_1h_flat", "KXBTCD-Z-T84000.99",
                        "2026-09-30T18:00:00Z", "2026-09-30T19:05:00Z", 9.0),
            ],
        )
        self._patches = [
            patch.object(kalshi_bridge, "kalshi_db_path", lambda: self.db),
            patch.object(kalshi_bridge, "lastmin_db_path", lambda: None),
            patch.object(kalshi_bridge, "experiment_epoch", lambda: EPOCH),
            patch.object(kalshi_bridge, "alt_epoch", lambda: ALT_EPOCH),
            patch.object(kalshi_bridge, "hourly_epoch", lambda: HOURLY_EPOCH),
            patch.object(kalshi_bridge, "live_bots", lambda: ("eva_wick",)),
        ]
        for p in self._patches:
            p.start()
        self.payload = kalshi_bridge.performance_payload(limit=15)

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def test_main_comparison_still_shows_only_the_three_sleeves(self) -> None:
        self.assertEqual(
            [b["bot_id"] for b in self.payload["bots"]],
            ["eva_wick", "eva_arb", "eva_streak"],
        )

    def test_hourly_books_get_their_own_section(self) -> None:
        hourly = self.payload["hourly"]
        self.assertTrue(hourly["available"])
        self.assertEqual(
            [b["bot_id"] for b in hourly["bots"]],
            ["eva_wick_1h_ladder", "eva_wick_1h_flat"],
        )
        for b in hourly["bots"]:
            self.assertEqual(b["mode"], "paper")
            self.assertEqual(b["series"], "KXBTCD · KXETHD")

    def test_hourly_trades_do_not_crowd_out_the_live_feed(self) -> None:
        self.assertEqual(
            [p["market_ticker"] for p in self.payload["closed"]],
            ["KXBTC15M-A"],
        )
        self.assertEqual(
            [p["market_ticker"] for p in self.payload["open"]],
            ["KXBTC15M-B"],
        )
        self.assertEqual(
            [p["market_ticker"] for p in self.payload["hourly"]["open"]],
            ["KXBTCD-B-T84299.99"],
        )

    def test_scored_from_the_hourly_epoch_not_the_sleeve_epoch(self) -> None:
        by_id = {b["bot_id"]: b for b in self.payload["hourly"]["bots"]}
        ladder = by_id["eva_wick_1h_ladder"]
        self.assertEqual(
            (ladder["closed"], ladder["wins"], ladder["losses"]), (2, 1, 1)
        )
        self.assertAlmostEqual(ladder["epoch_pnl_usd"], 1.0)
        # The flat book's only closed row predates the hourly epoch.
        flat = by_id["eva_wick_1h_flat"]
        self.assertEqual(flat["closed"], 0)
        self.assertEqual(flat["open"], 1)

    def test_zero_trade_book_still_gets_a_row(self) -> None:
        """Seeded paper_state alone must surface the book (funnel needs it)."""
        flat = next(b for b in self.payload["hourly"]["bots"]
                    if b["bot_id"] == "eva_wick_1h_flat")
        self.assertIsNone(flat["win_rate"])

    def test_hourly_rows_are_not_counted_as_hidden_sleeve_history(self) -> None:
        self.assertEqual(self.payload["hidden_closed"], 0)

    def test_hourly_books_never_render_as_live(self) -> None:
        with patch.object(
            kalshi_bridge, "live_bots",
            lambda: ("eva_wick", "eva_wick_1h_ladder", "eva_wick_1h_flat"),
        ):
            payload = kalshi_bridge.performance_payload(limit=15)
        for row in payload["hourly"]["bots"]:
            self.assertEqual(row["mode"], "paper")
        self.assertEqual(payload["totals"]["label"], "EVA wick")


if __name__ == "__main__":
    unittest.main()
