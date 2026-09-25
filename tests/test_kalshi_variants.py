"""Wick pairing-variant shadow table (dashboard/kalshi_variants.py)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dashboard import kalshi_variants


def _mk_kalshi(path: Path, rows) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE paper_positions (bot_id TEXT, market_ticker TEXT, "
        "side TEXT, contracts INTEGER, entry_cents REAL, pnl_usd REAL, "
        "opened_at TEXT, status TEXT)"
    )
    conn.executemany(
        "INSERT INTO paper_positions VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _mk_quotes(path: Path, rows) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE quotes (ts TEXT, ticker TEXT, yes_bid REAL, yes_ask REAL)"
    )
    conn.executemany("INSERT INTO quotes VALUES (?,?,?,?)", rows)
    conn.commit()
    conn.close()


class TestVariants(unittest.TestCase):
    def setUp(self) -> None:
        kalshi_variants._cache.update(at=0.0, payload=None)
        self.tmp = tempfile.TemporaryDirectory()
        self.kdb = Path(self.tmp.name) / "kalshi.db"
        self.qdb = Path(self.tmp.name) / "lastmin.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _payload(self, with_quotes: bool = True):
        return kalshi_variants._build(
            self.kdb, self.qdb if with_quotes else None)

    def test_opposite_window_variants_measure_the_recorded_book(self) -> None:
        # One opposite window: BTC YES first (lost -5.6), ETH NO second (won +2.4).
        _mk_kalshi(self.kdb, [
            ("eva_wick", "KXBTC15M-26SEP251515-15", "YES", 8, 70.0, -5.6,
             "2026-09-25T19:05:00Z", "settled"),
            ("eva_wick", "KXETH15M-26SEP251515-15", "NO", 8, 70.0, 2.4,
             "2026-09-25T19:08:00Z", "settled"),
        ])
        # First leg's book at the second entry: bid 40 / ask 44.
        _mk_quotes(self.qdb, [
            ("2026-09-25T19:08:01", "KXBTC15M-26SEP251515-15", 40.0, 44.0),
        ])
        p = self._payload()
        v = {x["key"]: x for x in p["variants"]}
        self.assertEqual(p["opposite_windows"], 1)
        # skip second: delta = -(+2.4)
        self.assertAlmostEqual(v["skip_second_opposite"]["delta_usd"], -2.4)
        # double second: +2.4 minus its own fee (0.07*.7*.3*8 = 0.1176 -> 0.12)
        self.assertAlmostEqual(v["double_second_opposite"]["delta_usd"], 2.28)
        # exit first at bid 40: (0.40-0.70)*8 = -2.40, fee
        # ceil(0.07*0.4*0.6*8*100)/100 = 0.14 -> pnl -2.54;
        # delta vs the recorded -5.6 = +3.06
        self.assertAlmostEqual(v["exit_first_opposite"]["delta_usd"], 3.06)

    def test_aligned_window_variants(self) -> None:
        # One aligned window: both YES; first won +2.4, second won +2.4.
        _mk_kalshi(self.kdb, [
            ("eva_wick", "KXBTC15M-26SEP251515-15", "YES", 8, 70.0, 2.4,
             "2026-09-25T19:05:00Z", "settled"),
            ("eva_wick", "KXETH15M-26SEP251515-15", "YES", 8, 70.0, 2.4,
             "2026-09-25T19:08:00Z", "settled"),
        ])
        # First leg asks 80c at the signal.
        _mk_quotes(self.qdb, [
            ("2026-09-25T19:08:01", "KXBTC15M-26SEP251515-15", 78.0, 80.0),
        ])
        p = self._payload()
        v = {x["key"]: x for x in p["variants"]}
        self.assertEqual(p["aligned_windows"], 1)
        self.assertAlmostEqual(v["double_second_aligned"]["delta_usd"], 2.28)
        # add to first at 80c ask, it settles a winner:
        # (1-0.8)*8 - fee(0.8 -> 0.0896 ceil 0.09) = 1.60-0.09 = 1.51
        self.assertAlmostEqual(v["double_first_aligned"]["delta_usd"], 1.51)

    def test_quote_gap_is_skipped_not_invented(self) -> None:
        _mk_kalshi(self.kdb, [
            ("eva_wick", "KXBTC15M-26SEP251515-15", "YES", 8, 70.0, -5.6,
             "2026-09-25T19:05:00Z", "settled"),
            ("eva_wick", "KXETH15M-26SEP251515-15", "NO", 8, 70.0, 2.4,
             "2026-09-25T19:08:00Z", "settled"),
        ])
        _mk_quotes(self.qdb, [
            # nearest quote is 5 minutes away -> too stale to price an exit
            ("2026-09-25T19:13:30", "KXBTC15M-26SEP251515-15", 40.0, 44.0),
        ])
        p = self._payload()
        v = {x["key"]: x for x in p["variants"]}
        self.assertEqual(v["exit_first_opposite"]["skipped_no_quote"], 1)
        self.assertEqual(v["exit_first_opposite"]["n_windows"], 0)

    def test_no_quote_log_fails_soft(self) -> None:
        _mk_kalshi(self.kdb, [
            ("eva_wick", "KXBTC15M-26SEP251515-15", "YES", 8, 70.0, -5.6,
             "2026-09-25T19:05:00Z", "settled"),
            ("eva_wick", "KXETH15M-26SEP251515-15", "NO", 8, 70.0, 2.4,
             "2026-09-25T19:08:00Z", "settled"),
        ])
        p = self._payload(with_quotes=False)
        v = {x["key"]: x for x in p["variants"]}
        self.assertFalse(v["exit_first_opposite"]["available"])
        self.assertTrue(v["skip_second_opposite"]["available"])

    def test_missing_ledger_reports_unavailable(self) -> None:
        p = kalshi_variants._build(Path(self.tmp.name) / "nope.db", None)
        self.assertFalse(p["available"])


if __name__ == "__main__":
    unittest.main()
