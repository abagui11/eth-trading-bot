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


class LedgerDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "ledger.db"
        self._patch = patch.object(config, "LEDGER_DB", db)
        self._patch.start()
        live_ledger.init_db()
        self.addCleanup(self._patch.stop)
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
