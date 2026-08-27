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
        self.assertTrue(pub["mill_excluded"])
        self.assertEqual(pub["hold_horizon"], "swing")

    def test_admits_eth_at_half_nav(self) -> None:
        decision = vault.propose(_sug(), open_rows=[])
        self.assertTrue(decision["admitted"])
        self.assertAlmostEqual(decision["notional_usd"], 1000.0)
        self.assertAlmostEqual(decision["qty"], 0.5)

    def test_no_trade_and_scale_in_skipped(self) -> None:
        self.assertFalse(vault.propose(_sug(action="no_trade"), open_rows=[])["admitted"])
        self.assertEqual(
            vault.propose(
                _sug(entry_tranche=str(bot_config.ADD_FIB_LEVEL)),
                open_rows=[],
            )["skip_reason"],
            "scale_in",
        )

    def test_one_name_per_product(self) -> None:
        open_eth = [{"cycle_id": "c0", "product_id": "ETH-USD", "notional_usd": 1000.0}]
        decision = vault.propose(_sug(), open_rows=open_eth)
        self.assertFalse(decision["admitted"])
        self.assertIn("product_open", decision["skip_reason"])

    def test_heat_cap(self) -> None:
        opens = [
            {"cycle_id": "c0", "product_id": "ETH-USD", "notional_usd": 1000.0},
            {"cycle_id": "c1", "product_id": "BTC-USD", "notional_usd": 1000.0},
        ]
        decision = vault.propose(_sug(product_id="ETH-USD"), open_rows=opens)
        self.assertFalse(decision["admitted"])
        self.assertIn(decision["skip_reason"], ("sleeve_full", "heat_cap", "product_open:ETH-USD"))

    def test_take_is_idempotent_and_streams(self) -> None:
        first = vault.take(_sug(), cycle_id="20260827T120000Z_ETH", title="High Quality · ETH")
        self.assertTrue(first["admitted"])
        second = vault.take(_sug(), cycle_id="20260827T120000Z_ETH")
        self.assertEqual(first["id"], second["id"])
        feed = vault.stream()
        self.assertEqual(len(feed["ideas"]), 1)
        self.assertEqual(feed["ideas"][0]["rail"], "hq")
        self.assertEqual(feed["ideas"][0]["notional_usd"], 1000.0)
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
