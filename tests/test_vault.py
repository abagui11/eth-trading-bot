"""HQ vault admit/size rules — mill never enters, ICT math unchanged."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_config
import config
import live_ledger
import vault
from models import Suggestion


def _sug(**overrides) -> Suggestion:
    base = dict(
        action="deriv_buy",
        size=0.5,
        entry=2000.0,
        stop_loss=1940.0,
        take_profits=[2060.0, 2120.0],
        risk_reward=2.0,
        rationale="ict",
        product_id="ETH-USD",
    )
    base.update(overrides)
    return Suggestion(**base)


class VaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmpdir.name) / "ledger.db"
        self._patch = patch.object(config, "LEDGER_DB", db)
        self._patch.start()
        live_ledger.init_db()
        vault.init_db()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_policy_matches_live_hq_sleeve(self) -> None:
        p = vault.policy()
        self.assertEqual(p.nav_usd, bot_config.LIVE_HQ_EQUITY_USD)
        self.assertEqual(p.deploy_pct, bot_config.LIVE_TRADE_DEPLOY_PCT)
        pub = vault.policy_public(p)
        self.assertEqual(pub["notional_per_name_usd"], 1000.0)
        self.assertEqual(pub["risk_per_name_usd"], 14.0)
        self.assertTrue(pub["mill_excluded"])
        self.assertEqual(pub["hold_horizon"], "swing")

    def test_clip_is_sized_to_the_risk_budget(self) -> None:
        # Budget pinned so the arithmetic stays readable and the test survives
        # retuning: $10 against a 25-point stop is $2.50 a nano, so 4 fit.
        # Notional follows from that, not the reverse.
        with patch.object(bot_config, "LIVE_HQ_RISK_PCT", 0.005):
            decision = vault.propose(
                _sug(entry=2400.0, stop_loss=2375.0, take_profits=[2450.0]),
                open_rows=[],
            )
        self.assertTrue(decision["admitted"])
        self.assertAlmostEqual(decision["qty"], 0.4)
        self.assertAlmostEqual(decision["risk_usd"], 10.0)
        self.assertAlmostEqual(decision["notional_usd"], 960.0)

    def test_a_wider_stop_buys_a_smaller_clip_not_more_risk(self) -> None:
        """The premise the stop study's R-multiples rest on.

        At constant dollar risk a 2x stop is a 0.5x position, so widening a
        stop costs upside rather than adding exposure. Size any other way and
        the measured R-multiples do not carry over to live.
        """
        with patch.object(bot_config, "LIVE_HQ_RISK_PCT", 0.005):
            tight = vault.propose(
                _sug(entry=2400.0, stop_loss=2375.0, take_profits=[2450.0]),
                open_rows=[],
            )
            wide = vault.propose(
                _sug(entry=2400.0, stop_loss=2350.0, take_profits=[2450.0]),
                open_rows=[],
            )
        self.assertAlmostEqual(tight["qty"], 0.4)
        self.assertAlmostEqual(wide["qty"], 0.2)
        self.assertAlmostEqual(tight["risk_usd"], wide["risk_usd"])

    def test_risk_is_measured_off_the_fill_not_the_plan(self) -> None:
        # Plan risks 25 points, but the market order fills 25 above it, so the
        # real distance to the untouched stop is 50 and the clip halves.
        with patch.object(bot_config, "LIVE_HQ_RISK_PCT", 0.005):
            decision = vault.propose(
                _sug(entry=2400.0, stop_loss=2375.0, take_profits=[2450.0]),
                spot=2425.0,
                open_rows=[],
            )
        self.assertTrue(decision["admitted"])
        self.assertAlmostEqual(decision["qty"], 0.2)
        self.assertAlmostEqual(decision["risk_usd"], 10.0)

    def test_btc_admits_one_contract_when_the_stop_is_tight_enough(self) -> None:
        decision = vault.propose(
            _sug(product_id="BTC-USD", entry=80000.0, stop_loss=79200.0,
                 take_profits=[81000.0]),
            open_rows=[],
        )
        self.assertTrue(decision["admitted"])
        self.assertAlmostEqual(decision["qty"], 0.01)
        self.assertAlmostEqual(decision["notional_usd"], 800.0)

    def test_refuses_when_one_contract_exceeds_the_budget(self) -> None:
        # BTC's smallest tradeable size is one nano. When that alone risks more
        # than the budget there is no clip small enough, so the idea is dropped
        # rather than taken at the wrong size.
        with patch.object(bot_config, "LIVE_HQ_RISK_PCT", 0.005):
            decision = vault.propose(
                _sug(product_id="BTC-USD", entry=80000.0, stop_loss=78900.0,
                     take_profits=[81000.0]),
                open_rows=[],
            )
        self.assertFalse(decision["admitted"])
        self.assertIn("risk_cap", decision["skip_reason"])

    def test_shipped_budget_admits_the_btc_stop_that_prompted_the_raise(self) -> None:
        """Cycle 20260902T181231Z: spot 77,387 against a stop at 76,027.

        One nano risked $13.60 there and $10 refused it. The raise to 0.7%
        exists to admit that trade, so it is asserted against the shipped
        config rather than a pinned one.
        """
        decision = vault.propose(
            _sug(product_id="BTC-USD", entry=76652.0, stop_loss=76027.0,
                 take_profits=[77369.59]),
            spot=77387.44,
            open_rows=[],
        )
        self.assertTrue(decision["admitted"], decision.get("skip_reason"))
        self.assertAlmostEqual(decision["qty"], 0.01)
        # The whole point of the raise: this nano fits $14 and did not fit $10.
        self.assertGreater(decision["risk_usd"], 10.0)
        self.assertLessEqual(decision["risk_usd"], 14.0)

    def test_clip_trims_to_sleeve_headroom_instead_of_refusing(self) -> None:
        # A stop tight enough to afford 20 nanos still has to fit the sleeve.
        # Trimming keeps the idea tradeable where a flat heat cap refused it.
        opens = [{"cycle_id": "c0", "product_id": "BTC-USD", "notional_usd": 1600.0}]
        with patch.object(bot_config, "LIVE_MAX_LEVERAGE", 1.0):
            decision = vault.propose(
                _sug(entry=2400.0, stop_loss=2395.0, take_profits=[2450.0]),
                open_rows=opens,
            )
        self.assertTrue(decision["admitted"])
        self.assertAlmostEqual(decision["qty"], 0.1)
        self.assertLessEqual(decision["notional_usd"], 400.0)

    def test_no_trade_and_scale_in_skipped(self) -> None:
        self.assertFalse(vault.propose(_sug(action="no_trade"), open_rows=[])["admitted"])
        self.assertEqual(
            vault.propose(
                _sug(entry_tranche=str(bot_config.ADD_FIB_LEVEL)),
                open_rows=[],
            )["skip_reason"],
            "scale_in",
        )

    def test_per_product_cap_blocks_the_next_clip(self) -> None:
        open_eth = [{"cycle_id": "c0", "product_id": "ETH-USD", "notional_usd": 1000.0}]
        with patch.object(bot_config, "LIVE_MAX_PER_PRODUCT_HQ", 1):
            decision = vault.propose(_sug(), open_rows=open_eth)
        self.assertFalse(decision["admitted"])
        self.assertIn("product_open", decision["skip_reason"])

    def test_per_product_cap_admits_up_to_its_limit(self) -> None:
        open_eth = [{"cycle_id": "c0", "product_id": "ETH-USD", "notional_usd": 200.0}]
        with patch.object(bot_config, "LIVE_MAX_PER_PRODUCT_HQ", 2):
            decision = vault.propose(_sug(), open_rows=open_eth)
        self.assertTrue(decision["admitted"])

    def test_heat_cap(self) -> None:
        opens = [
            {"cycle_id": "c0", "product_id": "ETH-USD", "notional_usd": 1000.0},
            {"cycle_id": "c1", "product_id": "BTC-USD", "notional_usd": 1000.0},
        ]
        with patch.object(bot_config, "LIVE_MAX_LEVERAGE", 1.0):
            decision = vault.propose(_sug(product_id="ETH-USD"), open_rows=opens)
        self.assertFalse(decision["admitted"])
        self.assertIn(decision["skip_reason"], ("sleeve_full", "heat_cap", "product_open:ETH-USD"))

    def test_take_is_idempotent_and_streams(self) -> None:
        with patch.object(bot_config, "LIVE_HQ_RISK_PCT", 0.005):
            first = vault.take(_sug(), cycle_id="20260827T120000Z_ETH", title="High Quality · ETH")
            self.assertTrue(first["admitted"])
            second = vault.take(_sug(), cycle_id="20260827T120000Z_ETH")
        self.assertEqual(first["id"], second["id"])
        feed = vault.stream()
        self.assertEqual(len(feed["ideas"]), 1)
        self.assertEqual(feed["ideas"][0]["rail"], "hq")
        self.assertEqual(feed["ideas"][0]["notional_usd"], 200.0)
        self.assertTrue(feed["ideas"][0]["followable"])

    def test_follow_while_open_then_duplicate(self) -> None:
        row = vault.take(_sug(), cycle_id="c-follow")
        result = vault.follow(int(row["id"]), 42, "accept")
        self.assertEqual(result["status"], "recorded")
        dup = vault.follow(int(row["id"]), 42, "reject")
        self.assertEqual(dup["status"], "duplicate")
        stream = vault.stream(user_id=42)
        self.assertEqual(stream["ideas"][0]["my_decision"], "accept")

    def test_skipped_cycles_do_not_hit_the_feed(self) -> None:
        vault.take(_sug(action="no_trade"), cycle_id="c-skip")
        self.assertEqual(vault.stream()["ideas"], [])
