"""Altcoin wick clones get their own section of the Kalshi payload.

The load-bearing behaviour is the separation: the altcoin books settle on the
same 15m clock as the live book, so if they shared the main comparison table
and the closed-trade feed they would bury it within the hour.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kalshi_bridge

EPOCH = "2026-09-08T18:00:00Z"
# The altcoin books were switched on well after the sleeves, so the two
# families are scored from different start dates.
ALT_EPOCH = "2026-09-30T09:00:00Z"

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
    return (pid, bot, opened, closed, "KX", ticker, "X", "YES", 1, 73.0,
            None, "r", "closed", result, 1.0, pnl)


def _open(pid, bot, ticker, opened):
    return (pid, bot, opened, None, "KX", ticker, "X", "YES", 2, 70.0,
            None, "r", "open", None, None, None)


class AltcoinSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "ledger.db"
        _mk_ledger(
            self.db,
            states=[
                ("eva_wick", 225.0, 200.0, -25.0),
                ("eva_streak", 225.0, 225.0, 0.0),
                ("eva_arb", 225.0, 225.0, 0.0),
                ("eva_wick_xrp", 225.0, 220.0, -5.0),
                ("eva_wick_sol", 225.0, 230.0, 5.0),
                ("eva_wick_hype", 225.0, 225.0, 0.0),
            ],
            positions=[
                _closed(1, "eva_wick", "KXBTC15M-A",
                        "2026-09-29T10:00:00Z", "2026-09-29T10:15:00Z", 2.0),
                # Altcoin trades are newer, so an unfiltered feed would show
                # only these.
                _closed(2, "eva_wick_xrp", "KXXRP15M-A",
                        "2026-09-30T10:00:00Z", "2026-09-30T10:15:00Z", -3.0),
                _closed(3, "eva_wick_xrp", "KXXRP15M-B",
                        "2026-09-30T10:30:00Z", "2026-09-30T10:45:00Z", 1.0,
                        result="no"),
                _closed(4, "eva_wick_sol", "KXSOL15M-A",
                        "2026-09-30T11:00:00Z", "2026-09-30T11:15:00Z", 5.0),
                _open(5, "eva_wick_sol", "KXSOL15M-B", "2026-09-30T11:30:00Z"),
                _open(6, "eva_wick", "KXBTC15M-B", "2026-09-30T11:30:00Z"),
            ],
        )
        self._patches = [
            patch.object(kalshi_bridge, "kalshi_db_path", lambda: self.db),
            patch.object(kalshi_bridge, "lastmin_db_path", lambda: None),
            patch.object(kalshi_bridge, "experiment_epoch", lambda: EPOCH),
            patch.object(kalshi_bridge, "alt_epoch", lambda: ALT_EPOCH),
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

    def test_altcoin_books_are_listed_in_asset_order(self) -> None:
        alt = self.payload["altcoins"]
        self.assertTrue(alt["available"])
        self.assertEqual(
            [b["bot_id"] for b in alt["bots"]],
            ["eva_wick_xrp", "eva_wick_sol", "eva_wick_hype"],
        )
        self.assertEqual([b["label"] for b in alt["bots"]],
                         ["XRP", "SOL", "HYPE"])

    def test_altcoin_trades_do_not_crowd_out_the_live_feed(self) -> None:
        tickers = [p["market_ticker"] for p in self.payload["closed"]]
        self.assertEqual(tickers, ["KXBTC15M-A"])
        self.assertEqual(
            [p["market_ticker"] for p in self.payload["open"]],
            ["KXBTC15M-B"],
        )

    def test_records_match_the_ledger(self) -> None:
        by_id = {b["bot_id"]: b for b in self.payload["altcoins"]["bots"]}
        xrp = by_id["eva_wick_xrp"]
        self.assertEqual((xrp["closed"], xrp["wins"], xrp["losses"]), (2, 1, 1))
        self.assertAlmostEqual(xrp["epoch_pnl_usd"], -2.0)
        self.assertAlmostEqual(xrp["win_rate"], 0.5)
        sol = by_id["eva_wick_sol"]
        self.assertEqual(sol["open"], 1)
        # Equity carries the cost of the open clip, as it does for every book.
        self.assertAlmostEqual(sol["equity_usd"], 230.0 + 1.40)

    def test_a_book_with_no_trades_yet_still_gets_a_row(self) -> None:
        hype = next(b for b in self.payload["altcoins"]["bots"]
                    if b["bot_id"] == "eva_wick_hype")
        self.assertEqual((hype["closed"], hype["open"]), (0, 0))
        self.assertIsNone(hype["win_rate"])

    def test_altcoin_books_never_render_as_live(self) -> None:
        """Even if someone adds them to KALSHI_LIVE_BOTS on the hub."""
        with patch.object(
            kalshi_bridge, "live_bots",
            lambda: ("eva_wick", "eva_wick_xrp", "eva_wick_sol",
                     "eva_wick_hype"),
        ):
            payload = kalshi_bridge.performance_payload(limit=15)
        for row in payload["altcoins"]["bots"]:
            self.assertEqual(row["mode"], "paper")
        # And they must not leak into the live totals.
        self.assertEqual(payload["totals"]["label"], "EVA wick")

    def test_epoch_is_when_the_books_were_switched_on(self) -> None:
        """Not the first trade — the windows they skipped are part of the record."""
        alt = self.payload["altcoins"]
        self.assertEqual(alt["epoch"], ALT_EPOCH)
        self.assertEqual(alt["first_trade"], "2026-09-30T10:00:00Z")

    def test_altcoin_rows_before_the_altcoin_epoch_are_not_scored(self) -> None:
        """A reset moves the start date; the old rows must drop out with it."""
        with patch.object(
            kalshi_bridge, "alt_epoch", lambda: "2026-09-30T10:15:00Z"
        ):
            payload = kalshi_bridge.performance_payload(limit=15)
        xrp = next(b for b in payload["altcoins"]["bots"]
                   if b["bot_id"] == "eva_wick_xrp")
        # Only the 10:30 entry survives the later cut-off.
        self.assertEqual((xrp["closed"], xrp["wins"], xrp["losses"]), (1, 1, 0))
        # The sleeves keep their own, earlier epoch.
        wick = next(b for b in payload["bots"] if b["bot_id"] == "eva_wick")
        self.assertEqual(wick["closed"], 1)

    def test_altcoin_rows_are_not_counted_as_hidden_sleeve_history(self) -> None:
        self.assertEqual(self.payload["hidden_closed"], 0)

    def test_open_altcoin_positions_are_kept_with_their_section(self) -> None:
        self.assertEqual(
            [p["market_ticker"] for p in self.payload["altcoins"]["open"]],
            ["KXSOL15M-B"],
        )


if __name__ == "__main__":
    unittest.main()
