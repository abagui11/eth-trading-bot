"""Live take-profits must be enforced by the exchange, not just recorded.

Before this, ``execute.py`` placed the entry and one protective stop, wrote the
target ladder into ``live_trades.take_profits_json``, and never acted on it —
nothing read that column except the dashboard. A live trade could only ever end
at its stop, which is why every closed live trade was a loss. These tests pin
the ladder maths, the arming path, and the reconciliation that books fills.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import bot_config
import config
import execute as live_exec
import live_ledger
from coinbase_deriv import GatewayError


class TpLadderTests(unittest.TestCase):
    """Contracts are indivisible, so the ladder must be whole numbers."""

    def test_even_split_across_targets(self) -> None:
        self.assertEqual(
            live_exec._tp_ladder(6, [2440.0, 2477.0, 2534.0]),
            [(2440.0, 2), (2477.0, 2), (2534.0, 2)],
        )

    def test_remainder_rides_the_furthest_target(self) -> None:
        """Eva's 4-contract clip over 3 targets: the runner is the last rung."""
        self.assertEqual(
            live_exec._tp_ladder(4, [2440.0, 2477.0, 2534.0]),
            [(2440.0, 1), (2477.0, 1), (2534.0, 2)],
        )
        self.assertEqual(
            live_exec._tp_ladder(5, [2440.0, 2477.0, 2534.0]),
            [(2440.0, 1), (2477.0, 2), (2534.0, 2)],
        )

    def test_single_contract_closes_fully_at_tp1(self) -> None:
        """A mill clip is one contract — it can't scale, so it banks early."""
        self.assertEqual(
            live_exec._tp_ladder(1, [2440.0, 2477.0, 2534.0]), [(2440.0, 1)]
        )

    def test_fewer_contracts_than_targets_uses_nearest(self) -> None:
        self.assertEqual(
            live_exec._tp_ladder(2, [2440.0, 2477.0, 2534.0]),
            [(2440.0, 1), (2477.0, 1)],
        )

    def test_every_contract_is_allocated(self) -> None:
        for n in range(1, 25):
            ladder = live_exec._tp_ladder(n, [1.0, 2.0, 3.0])
            self.assertEqual(sum(c for _, c in ladder), n, f"lost contracts at {n}")

    def test_no_contracts_or_no_targets_yields_nothing(self) -> None:
        self.assertEqual(live_exec._tp_ladder(0, [2440.0]), [])
        self.assertEqual(live_exec._tp_ladder(4, []), [])


class OrderedTpTests(unittest.TestCase):
    def test_long_targets_ascend_and_drop_wrong_side(self) -> None:
        self.assertEqual(
            live_exec._ordered_tps("long", [2534.0, 2380.0, 2440.0], 2411.5),
            [2440.0, 2534.0],
        )

    def test_short_targets_descend_and_drop_wrong_side(self) -> None:
        self.assertEqual(
            live_exec._ordered_tps("short", [2300.0, 2600.0, 2380.0], 2411.5),
            [2380.0, 2300.0],
        )


class ArmableTpTests(unittest.TestCase):
    """The recorded plan must match the rungs the clip can actually place."""

    def test_one_btc_contract_records_only_tp1(self) -> None:
        self.assertEqual(
            live_exec._armable_tps(
                "BTC-USD", "long", 0.01, 80000.0, [81000.0, 82000.0, 83000.0]
            ),
            [81000.0],
        )

    def test_three_eth_contracts_record_the_whole_ladder(self) -> None:
        self.assertEqual(
            live_exec._armable_tps(
                "ETH-USD", "long", 0.3, 2400.0, [2440.0, 2477.0, 2534.0]
            ),
            [2440.0, 2477.0, 2534.0],
        )

    def test_a_fourth_target_is_unreachable_on_three_contracts(self) -> None:
        self.assertEqual(
            live_exec._armable_tps(
                "ETH-USD", "long", 0.3, 2400.0, [2440.0, 2477.0, 2534.0, 2600.0]
            ),
            [2440.0, 2477.0, 2534.0],
        )

    def test_targets_behind_the_fill_are_dropped_first(self) -> None:
        self.assertEqual(
            live_exec._armable_tps(
                "BTC-USD", "short", 0.01, 80000.0, [81000.0, 79000.0, 78000.0]
            ),
            [79000.0],
        )

    def test_unknown_product_keeps_every_target(self) -> None:
        self.assertEqual(
            live_exec._armable_tps(
                "SOL-USD", "long", 5.0, 100.0, [110.0, 120.0]
            ),
            [110.0, 120.0],
        )


def _gateway(mark: float = 2420.0, contract_size: float = 0.1) -> MagicMock:
    gw = MagicMock()
    gw.contract_size.return_value = contract_size
    gw.get_position.return_value = {"size": 0.4, "mark_price": mark}
    gw.place_bracket.side_effect = lambda **kw: {
        "order": {"order_id": f"br-{kw['limit_price']:g}"}
    }
    gw.place_stop_market.return_value = {"order": {"order_id": "stop-1"}}
    gw.place_market_order.side_effect = lambda **kw: {
        "order": {
            "order_id": f"mkt-{kw['amount']:g}",
            "average_price": mark,
            "filled_qty": kw["amount"],
        }
    }
    return gw


class ArmExitsTests(unittest.TestCase):
    def test_each_target_rests_as_a_bracket_carrying_the_stop(self) -> None:
        gw = _gateway(mark=2420.0)
        res = live_exec.arm_exits(
            gw,
            instrument="ETP-20DEC30-CDE",
            side="long",
            qty=0.4,
            entry=2411.5,
            stop_loss=2385.0,
            take_profits=[2440.0, 2477.0, 2534.0],
            label="hq:c1",
        )
        self.assertEqual(res["mode"], "brackets")
        self.assertEqual(len(res["exit_order_ids"]), 3)
        self.assertEqual(res["realized"], [])
        # Total resting size equals the position — never more.
        self.assertAlmostEqual(
            sum(c.kwargs["amount"] for c in gw.place_bracket.call_args_list), 0.4
        )
        # Every rung carries the same protective stop.
        for call in gw.place_bracket.call_args_list:
            self.assertEqual(call.kwargs["stop_trigger_price"], 2385.0)
        gw.place_stop_market.assert_not_called()

    def test_target_already_through_the_market_is_banked_now(self) -> None:
        """Mirrors the paper ladder's gap-through fill."""
        gw = _gateway(mark=2470.0)
        res = live_exec.arm_exits(
            gw,
            instrument="ETP-20DEC30-CDE",
            side="long",
            qty=0.4,
            entry=2411.5,
            stop_loss=2385.0,
            take_profits=[2440.0, 2477.0, 2534.0],
            label="hq:c1",
        )
        self.assertEqual(len(res["realized"]), 1)
        self.assertEqual(res["realized"][0]["target"], 2440.0)
        self.assertAlmostEqual(res["realized"][0]["price"], 2470.0)
        # TP2 and TP3 are still ahead, so they rest.
        self.assertEqual(len(res["exit_order_ids"]), 2)

    def test_short_side_targets_below_the_market_are_banked(self) -> None:
        gw = _gateway(mark=2300.0)
        res = live_exec.arm_exits(
            gw,
            instrument="ETP-20DEC30-CDE",
            side="short",
            qty=0.4,
            entry=2411.5,
            stop_loss=2450.0,
            take_profits=[2380.0, 2340.0, 2250.0],
            label="hq:c1",
        )
        self.assertEqual([r["target"] for r in res["realized"]], [2380.0, 2340.0])
        self.assertEqual(len(res["exit_order_ids"]), 1)

    def test_bracket_rejection_falls_back_to_a_protective_stop(self) -> None:
        """The position must never be left without a stop."""
        gw = _gateway(mark=2420.0)
        gw.place_bracket.side_effect = GatewayError("brackets unavailable")
        res = live_exec.arm_exits(
            gw,
            instrument="ETP-20DEC30-CDE",
            side="long",
            qty=0.4,
            entry=2411.5,
            stop_loss=2385.0,
            take_profits=[2440.0, 2477.0, 2534.0],
            label="hq:c1",
        )
        self.assertEqual(res["mode"], "stop_only")
        self.assertEqual(res["stop_order_id"], "stop-1")
        gw.place_stop_market.assert_called_once()
        self.assertAlmostEqual(gw.place_stop_market.call_args.kwargs["amount"], 0.4)

    def test_partial_brackets_are_cancelled_before_falling_back(self) -> None:
        gw = _gateway(mark=2420.0)
        gw.place_bracket.side_effect = [
            {"order": {"order_id": "br-1"}},
            GatewayError("rejected"),
        ]
        live_exec.arm_exits(
            gw,
            instrument="ETP-20DEC30-CDE",
            side="long",
            qty=0.4,
            entry=2411.5,
            stop_loss=2385.0,
            take_profits=[2440.0, 2477.0, 2534.0],
            label="hq:c1",
        )
        gw.cancel_orders.assert_called_once_with(["br-1"])

    def test_no_usable_targets_leaves_a_plain_stop(self) -> None:
        gw = _gateway()
        res = live_exec.arm_exits(
            gw,
            instrument="ETP-20DEC30-CDE",
            side="long",
            qty=0.4,
            entry=2411.5,
            stop_loss=2385.0,
            take_profits=[2300.0],  # below entry on a long: unusable
            label="hq:c1",
        )
        self.assertEqual(res["mode"], "stop_only")
        gw.place_bracket.assert_not_called()


class MarketOrderConfirmationTests(unittest.TestCase):
    """A just-placed order 404s on lookup — that is not a failed order.

    Treating the 404 as "did not fill" once caused a live sell to execute while
    the caller believed it had not, and then try to protect a position that had
    already shrunk.
    """

    def _gateway(self) -> Any:
        from coinbase_deriv import DerivGateway

        gw = DerivGateway.__new__(DerivGateway)
        gw.contract_size = lambda _pid: 0.1
        gw._to_contracts = lambda _pid, amt: int(round(amt / 0.1))
        gw._create_order = lambda **kw: "ord-1"
        return gw

    def test_lookup_404_is_retried_not_treated_as_no_fill(self) -> None:
        gw = self._gateway()
        calls = []

        def fetch(order_id):
            calls.append(order_id)
            if len(calls) < 3:
                raise GatewayError("HTTP 404 order with this orderID was not found")
            return {
                "status": "FILLED",
                "filled_size": "1",
                "average_filled_price": "2466",
            }

        gw._fetch_order = fetch
        with patch("coinbase_deriv.time.sleep"):
            out = gw.place_market_order(
                instrument="ETP-20DEC30-CDE", side="sell", amount=0.1, label="t"
            )
        self.assertEqual(out["order"]["average_price"], 2466.0)
        self.assertAlmostEqual(out["order"]["filled_qty"], 0.1)
        self.assertGreaterEqual(len(calls), 3)

    def test_never_confirmed_order_says_so_rather_than_no_fill(self) -> None:
        gw = self._gateway()
        gw._fetch_order = MagicMock(side_effect=GatewayError("HTTP 404"))
        with patch("coinbase_deriv.time.sleep"):
            with self.assertRaises(GatewayError) as ctx:
                gw.place_market_order(
                    instrument="ETP-20DEC30-CDE", side="sell", amount=0.1, label="t"
                )
        self.assertIn("could not be confirmed", str(ctx.exception))


class ArmExitsFallbackSizingTests(unittest.TestCase):
    def test_fallback_stop_never_exceeds_what_the_exchange_holds(self) -> None:
        """A market leg can fail after executing, shrinking the position."""
        gw = _gateway(mark=2420.0)
        gw.place_bracket.side_effect = GatewayError("rejected")
        gw.get_position.return_value = {"size": 0.1, "mark_price": 2420.0}
        res = live_exec.arm_exits(
            gw,
            instrument="ETP-20DEC30-CDE",
            side="long",
            qty=0.4,
            entry=2411.5,
            stop_loss=2385.0,
            take_profits=[2440.0, 2477.0, 2534.0],
            label="hq:c1",
            mark=2420.0,
        )
        self.assertEqual(res["mode"], "stop_only")
        self.assertAlmostEqual(gw.place_stop_market.call_args.kwargs["amount"], 0.1)


class TrailedStopTests(unittest.TestCase):
    """The runner trails one rung behind: breakeven after TP1, TP1 after TP2.

    This is the exit rule the stop study measures Eva under, so live has to
    run it for the study's baseline to describe live. Locking the last filled
    target instead came from a single trade and re-measured as a wash.
    """

    def test_no_trail_before_any_target_fills(self) -> None:
        self.assertIsNone(live_exec._trailed_stop(2411.5, [2440.0, 2477.0], 0))

    def test_trails_one_rung_behind_the_target_that_paid(self) -> None:
        tps = [2440.0, 2477.0, 2534.0]
        self.assertEqual(live_exec._trailed_stop(2411.5, tps, 1), 2411.5)
        self.assertEqual(live_exec._trailed_stop(2411.5, tps, 2), 2440.0)
        self.assertEqual(live_exec._trailed_stop(2411.5, tps, 3), 2477.0)

    def test_trail_only_ever_reduces_risk(self) -> None:
        self.assertTrue(live_exec._improves_stop("long", 2385.0, 2411.5))
        self.assertFalse(live_exec._improves_stop("long", 2411.5, 2385.0))
        self.assertTrue(live_exec._improves_stop("short", 2450.0, 2411.5))
        self.assertFalse(live_exec._improves_stop("short", 2411.5, 2450.0))

    def test_counts_rungs_price_actually_reached(self) -> None:
        """A profitable stop at TP1 is not a third take-profit."""
        trade = {
            "side": "long",
            "entry": 2411.5,
            "take_profits_json": json.dumps([2440.0, 2477.0, 2534.0]),
            "exit_fills_json": json.dumps(
                {
                    "a": {"reason": "take_profit", "price": 2468.5},
                    "b": {"reason": "take_profit", "price": 2477.0},
                    "c": {"reason": "take_profit", "price": 2439.5},
                }
            ),
        }
        self.assertEqual(live_exec._tps_taken(trade), 2)

    def test_counts_only_take_profit_legs(self) -> None:
        trade = {
            "exit_fills_json": json.dumps(
                {
                    "a": {"reason": "take_profit"},
                    "b": {"reason": "stop_loss"},
                    "c": {"reason": "take_profit"},
                }
            )
        }
        self.assertEqual(live_exec._tps_taken(trade), 2)


class RetrailExitsTests(unittest.TestCase):
    def setUp(self) -> None:
        # Real settle waits are seconds long by design; the behaviour under
        # test is the ordering, not the clock.
        p = patch.object(live_exec, "_SETTLE_SLEEP", 0)
        p.start()
        self.addCleanup(p.stop)

    def _wire(self, gw: MagicMock, orders: dict[str, dict], *, lag: int = 0) -> None:
        """Model a venue where cancelling actually takes an order off the book.

        The retrail now waits for a cancel to settle before re-placing, so a
        gateway that reports OPEN forever is not a stand-in for the exchange —
        it is the pathological case, and it gets its own test. ``lag`` is how
        many status reads still say OPEN after the cancel is accepted, which is
        the real behaviour that broke Eva #19.
        """
        pending: dict[str, int] = {}

        def get_order(oid: str) -> dict:
            order = dict(orders[oid])
            if oid in pending:
                if pending[oid] > 0:
                    pending[oid] -= 1
                else:
                    order["status"] = "CANCELLED"
            return order

        gw.get_order.side_effect = get_order
        gw.cancel_orders.side_effect = lambda ids: pending.update(
            {oid: lag for oid in ids}
        )

    @staticmethod
    def _bracket(size: str, limit: str, status: str = "OPEN") -> dict:
        return {
            "status": status,
            "order_configuration": {
                "trigger_bracket_gtc": {
                    "base_size": size,
                    "limit_price": limit,
                    "stop_trigger_price": "2385",
                }
            },
        }

    def _trade(self) -> dict:
        return {
            "id": 8,
            "source": "hq",
            "instrument": "ETP-20DEC30-CDE",
            "side": "long",
            "exit_order_ids_json": json.dumps(["br-2477", "br-2534"]),
        }

    def test_each_rung_is_replaced_with_the_new_stop(self) -> None:
        gw = _gateway()
        self._wire(gw, {
            "br-2477": self._bracket("1", "2477"),
            "br-2534": self._bracket("2", "2534"),
        })
        fresh = live_exec.retrail_exits(gw, self._trade(), 2411.5)

        self.assertEqual(len(fresh), 2)
        # Targets are preserved; only the stop leg moves.
        self.assertEqual(
            [c.kwargs["limit_price"] for c in gw.place_bracket.call_args_list],
            [2477.0, 2534.0],
        )
        for call in gw.place_bracket.call_args_list:
            self.assertEqual(call.kwargs["stop_trigger_price"], 2411.5)
        self.assertAlmostEqual(gw.place_bracket.call_args_list[1].kwargs["amount"], 0.2)
        # One rung at a time, so the whole position is never uncovered at once.
        self.assertEqual(gw.cancel_orders.call_count, 2)

    def test_already_filled_rung_is_dropped(self) -> None:
        gw = _gateway()
        self._wire(gw, {
            "br-2477": self._bracket("1", "2477", status="FILLED"),
            "br-2534": self._bracket("2", "2534"),
        })
        fresh = live_exec.retrail_exits(gw, self._trade(), 2411.5)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(gw.place_bracket.call_count, 1)

    def test_failed_replacement_is_covered_by_a_plain_stop(self) -> None:
        """Cancel succeeded but re-place failed: that size must not stay naked."""
        gw = _gateway()
        self._wire(gw, {
            "br-2477": self._bracket("1", "2477"),
            "br-2534": self._bracket("2", "2534"),
        })
        gw.place_bracket.side_effect = [
            {"order": {"order_id": "new-1"}},
            GatewayError("rejected"),
        ]
        fresh = live_exec.retrail_exits(gw, self._trade(), 2411.5)

        gw.place_stop_market.assert_called_once()
        self.assertAlmostEqual(gw.place_stop_market.call_args.kwargs["amount"], 0.2)
        self.assertEqual(gw.place_stop_market.call_args.kwargs["trigger_price"], 2411.5)
        self.assertIn("stop-1", fresh)

    def test_a_rung_that_cannot_be_cancelled_is_left_alone(self) -> None:
        gw = _gateway()
        self._wire(gw, {
            "br-2477": self._bracket("1", "2477"),
            "br-2534": self._bracket("2", "2534"),
        })
        wired = gw.cancel_orders.side_effect
        calls: list[int] = []

        def cancel(ids: list[str]) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise GatewayError("cancel failed")
            wired(ids)

        gw.cancel_orders.side_effect = cancel
        fresh = live_exec.retrail_exits(gw, self._trade(), 2411.5)
        # Untouched rung keeps its original id and its original protection.
        self.assertIn("br-2477", fresh)
        self.assertEqual(gw.place_bracket.call_count, 1)

    def test_replacement_waits_for_the_cancel_to_settle(self) -> None:
        """The Eva #19 failure: the venue frees the size a few reads late.

        Sending the replacement into that window is what earned
        ``exceeds_position`` and cost the rung its target, so the retrail must
        not place until the cancel has actually reported terminal.
        """
        gw = _gateway()
        self._wire(gw, {"br-2477": self._bracket("1", "2477")}, lag=3)
        trade = self._trade() | {"exit_order_ids_json": json.dumps(["br-2477"])}
        seen_at_place: list[str] = []
        gw.place_bracket.side_effect = lambda **kw: (
            seen_at_place.append(gw.get_order("br-2477")["status"]),
            {"order": {"order_id": "new-1"}},
        )[1]

        fresh = live_exec.retrail_exits(gw, trade, 2411.5)

        self.assertEqual(fresh, ["new-1"])
        self.assertEqual(seen_at_place, ["CANCELLED"])
        gw.place_stop_market.assert_not_called()

    def test_a_late_reservation_release_is_retried_not_abandoned(self) -> None:
        """A settled cancel can still leave the freed size lagging a beat."""
        gw = _gateway()
        self._wire(gw, {"br-2477": self._bracket("1", "2477")})
        trade = self._trade() | {"exit_order_ids_json": json.dumps(["br-2477"])}
        gw.place_bracket.side_effect = [
            GatewayError(
                "order rejected: {'error': 'UNKNOWN_FAILURE_REASON', "
                "'error_details': 'preview_bracket_order_size_exceeds_position'}"
            ),
            {"order": {"order_id": "new-1"}},
        ]

        fresh = live_exec.retrail_exits(gw, trade, 2411.5)

        self.assertEqual(fresh, ["new-1"])
        # The target survived, so no bare-stop fallback was needed.
        gw.place_stop_market.assert_not_called()

    def test_an_unconfirmed_cancel_still_gets_a_replacement(self) -> None:
        """Status never catches up, but the cancel was accepted.

        Skipping the replacement here would leave the tranche naked while the
        ledger believed it covered — strictly worse than asking the venue,
        which refuses anything exceeding the unreserved position.
        """
        gw = _gateway()
        gw.get_order.side_effect = lambda oid: self._bracket("1", "2477")
        trade = self._trade() | {"exit_order_ids_json": json.dumps(["br-2477"])}

        fresh = live_exec.retrail_exits(gw, trade, 2411.5)

        self.assertEqual(gw.place_bracket.call_count, 1)
        self.assertEqual(fresh, ["br-2477"])


class LedgerDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patch = patch.object(config, "LEDGER_DB", db)
        self._cs = patch.object(bot_config, "CASE_STUDY_ENABLED", False)
        self._patch.start()
        self._cs.start()
        live_ledger.init_db()
        # Trade ids restart at 1 in each temp ledger, so the re-arm counter
        # would otherwise carry one test's attempts into the next.
        live_exec._rearm_attempts.clear()
        self.addCleanup(live_exec._rearm_attempts.clear)
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._cs.stop)
        self.addCleanup(self._tmp.cleanup)

    def _open_trade(self, **over) -> int:
        kwargs = dict(
            cycle_id="c1",
            source="hq",
            product_id="ETH-USD",
            instrument="ETP-20DEC30-CDE",
            side="long",
            qty=0.4,
            entry=2411.5,
            stop_loss=2385.0,
            take_profits_json=json.dumps([2440.0, 2477.0, 2534.0]),
            order_id="entry-1",
            stop_order_id=None,
            exit_order_ids=["br-2440", "br-2477", "br-2534"],
        )
        kwargs.update(over)
        return live_ledger.record_open(**kwargs)


class PartialExitLedgerTests(LedgerDbTestCase):
    def test_partial_exit_banks_pnl_and_reduces_open_size(self) -> None:
        tid = self._open_trade()
        ok = live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2440.0, pnl_usd=2.85,
            order_id="br-2440", reason="take_profit",
        )
        self.assertTrue(ok)
        row = live_ledger.get_trade(tid)
        self.assertAlmostEqual(row["qty_open"], 0.3)
        self.assertAlmostEqual(row["realized_pnl_usd"], 2.85)
        self.assertEqual(row["status"], "open")

    def test_booking_the_same_order_twice_is_a_no_op(self) -> None:
        """Reconciliation re-reads the same resting orders every pass."""
        tid = self._open_trade()
        first = live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2440.0, pnl_usd=2.85,
            order_id="br-2440", reason="take_profit",
        )
        second = live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2440.0, pnl_usd=2.85,
            order_id="br-2440", reason="take_profit",
        )
        self.assertTrue(first)
        self.assertFalse(second)
        row = live_ledger.get_trade(tid)
        self.assertAlmostEqual(row["qty_open"], 0.3)
        self.assertAlmostEqual(row["realized_pnl_usd"], 2.85)

    def test_close_totals_banked_partials_into_pnl(self) -> None:
        """Performance sums pnl_usd, so it must hold the whole trade."""
        tid = self._open_trade()
        live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2440.0, pnl_usd=2.85,
            order_id="br-2440", reason="take_profit",
        )
        live_ledger.record_close(
            tid, exit_price=2477.0, pnl_usd=19.65, close_reason="take_profit"
        )
        row = live_ledger.get_trade(tid)
        self.assertAlmostEqual(row["pnl_usd"], 22.50)
        self.assertEqual(row["qty_open"], 0)
        self.assertEqual(row["status"], "closed")

    def test_open_rows_start_fully_open_with_their_exit_orders(self) -> None:
        tid = self._open_trade()
        row = live_ledger.get_trade(tid)
        self.assertAlmostEqual(row["qty_open"], 0.4)
        self.assertEqual(row["realized_pnl_usd"], 0)
        self.assertEqual(
            json.loads(row["exit_order_ids_json"]),
            ["br-2440", "br-2477", "br-2534"],
        )


class RealizedPerformanceTests(LedgerDbTestCase):
    """Banked scale-outs are realized cash even while the trade runs."""

    def test_banked_partial_on_an_open_trade_counts_as_realized(self) -> None:
        tid = self._open_trade()
        live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2468.5, pnl_usd=5.70,
            order_id="mkt-1", reason="take_profit",
        )
        hq = live_ledger.get_live_performance()["by_source"]["hq"]
        self.assertAlmostEqual(hq["pnl_usd"], 5.70)
        self.assertAlmostEqual(hq["banked_open_usd"], 5.70)
        self.assertEqual(hq["open"], 1)
        self.assertEqual(hq["closed"], 0)

    def test_closing_the_trade_does_not_double_count_the_partial(self) -> None:
        tid = self._open_trade()
        live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2468.5, pnl_usd=5.70,
            order_id="mkt-1", reason="take_profit",
        )
        live_ledger.record_close(
            tid, exit_price=2477.0, pnl_usd=19.65, close_reason="take_profit"
        )
        hq = live_ledger.get_live_performance()["by_source"]["hq"]
        self.assertAlmostEqual(hq["pnl_usd"], 25.35)
        self.assertAlmostEqual(hq["banked_open_usd"], 0.0)

    def test_untouched_open_trade_reports_no_realized(self) -> None:
        self._open_trade()
        hq = live_ledger.get_live_performance()["by_source"]["hq"]
        self.assertEqual(hq["pnl_usd"], 0.0)
        self.assertEqual(hq["banked_open_usd"], 0.0)


class ReconcileTests(LedgerDbTestCase):
    """The netting bug: two sleeves share one contract, so a size check lies."""

    def setUp(self) -> None:
        super().setUp()
        self._patches = [
            patch.object(config, "EXECUTION_MODE", "live"),
            patch.object(live_exec.bot_config, "LIVE_FILL_ALERTS_ENABLED", False),
            patch.object(live_exec, "_check_daily_loss"),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    @staticmethod
    def _order(status: str, filled: float, price: float) -> dict:
        return {
            "status": status,
            "filled_size": str(filled),
            "average_filled_price": str(price),
        }

    def test_filled_target_is_booked_while_position_still_nets_open(self) -> None:
        tid = self._open_trade(exit_order_ids=["br-2440", "br-2477", "br-2534"])
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        # Exchange still shows size: the old size-only check saw nothing here.
        gw.get_position.return_value = {"size": 0.3, "mark_price": 2450.0}
        gw.get_order.side_effect = lambda oid: {
            "br-2440": self._order("FILLED", 1, 2440.0),
            "br-2477": self._order("OPEN", 0, 0),
            "br-2534": self._order("OPEN", 0, 0),
        }[oid]
        gw.get_open_orders.return_value = []

        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        row = live_ledger.get_trade(tid)
        self.assertAlmostEqual(row["qty_open"], 0.3)
        self.assertAlmostEqual(row["realized_pnl_usd"], (2440.0 - 2411.5) * 0.1)
        self.assertEqual(row["status"], "open")

    def test_first_target_moves_the_stop_to_breakeven(self) -> None:
        """End to end: bank TP1, and the runner stops risking the account."""
        tid = self._open_trade(exit_order_ids=["br-2440", "br-2477", "br-2534"])
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        gw.get_position.return_value = {"size": 0.3, "mark_price": 2450.0}
        gw.get_open_orders.return_value = []
        gw.place_bracket.side_effect = lambda **kw: {
            "order": {"order_id": f"new-{kw['limit_price']:g}"}
        }

        def order(oid):
            if oid == "br-2440":
                return {
                    "status": "FILLED",
                    "filled_size": "1",
                    "average_filled_price": "2440.0",
                }
            limit = "2477" if oid.endswith("2477") else "2534"
            return {
                "status": "OPEN",
                "filled_size": "0",
                "average_filled_price": "0",
                "order_configuration": {
                    "trigger_bracket_gtc": {
                        "base_size": "1" if limit == "2477" else "2",
                        "limit_price": limit,
                        "stop_trigger_price": "2385",
                    }
                },
            }

        gw.get_order.side_effect = order
        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        row = live_ledger.get_trade(tid)
        self.assertAlmostEqual(row["stop_loss"], 2411.5)
        self.assertEqual(
            json.loads(row["exit_order_ids_json"]), ["new-2477", "new-2534"]
        )
        for call in gw.place_bracket.call_args_list:
            self.assertEqual(call.kwargs["stop_trigger_price"], 2411.5)

    def test_a_stop_left_behind_is_caught_up_on_a_later_pass(self) -> None:
        """Self-heal: a target banked earlier still earns its trail."""
        tid = self._open_trade(exit_order_ids=["br-2477"])
        live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2440.0, pnl_usd=2.85,
            order_id="br-2440", reason="take_profit",
        )
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        gw.get_position.return_value = {"size": 0.3, "mark_price": 2450.0}
        gw.get_open_orders.return_value = []
        gw.place_bracket.return_value = {"order": {"order_id": "healed"}}
        gw.get_order.return_value = {
            "status": "OPEN",
            "filled_size": "0",
            "average_filled_price": "0",
            "order_configuration": {
                "trigger_bracket_gtc": {
                    "base_size": "3",
                    "limit_price": "2477",
                    "stop_trigger_price": "2385",
                }
            },
        }
        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        self.assertAlmostEqual(live_ledger.get_trade(tid)["stop_loss"], 2411.5)
        self.assertEqual(gw.place_bracket.call_args.kwargs["stop_trigger_price"], 2411.5)

    def _after_a_half_failed_retrail(self) -> tuple[int, MagicMock]:
        """A trade whose stop is already trailed but whose TP3 rung is bare.

        This is the state Eva #19 was left in: TP1 banked, the stop correctly
        at breakeven, one rung re-armed, and the other sitting on a fallback
        plain stop with no target above it.
        """
        tid = self._open_trade(
            exit_order_ids=["br-2477", "stop-fallback"], stop_loss=2411.5
        )
        live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2440.0, pnl_usd=2.85,
            order_id="br-2440", reason="take_profit",
        )
        gw = _gateway()
        gw.get_position.return_value = {"size": 0.3, "mark_price": 2450.0}
        gw.get_open_orders.return_value = []
        orders = {
            "br-2477": {
                "status": "OPEN", "filled_size": "0", "average_filled_price": "0",
                "order_id": "br-2477",
                "order_configuration": {
                    "trigger_bracket_gtc": {
                        "base_size": "1", "limit_price": "2477",
                        "stop_trigger_price": "2411.5",
                    }
                },
            },
            "stop-fallback": {
                "status": "OPEN", "filled_size": "0", "average_filled_price": "0",
                "order_id": "stop-fallback",
                "order_configuration": {
                    "stop_limit_stop_limit_gtc": {
                        "base_size": "2", "limit_price": "2387",
                        "stop_price": "2411.5",
                    }
                },
            },
        }
        cancelled: set[str] = set()
        placed = {"n": 0}

        def get_order(oid: str) -> dict:
            order = dict(orders[oid])
            if oid in cancelled:
                order["status"] = "CANCELLED"
            return order

        def place_stop(**kw) -> dict:
            # A restored fallback is a *new* resting order, so the next pass
            # must see it open — otherwise the heal looks healed for the wrong
            # reason and a retry cap can't be observed.
            placed["n"] += 1
            oid = f"stop-refallback-{placed['n']}"
            orders[oid] = {
                "status": "OPEN", "filled_size": "0", "average_filled_price": "0",
                "order_id": oid,
                "order_configuration": {
                    "stop_limit_stop_limit_gtc": {
                        "base_size": str(int(round(kw["amount"] / 0.1))),
                        "limit_price": "2387",
                        "stop_price": str(kw["trigger_price"]),
                    }
                },
            }
            return {"order": {"order_id": oid}}

        gw.get_order.side_effect = get_order
        gw.cancel_orders.side_effect = cancelled.update
        gw.place_stop_market.side_effect = place_stop
        return tid, gw

    def test_a_rung_stranded_on_a_bare_stop_gets_its_target_back(self) -> None:
        """The heal that matters: bare size can only reach breakeven.

        The old guard compared stop prices, and the fallback stop sets the
        price correctly — so this state looked healthy and the lost target was
        never recovered for the life of the trade.
        """
        tid, gw = self._after_a_half_failed_retrail()
        with patch.object(live_exec, "_SETTLE_SLEEP", 0), \
                patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        gw.cancel_orders.assert_called_once_with(["stop-fallback"])
        placed = gw.place_bracket.call_args_list
        self.assertEqual([c.kwargs["limit_price"] for c in placed], [2534.0])
        self.assertAlmostEqual(placed[0].kwargs["amount"], 0.2)
        self.assertEqual(placed[0].kwargs["stop_trigger_price"], 2411.5)
        ids = json.loads(live_ledger.get_trade(tid)["exit_order_ids_json"])
        # The healthy rung is untouched; only the bare one was swapped.
        self.assertEqual(ids, ["br-2477", "br-2534"])

    def test_the_heal_restores_the_stop_if_the_target_will_not_arm(self) -> None:
        """A re-arm that fails must not leave the size naked."""
        _tid, gw = self._after_a_half_failed_retrail()
        gw.place_bracket.side_effect = GatewayError("still rejected")
        with patch.object(live_exec, "_SETTLE_SLEEP", 0), \
                patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        gw.place_stop_market.assert_called_once()
        self.assertAlmostEqual(gw.place_stop_market.call_args.kwargs["amount"], 0.2)
        self.assertEqual(
            gw.place_stop_market.call_args.kwargs["trigger_price"], 2411.5
        )

    def test_the_heal_gives_up_rather_than_churning_the_stop_forever(self) -> None:
        """Each attempt reopens an uncovered window; a bare stop beats a gap.

        Without a cap this runs once a minute for the life of the trade, so a
        lost target would be traded for a permanently flickering stop.
        """
        tid, gw = self._after_a_half_failed_retrail()
        gw.place_bracket.side_effect = GatewayError("still rejected")
        with patch.object(live_exec, "_SETTLE_SLEEP", 0), \
                patch.object(live_exec, "get_gateway", return_value=gw):
            for _ in range(6):
                live_exec.sync_live_positions()

        self.assertEqual(
            gw.place_stop_market.call_count, live_exec._REARM_MAX_ATTEMPTS
        )
        self.assertEqual(live_exec._rearm_attempts[tid], live_exec._REARM_MAX_ATTEMPTS)

    def test_fully_armed_ladder_is_left_alone(self) -> None:
        """No bare stop, nothing to heal — and no orders churned every minute."""
        tid = self._open_trade(
            exit_order_ids=["br-2477", "br-2534"], stop_loss=2411.5
        )
        live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2440.0, pnl_usd=2.85,
            order_id="br-2440", reason="take_profit",
        )
        gw = _gateway()
        gw.get_position.return_value = {"size": 0.3, "mark_price": 2450.0}
        gw.get_open_orders.return_value = []
        gw.get_order.side_effect = lambda oid: {
            "status": "OPEN", "filled_size": "0", "average_filled_price": "0",
            "order_id": oid,
            "order_configuration": {
                "trigger_bracket_gtc": {
                    "base_size": "1", "limit_price": oid.split("-")[1],
                    "stop_trigger_price": "2411.5",
                }
            },
        }
        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        gw.cancel_orders.assert_not_called()
        gw.place_bracket.assert_not_called()

    def test_untouched_trade_keeps_its_structural_stop(self) -> None:
        tid = self._open_trade(exit_order_ids=["br-2477"])
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        gw.get_position.return_value = {"size": 0.4, "mark_price": 2420.0}
        gw.get_open_orders.return_value = []
        gw.get_order.return_value = {
            "status": "OPEN", "filled_size": "0", "average_filled_price": "0",
            "order_configuration": {
                "trigger_bracket_gtc": {
                    "base_size": "4", "limit_price": "2477",
                    "stop_trigger_price": "2385",
                }
            },
        }
        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        self.assertAlmostEqual(live_ledger.get_trade(tid)["stop_loss"], 2385.0)
        gw.place_bracket.assert_not_called()
        gw.cancel_orders.assert_not_called()

    def test_all_targets_filled_closes_at_weighted_average(self) -> None:
        tid = self._open_trade(exit_order_ids=["br-2440", "br-2477", "br-2534"])
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        gw.get_position.return_value = {"size": 0.0, "mark_price": 2534.0}
        gw.get_order.side_effect = lambda oid: {
            "br-2440": self._order("FILLED", 1, 2440.0),
            "br-2477": self._order("FILLED", 1, 2477.0),
            "br-2534": self._order("FILLED", 2, 2534.0),
        }[oid]
        gw.get_open_orders.return_value = []

        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        row = live_ledger.get_trade(tid)
        self.assertEqual(row["status"], "closed")
        expected_pnl = (
            (2440.0 - 2411.5) * 0.1
            + (2477.0 - 2411.5) * 0.1
            + (2534.0 - 2411.5) * 0.2
        )
        self.assertAlmostEqual(row["pnl_usd"], expected_pnl)
        # Weighted by size, so the 2-contract runner dominates.
        self.assertAlmostEqual(row["exit_price"], (2440 + 2477 + 2534 * 2) / 4)
        self.assertEqual(row["close_reason"], "take_profit")

    def test_unfilled_siblings_are_cancelled_on_close(self) -> None:
        """No reduce_only on this venue: a leftover exit could open a short."""
        tid = self._open_trade(exit_order_ids=["br-2440", "br-2534"])
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        gw.get_position.return_value = {"size": 0.0, "mark_price": 2385.0}
        gw.get_order.side_effect = lambda oid: {
            # Stop leg of the first bracket took the whole position out.
            "br-2440": self._order("FILLED", 4, 2385.0),
            "br-2534": self._order("OPEN", 0, 0),
        }[oid]
        gw.get_open_orders.return_value = []

        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        row = live_ledger.get_trade(tid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["close_reason"], "stop_loss")
        gw.cancel_orders.assert_any_call(["br-2440", "br-2534"])

    def test_trailed_stop_fill_is_not_tagged_take_profit(self) -> None:
        """P&L sign is the wrong signal once the stop has moved to TP1."""
        tid = self._open_trade(exit_order_ids=["br-2534"])
        live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2468.5, pnl_usd=5.7,
            order_id="br-2440", reason="take_profit",
        )
        live_ledger.record_partial_exit(
            tid, exit_qty=0.1, exit_price=2477.0, pnl_usd=6.55,
            order_id="br-2477", reason="take_profit",
        )
        live_ledger.set_stop_loss(tid, 2440.0)
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        gw.get_position.return_value = {"size": 0.0, "mark_price": 2439.5}
        gw.get_open_orders.return_value = []
        gw.get_order.return_value = {
            "status": "FILLED",
            "filled_size": "2",
            "average_filled_price": "2439.5",
            "order_configuration": {
                "trigger_bracket_gtc": {
                    "base_size": "2",
                    "limit_price": "2534",
                    "stop_trigger_price": "2440",
                }
            },
        }
        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        row = live_ledger.get_trade(tid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["close_reason"], "stop_loss")
        fills = json.loads(row["exit_fills_json"])
        self.assertEqual(fills["br-2534"]["reason"], "stop_loss")

    def test_manual_flatten_closes_the_remainder_at_mark(self) -> None:
        tid = self._open_trade(exit_order_ids=["br-2440"])
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        gw.get_position.return_value = {"size": 0.0, "mark_price": 2460.0}
        gw.get_order.return_value = self._order("OPEN", 0, 0)
        gw.get_open_orders.return_value = []

        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        row = live_ledger.get_trade(tid)
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["close_reason"], "exchange_close")
        self.assertAlmostEqual(row["pnl_usd"], (2460.0 - 2411.5) * 0.4)

    def test_unattributed_shortfall_is_flagged_not_guessed(self) -> None:
        """Two netted sleeves make a partial shortfall ambiguous."""
        self._open_trade(source="hq", qty=0.4, exit_order_ids=["br-a"])
        self._open_trade(source="mill", qty=0.1, exit_order_ids=["br-b"])
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        gw.get_position.return_value = {"size": 0.3, "mark_price": 2460.0}
        gw.get_order.return_value = self._order("OPEN", 0, 0)
        gw.get_open_orders.return_value = []

        with patch.object(live_exec, "get_gateway", return_value=gw), self.assertLogs(
            "execute", level="WARNING"
        ) as logs:
            live_exec.sync_live_positions()

        self.assertTrue(
            any("unattributed shortfall" in m for m in logs.output), logs.output
        )
        self.assertEqual(len(live_ledger.get_open_trades()), 2)

    def test_orphaned_resting_orders_are_cancelled(self) -> None:
        self._open_trade(exit_order_ids=["br-mine"])
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        gw.get_position.return_value = {"size": 0.4, "mark_price": 2450.0}
        gw.get_order.return_value = self._order("OPEN", 0, 0)
        gw.get_open_orders.return_value = [
            {"order_id": "br-mine"},
            {"order_id": "stale-stop"},
        ]

        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        gw.cancel_orders.assert_called_once_with(["stale-stop"])

    def test_reconcile_survives_an_unreadable_order(self) -> None:
        tid = self._open_trade(exit_order_ids=["br-a", "br-b"])
        gw = MagicMock()
        gw.contract_size.return_value = 0.1
        gw.get_position.return_value = {"size": 0.4, "mark_price": 2450.0}
        gw.get_order.side_effect = [
            GatewayError("order lookup failed"),
            self._order("OPEN", 0, 0),
        ]
        gw.get_open_orders.return_value = []

        with patch.object(live_exec, "get_gateway", return_value=gw):
            live_exec.sync_live_positions()

        self.assertEqual(live_ledger.get_trade(tid)["status"], "open")


if __name__ == "__main__":
    unittest.main()
