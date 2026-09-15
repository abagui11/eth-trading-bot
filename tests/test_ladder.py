"""The two books must scale out on the same rungs.

Live split its clip into whole CDE nano contracts and let the remainder ride
the furthest target; paper divided the size still open by the targets still
ahead, an exact even split. So a four-contract idea put half its size on the
last target in the live sleeve and a third of it there in the published paper
journal, and a BTC idea laddered across three targets in paper that a
one-contract live clip closes entirely at the first. Paper is the journal
subscribers read, so that gap was a reporting error.

These pin the shared ladder and the end-to-end parity between the books.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import execute as live_exec
import ladder
import paper
from models import Suggestion

TARGETS = [2440.0, 2477.0, 2534.0]


class ContractRungTests(unittest.TestCase):
    """`execute` keeps its own spelling of these, so they must be the same object."""

    def test_execute_uses_the_shared_ladder(self) -> None:
        self.assertIs(live_exec._tp_ladder, ladder.contract_rungs)
        self.assertIs(live_exec._ordered_tps, ladder.ordered_targets)


class UnitCountTests(unittest.TestCase):
    def test_counts_whole_units_and_ignores_the_remainder(self) -> None:
        self.assertEqual(ladder.unit_count(0.75, 0.1), 7)
        self.assertEqual(ladder.unit_count(0.4, 0.1), 4)

    def test_float_error_does_not_lose_a_unit(self) -> None:
        """0.1 * 3 is 0.30000000000000004 — truncation must not read it as 2."""
        self.assertEqual(ladder.unit_count(0.1 + 0.1 + 0.1, 0.1), 3)

    def test_a_position_smaller_than_one_unit_holds_none(self) -> None:
        self.assertEqual(ladder.unit_count(0.005, 0.01), 0)

    def test_no_unit_means_nothing_to_count(self) -> None:
        self.assertEqual(ladder.unit_count(5.0, None), 0)
        self.assertEqual(ladder.unit_count(0.0, 0.1), 0)


class WeightTests(unittest.TestCase):
    def test_a_freely_divisible_book_splits_evenly(self) -> None:
        self.assertEqual(
            ladder.weights(TARGETS),
            [(2440.0, 1 / 3), (2477.0, 1 / 3), (2534.0, 1 / 3)],
        )

    def test_weights_mirror_the_contract_plan(self) -> None:
        """Eva's 4-contract clip: 1/4, 1/4, 1/2 — the runner takes the remainder."""
        self.assertEqual(
            ladder.weights(TARGETS, units=4),
            [(2440.0, 0.25), (2477.0, 0.25), (2534.0, 0.5)],
        )

    def test_weights_always_sum_to_one(self) -> None:
        for units in range(1, 25):
            with self.subTest(units=units):
                shares = [share for _, share in ladder.weights(TARGETS, units=units)]
                self.assertAlmostEqual(sum(shares), 1.0, places=12)

    def test_one_unit_puts_everything_on_the_nearest_target(self) -> None:
        self.assertEqual(ladder.weights(TARGETS, units=1), [(2440.0, 1.0)])

    def test_a_position_below_one_unit_still_gets_a_rung(self) -> None:
        """Half a contract cannot be split either, so it banks at TP1."""
        self.assertEqual(ladder.weights(TARGETS, units=0), [(2440.0, 1.0)])

    def test_fewer_units_than_targets_uses_the_nearest(self) -> None:
        self.assertEqual(
            ladder.weights(TARGETS, units=2), [(2440.0, 0.5), (2477.0, 0.5)]
        )

    def test_no_targets_is_no_ladder(self) -> None:
        self.assertEqual(ladder.weights([]), [])
        self.assertEqual(ladder.weights([], units=4), [])


class LadderParityTests(unittest.TestCase):
    """Same idea, same size, same rungs — whichever book is holding it."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patches = [
            patch.object(config, "LEDGER_DB", Path(self._tmpdir.name) / "ledger.db"),
            patch.object(config, "PAPER_PORTFOLIO_VALUE", 5000.0),
            patch.object(paper, "flush_pending_outcome_charts"),
        ]
        for p in self._patches:
            p.start()
        paper.init_db()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmpdir.cleanup()

    def _open(self, qty: float, product_id: str, entry: float, tps: list[float]) -> None:
        paper.restore_open_position(
            action="spot_buy",
            entry=entry,
            eth_qty=qty,
            stop_loss=entry * 0.99,
            take_profits=tps,
            risk_reward=3.0,
            suggested_size=qty,
            opened_at="2026-09-11T12:00:00Z",
            open_cycle_id=f"parity_{product_id}_{qty}",
            spot_price=entry,
            product_id=product_id,
            force=True,
        )

    def _tp_fills(self) -> list[float]:
        """Take-profit fill sizes, nearest rung first."""
        closed = paper.get_closed_trades(limit=10)
        closed.reverse()  # get_closed_trades hands back most-recent-first
        return [
            float(t["eth_qty"]) for t in closed if t["close_reason"] == "take_profit"
        ]

    def _mark(self, product_id: str, price: float, cycle_id: str) -> None:
        paper.update(
            Suggestion.no_trade("mark"),
            spot_price=price,
            cycle_id=cycle_id,
            spots={product_id: price},
        )

    def test_paper_banks_the_same_fractions_a_live_clip_would(self) -> None:
        """0.4 ETH is 4 contracts: live rests 1/1/2, so paper banks 25/25/50."""
        self._open(0.4, "ETH-USD", 2400.0, TARGETS)
        self._mark("ETH-USD", 2534.0, "gap")

        expected = [n * 0.1 for _, n in live_exec._tp_ladder(4, TARGETS)]
        fills = self._tp_fills()
        self.assertEqual(len(fills), 3)
        for got, want in zip(fills, expected):
            self.assertAlmostEqual(got, want, places=6)

    def test_a_single_contract_position_closes_fully_at_tp1(self) -> None:
        """One BTC nano is one rung. Paper used to ladder it across all three."""
        self._open(0.016, "BTC-USD", 77000.0, [78000.0, 79000.0, 80000.0])
        self._mark("BTC-USD", 78000.0, "btc")

        fills = self._tp_fills()
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0], 0.016, places=6)
        self.assertFalse(paper.is_open())

    def test_a_target_beyond_the_last_rung_never_fills_on_its_own(self) -> None:
        """Two contracts over three targets: TP3 is out of reach, TP2 takes the rest."""
        self._open(0.25, "ETH-USD", 2400.0, TARGETS)
        self._mark("ETH-USD", 2477.0, "two")

        fills = self._tp_fills()
        self.assertEqual(len(fills), 2)
        self.assertAlmostEqual(sum(fills), 0.25, places=6)
        self.assertFalse(paper.is_open())

    def test_the_flag_restores_the_old_even_split(self) -> None:
        with patch.object(bot_config, "PAPER_LADDER_MATCHES_LIVE", False):
            self._open(0.4, "ETH-USD", 2400.0, TARGETS)
            self._mark("ETH-USD", 2534.0, "even")

            fills = self._tp_fills()
        self.assertEqual(len(fills), 3)
        for fill in fills:
            self.assertAlmostEqual(fill, 0.4 / 3, places=6)


if __name__ == "__main__":
    unittest.main()
