"""Tests for analyze order_block / entry validation."""

from __future__ import annotations

import pytest

from analyze import _validate
from patterns.market_context import MarketContext
from patterns.htf_structure import HTFZone
from patterns.order_block import OrderBlock


def _trade_payload(**overrides):
    base = {
        "action": "spot_buy",
        "size": 0.5,
        "entry": 2395.0,
        "stop_loss": 2350.0,
        "take_profits": [2500.0],
        "risk_reward": 2.0,
        "rationale": "M5 OB fib entry.",
        "structure_chart": "H4",
        "entry_chart": "M5",
        "order_block": {
            "low": 2380.0,
            "high": 2420.0,
            "start_ts": "2026-06-20T12:00:00Z",
            "end_ts": "2026-06-20T12:00:00Z",
        },
    }
    base.update(overrides)
    return base


def test_validate_rejects_entry_outside_fib_zone():
    with pytest.raises(ValueError, match="outside M5 OB fib"):
        _validate(_trade_payload(entry=2500.0))


def test_validate_rejects_narrow_order_block():
    with pytest.raises(ValueError, match="below minimum 1.25%"):
        _validate(_trade_payload(order_block={
            "low": 2400.0,
            "high": 2405.0,
            "start_ts": "2026-06-20T12:00:00Z",
            "end_ts": "2026-06-20T12:00:00Z",
        }))


def test_validate_rejects_h4_bounds_as_order_block():
    m5_ob = OrderBlock(
        direction="bullish",
        low=1570.0,
        high=1590.0,
        start_ts="2026-06-28T08:00:00Z",
        end_ts="2026-06-28T08:00:00Z",
        displacement_ts="2026-06-28T12:00:00Z",
    )
    ctx = MarketContext(
        range_24h=None,
        is_ranging=False,
        range_break=None,
        spot=1569.0,
        zone_snapshot=None,
        setup_state=None,
        order_blocks=[m5_ob],
        htf_zones=[
            HTFZone(
                "order_block",
                "bullish",
                1554.47,
                1586.51,
                "2026-06-28T10:00:00Z",
            )
        ],
    )
    payload = _trade_payload(
        entry=1574.5,
        order_block={
            "low": 1554.47,
            "high": 1586.51,
            "start_ts": "2026-06-28T10:00:00Z",
            "end_ts": "2026-06-28T10:00:00Z",
        },
    )
    with pytest.raises(ValueError, match="matches H4 OB"):
        _validate(payload, market_context=ctx)


def test_validate_accepts_matching_m5_ob_and_fib_entry():
    m5_ob = OrderBlock(
        direction="bullish",
        low=2380.0,
        high=2420.0,
        start_ts="2026-06-20T12:00:00Z",
        end_ts="2026-06-20T12:00:00Z",
        displacement_ts="2026-06-20T14:00:00Z",
    )
    ctx = MarketContext(
        range_24h=None,
        is_ranging=False,
        range_break=None,
        spot=2395.0,
        zone_snapshot=None,
        setup_state=None,
        order_blocks=[m5_ob],
    )
    s = _validate(_trade_payload(), market_context=ctx)
    assert s.action == "spot_buy"
    assert s.entry == 2395.0
