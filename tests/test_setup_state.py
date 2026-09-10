"""Tests for setup state machine and zone resolver."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

import config
from patterns.htf_structure import HTFZone
from patterns.setup_state import (
    SETUP_STATE_KEY,
    SetupState,
    load_setup_state,
    save_setup_state,
    update_bearish_retest_state,
)
from patterns.signal_state import get_state, set_state
from patterns.zone_resolver import resolve_zones


class ZoneResolverTests(unittest.TestCase):
    def test_resolve_primary_bearish_at_price(self) -> None:
        zones = [
            HTFZone("order_block", "bearish", 1564.0, 1658.0, "2026-06-20T00:00:00Z"),
            HTFZone("order_block", "bullish", 1554.0, 1586.0, "2026-06-28T08:00:00Z"),
        ]
        snap = resolve_zones(1575.0, zones)
        self.assertIsNotNone(snap.primary_bearish)
        self.assertIsNotNone(snap.primary_bullish)
        self.assertIsNotNone(snap.bearish_retest_low)
        self.assertGreater(snap.bearish_retest_low, 1564.0)


class SetupStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_path = self._tmpdir.name + "/ledger.db"
        self._patch = patch.object(config, "LEDGER_DB", self._db_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_retest_filled_when_24h_high_tags_zone(self) -> None:
        state, alerts, tags = update_bearish_retest_state(
            spot=1575.0,
            range_high_24h=1624.0,
            retest_low=1610.0,
            retest_high=1658.0,
            htf_bearish_bias=True,
            recent_bearish_m5_sfp=False,
        )
        self.assertEqual(state.phase, "bearish_retest_filled")
        self.assertTrue(any("BEARISH RETEST FILLED" in a for a in alerts))
        self.assertIn("bearish_retest_filled", tags)

    def test_short_trigger_on_rejection(self) -> None:
        save_setup_state(
            SetupState(
                phase="bearish_retest_filled",
                retest_low=1610.0,
                retest_high=1658.0,
                tagged_high=1624.0,
                tagged_ts="2026-06-30T14:00:00Z",
            )
        )
        state, alerts, tags = update_bearish_retest_state(
            spot=1575.0,
            range_high_24h=1624.0,
            retest_low=1610.0,
            retest_high=1658.0,
            htf_bearish_bias=True,
            recent_bearish_m5_sfp=True,
        )
        self.assertEqual(state.phase, "bearish_retest_rejected")
        self.assertIn("short_trigger_retest", tags)

    def test_retest_state_is_per_product(self) -> None:
        """One product's latched retest phase must not leak into another's."""
        eth, _, _ = update_bearish_retest_state(
            spot=1575.0,
            range_high_24h=1624.0,
            retest_low=1610.0,
            retest_high=1658.0,
            htf_bearish_bias=True,
            product_id="ETH-USD",
        )
        self.assertEqual(eth.phase, "bearish_retest_filled")

        # BTC has no bearish zone this cycle, so it must read as idle.
        btc, alerts, tags = update_bearish_retest_state(
            spot=77_000.0,
            range_high_24h=78_000.0,
            retest_low=None,
            retest_high=None,
            htf_bearish_bias=False,
            product_id="BTC-USD",
        )
        self.assertEqual(btc.phase, "idle")
        self.assertEqual(alerts, [])
        self.assertEqual(tags, [])

        # ETH keeps its own latched zone rather than BTC's reset.
        self.assertEqual(load_setup_state("ETH-USD").phase, "bearish_retest_filled")
        self.assertEqual(load_setup_state("ETH-USD").retest_low, 1610.0)
        self.assertEqual(load_setup_state("BTC-USD").phase, "idle")

    def test_unscoped_state_is_isolated_from_product_state(self) -> None:
        save_setup_state(SetupState(phase="bearish_retest_filled"), "ETH-USD")
        self.assertEqual(load_setup_state().phase, "idle")
        self.assertIsNone(get_state(SETUP_STATE_KEY))


if __name__ == "__main__":
    unittest.main()
