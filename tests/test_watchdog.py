"""Tests for programmatic watchdog entry scanner."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import config
from models import Suggestion
from patterns.htf_structure import HTFZone
from patterns.market_context import MarketContext
from patterns.order_block import OrderBlock, fib_zone_bounds
from patterns.zone_resolver import ZoneSnapshot
from watchdog import (
    WatchdogTrigger,
    _build_rationale,
    _is_on_cooldown,
    _record_fire,
    build_suggestion,
    evaluate_triggers,
)


def _bar(ts: str, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 100.0}


def _h12_bars() -> list[dict]:
    base = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    bars = []
    price = 2400.0
    for i in range(40):
        ts = (base + timedelta(hours=12 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        swing = 30 if i % 4 == 0 else -20
        price += swing
        bars.append(_bar(ts, price, price + 15, price - 15, price))
    return bars


def _bullish_ob() -> OrderBlock:
    return OrderBlock(
        direction="bullish",
        low=2380.0,
        high=2420.0,
        start_ts="2026-06-20T12:00:00Z",
        end_ts="2026-06-20T12:00:00Z",
        displacement_ts="2026-06-20T14:00:00Z",
    )


def _bearish_ob() -> OrderBlock:
    return OrderBlock(
        direction="bearish",
        low=2580.0,
        high=2620.0,
        start_ts="2026-06-25T12:00:00Z",
        end_ts="2026-06-25T12:00:00Z",
        displacement_ts="2026-06-25T14:00:00Z",
    )


def _bullish_ctx(spot: float) -> MarketContext:
    ob = _bullish_ob()
    z_low, z_high = fib_zone_bounds("bullish", ob.low, ob.high)
    assert z_low <= spot <= z_high
    h12_zone = HTFZone(
        "order_block",
        "bullish",
        ob.low,
        ob.high,
        "2026-06-20T10:00:00Z",
    )
    snap = ZoneSnapshot(
        spot=spot,
        zones_containing_price=[h12_zone],
        primary_bullish=h12_zone,
        primary_bearish=None,
        nearest_bearish_above=None,
        nearest_bullish_below=None,
        bearish_retest_low=None,
        bearish_retest_high=None,
    )
    return MarketContext(
        range_24h=None,
        is_ranging=False,
        range_break=None,
        spot=spot,
        zone_snapshot=snap,
        setup_state=None,
        order_blocks=[ob],
        htf_zones=[h12_zone],
        setup_tags=["h1_ob_bullish_in_fib"],
    )


class WatchdogTriggerTests(unittest.TestCase):
    def test_evaluate_long_fib_trigger(self) -> None:
        spot = 2408.0
        ctx = _bullish_ctx(spot)
        h1 = [_bar("2026-06-30T18:00:00Z", spot, spot + 5, spot - 5, spot)]
        triggers = evaluate_triggers(ctx, h1)
        self.assertTrue(any(t.name == "h1_ob_fib_long" for t in triggers))

    def test_evaluate_short_retest_trigger(self) -> None:
        ob = _bearish_ob()
        spot = 2592.0
        h12_zone = HTFZone(
            "order_block",
            "bearish",
            ob.low,
            ob.high,
            "2026-06-25T10:00:00Z",
        )
        snap = ZoneSnapshot(
            spot=spot,
            zones_containing_price=[h12_zone],
            primary_bullish=None,
            primary_bearish=h12_zone,
            nearest_bearish_above=None,
            nearest_bullish_below=None,
            bearish_retest_low=2585.0,
            bearish_retest_high=2620.0,
        )
        ctx = MarketContext(
            range_24h=None,
            is_ranging=False,
            range_break=None,
            spot=spot,
            zone_snapshot=snap,
            setup_state=None,
            order_blocks=[ob],
            htf_zones=[h12_zone],
            setup_tags=["short_trigger_retest", "h1_ob_bearish_in_fib"],
        )
        h1 = [_bar("2026-06-30T18:00:00Z", spot, spot + 5, spot - 5, spot)]
        triggers = evaluate_triggers(ctx, h1)
        self.assertTrue(any(t.name == "short_trigger_retest" for t in triggers))

    def test_build_suggestion_passes_validation(self) -> None:
        spot = 2408.0
        ctx = _bullish_ctx(spot)
        trigger = WatchdogTrigger(
            name="h1_ob_fib_long",
            direction="bullish",
            ob=_bullish_ob(),
            reason="test",
            priority=70,
        )
        suggestion = build_suggestion(trigger, ctx, _h12_bars())
        self.assertEqual(suggestion.action, "spot_buy")
        self.assertIsNotNone(suggestion.risk_reward)
        self.assertGreaterEqual(suggestion.risk_reward or 0, 1.0)
        self.assertIn("[Watchdog — h1_ob_fib_long]", suggestion.rationale)
        self.assertIn("H12 bullish zone", suggestion.rationale)
        self.assertIn("H1 OB coincides with H12 OB", suggestion.rationale)

    def test_build_rationale_includes_signals_block(self) -> None:
        spot = 2408.0
        ctx = _bullish_ctx(spot)
        ctx.alerts.append("Price in bullish H1 OB fib zone")
        trigger = WatchdogTrigger(
            name="h1_ob_fib_long",
            direction="bullish",
            ob=_bullish_ob(),
            reason="Aligned long setup",
            priority=70,
        )
        rationale = _build_rationale(trigger, ctx, _bullish_ob(), spot)
        self.assertIn("Signals:", rationale)
        self.assertIn("HTF context:", rationale)
        self.assertIn("Aligned long setup", rationale)

    def test_htf_conflict_blocks_long(self) -> None:
        spot = 2408.0
        ctx = _bullish_ctx(spot)
        ctx.setup_tags.append("htf_zone_conflict")
        h1 = [_bar("2026-06-30T18:00:00Z", spot, spot + 5, spot - 5, spot)]
        triggers = evaluate_triggers(ctx, h1)
        self.assertFalse(any(t.name == "h1_ob_fib_long" for t in triggers))


class WatchdogCooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_path = self._tmpdir.name + "/ledger.db"
        self._patch = patch.object(config, "LEDGER_DB", self._db_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_cooldown_blocks_repeat(self) -> None:
        trigger = WatchdogTrigger(
            name="h1_ob_fib_long",
            direction="bullish",
            ob=_bullish_ob(),
            reason="test",
            priority=70,
        )
        key = f"{trigger.name}:{trigger.ob.displacement_ts}:"
        self.assertFalse(_is_on_cooldown(key))
        _record_fire(key, "WDTEST001")
        self.assertTrue(_is_on_cooldown(key))


class WatchdogNotifyTests(unittest.TestCase):
    def test_watchdog_caption_prefix(self) -> None:
        from notify import build_caption

        suggestion = Suggestion(
            action="spot_buy",
            size=0.5,
            entry=2400.0,
            stop_loss=2350.0,
            take_profits=[2500.0],
            risk_reward=2.0,
            rationale="[Watchdog — h1_ob_fib_long]\n\nSetup.",
        )
        caption = build_caption(suggestion)
        self.assertTrue(caption.startswith("WATCHDOG — SPOT_BUY"))

    def test_send_suggestion_handles_empty_paths(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from notify import send_suggestion_to_chat

        bot = MagicMock()
        bot.send_message = AsyncMock()
        suggestion = Suggestion(
            action="spot_buy",
            size=0.5,
            entry=2400.0,
            stop_loss=2350.0,
            take_profits=[2500.0],
            risk_reward=2.0,
            rationale="test",
        )

        asyncio.run(
            send_suggestion_to_chat(bot, 123, suggestion, [], "PnL footer")
        )
        bot.send_message.assert_called_once()
