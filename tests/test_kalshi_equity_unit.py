"""The Kalshi tab chart is size-agnostic: per-contract P&L at a constant ref.

Load-bearing behaviour: an 8-contract-era trade and a 100-contract-era trade
with the same per-contract result move the curve by the same step. The old
seed-divided curve let the clip scale-ups (and the books' 10x different
seeds) masquerade as performance.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kalshi_bridge
from dashboard import edge_analytics


def _mk_ledger(path: Path, positions) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE paper_positions (id INTEGER PRIMARY KEY, bot_id TEXT, "
        "opened_at TEXT, closed_at TEXT, market_ticker TEXT, side TEXT, "
        "contracts INTEGER, entry_cents REAL, status TEXT, pnl_usd REAL)"
    )
    conn.executemany(
        "INSERT INTO paper_positions (bot_id, opened_at, closed_at, "
        "market_ticker, side, contracts, entry_cents, status, pnl_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        positions,
    )
    conn.commit()
    conn.close()


class KalshiEquityUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "ledger.db"
        # Same +$1/ct result at 8 ct and at 100 ct, then a -$0.80/ct loss
        # at 100 ct. Dollar P&L differs 12.5x; per-contract steps must not.
        _mk_ledger(self.db, [
            ("eva_wick", "2026-09-20T10:00:00Z", "2026-09-20T10:15:00Z",
             "KXBTC15M-A", "YES", 8, 78.0, "closed", 8.0),
            ("eva_wick", "2026-10-02T10:00:00Z", "2026-10-02T10:15:00Z",
             "KXBTC15M-B", "YES", 100, 78.0, "closed", 100.0),
            ("eva_wick", "2026-10-02T11:00:00Z", "2026-10-02T11:15:00Z",
             "KXBTC15M-C", "NO", 100, 80.0, "closed", -80.0),
            # Open trade: no pnl yet, must be excluded.
            ("eva_wick", "2026-10-02T12:00:00Z", None,
             "KXBTC15M-D", "YES", 100, 75.0, "open", None),
        ])
        self._patch = patch.object(
            kalshi_bridge, "kalshi_db_path", lambda: self.db
        )
        self._patch.start()
        edge_analytics._equity_cache.update(at=0.0, payload=None)

    def tearDown(self) -> None:
        self._patch.stop()
        edge_analytics._equity_cache.update(at=0.0, payload=None)
        self._tmp.cleanup()

    def test_equal_per_contract_results_step_equally(self) -> None:
        payload = edge_analytics.kalshi_equity_curves()
        self.assertTrue(payload["available"])
        wick = next(
            b for b in payload["books"] if b["key"] == "kalshi:eva_wick"
        )
        ys = [p[1] for p in wick["equity"]]
        self.assertEqual(len(ys), 3)
        scale = (
            100.0 * edge_analytics.KALSHI_UNIT_REF_CT
            / edge_analytics.KALSHI_UNIT_REF_BOOK_USD
        )
        # +$1/ct, +$1/ct, -$0.80/ct — the 8ct and 100ct wins step identically.
        self.assertAlmostEqual(ys[0], 1.0 * scale, places=2)
        self.assertAlmostEqual(ys[1] - ys[0], 1.0 * scale, places=2)
        self.assertAlmostEqual(ys[2] - ys[1], -0.8 * scale, places=2)
        self.assertAlmostEqual(wick["unit_usd_per_ct"], 1.2, places=2)
        self.assertEqual(wick["ref_ct"], edge_analytics.KALSHI_UNIT_REF_CT)

    def test_books_without_trades_are_omitted(self) -> None:
        payload = edge_analytics.kalshi_equity_curves()
        keys = {b["key"] for b in payload["books"]}
        self.assertEqual(keys, {"kalshi:eva_wick"})


class LastminPaginationTests(unittest.TestCase):
    """Offset pages skip the dip study — pagination only needs closed rows."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self._tmp.name) / "ledger.db"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE paper_state (bot_id TEXT, starting_usd REAL, "
            "cash_usd REAL, realized_pnl_usd REAL)"
        )
        conn.execute(
            "CREATE TABLE paper_positions (id INTEGER PRIMARY KEY, "
            "bot_id TEXT, opened_at TEXT, closed_at TEXT, series TEXT, "
            "market_ticker TEXT, product_id TEXT, side TEXT, "
            "contracts INTEGER, entry_cents REAL, expiry_ts TEXT, "
            "rationale TEXT, status TEXT, result TEXT, payout_usd REAL, "
            "pnl_usd REAL)"
        )
        conn.execute(
            "INSERT INTO paper_state VALUES ('eva_wick', 225.0, 227.0, 2.0)"
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO paper_positions (bot_id, opened_at, closed_at, "
                "market_ticker, side, contracts, entry_cents, status, "
                "result, pnl_usd) VALUES ('eva_wick', ?, ?, 'KXBTC15M-X', "
                "'YES', 8, 73.0, 'closed', 'yes', 2.0)",
                (f"2026-09-29T1{i}:00:00Z", f"2026-09-29T1{i}:15:00Z"),
            )
        conn.commit()
        conn.close()
        self._patches = [
            patch.object(kalshi_bridge, "kalshi_db_path", lambda: self.db),
            patch.object(kalshi_bridge, "live_bots", lambda: ("eva_wick",)),
            patch.object(
                kalshi_bridge, "lastmin_payload", lambda: {"windows": 7}
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def test_first_page_carries_lastmin_offset_pages_skip_it(self) -> None:
        page1 = kalshi_bridge.performance_payload(limit=1, offset=0)
        page2 = kalshi_bridge.performance_payload(limit=1, offset=1)
        self.assertEqual(page1["lastmin"], {"windows": 7})
        self.assertIsNone(page2["lastmin"])
        # Pagination itself still works.
        self.assertEqual(len(page2["closed"]), 1)
        self.assertTrue(page1["closed_has_more"])


if __name__ == "__main__":
    unittest.main()
