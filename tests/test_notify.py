"""Tests for Telegram notify formatting and broadcast policy helpers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from critic import AuditFinding, AuditVerdict, compose_rationale, build_market_context_block
from models import Suggestion
from notify import (
    build_caption,
    build_rationale_message,
    format_hourly_monitor_report,
    send_suggestion_to_chat,
)
import display_summary


def test_hourly_monitor_report_no_trade_skipped_broadcast():
    verdict = AuditVerdict(
        source="hourly",
        cycle_id="20260701T120000Z",
        action="no_trade",
        text_excerpt="HTF bearish, no valid entry.",
        deterministic=[],
        llm_hallucinations=[],
        llm_verified=["Bearish H4 structure cited correctly"],
    )
    text = format_hourly_monitor_report(verdict, broadcast_sent=False)
    assert "NO_TRADE" in text
    assert "Subscriber broadcast: skipped (no_trade)" in text
    assert "All deterministic fact-checks passed" in text
    assert "VERIFIED CLAIMS" in text


def test_hourly_monitor_report_trade_with_issues():
    verdict = AuditVerdict(
        source="hourly",
        cycle_id="20260701T130000Z",
        action="deriv_sell",
        text_excerpt="Short at M5 OB retest.",
        deterministic=[
            AuditFinding(code="M5_OB_MISLABEL", message="bounds wrong"),
        ],
        llm_hallucinations=[
            AuditFinding(code="LLM_HALLUCINATION", message="fake SFP"),
        ],
        sanitized=True,
    )
    text = format_hourly_monitor_report(verdict, broadcast_sent=True)
    assert "Subscriber broadcast: sent" in text
    assert "M5_OB_MISLABEL" in text
    assert "LLM_HALLUCINATION" in text
    assert "sanitized" in text.lower()


def test_hourly_monitor_report_shows_refine_metadata():
    verdict = AuditVerdict(
        source="hourly",
        cycle_id="20260701T140000Z",
        action="no_trade",
        text_excerpt="Audit downgrade.",
        deterministic=[],
        llm_hallucinations=[],
        sanitized=True,
        downgraded=True,
        passes_used=2,
    )
    text = format_hourly_monitor_report(verdict, broadcast_sent=False)
    assert "downgraded to no_trade" in text
    assert "Refine passes used: 2" in text


def test_rationale_message_why_then_market_context():
    context = build_market_context_block(
        ["Price inside bullish M5 OB (1,915.70-1,920.32) — wait for fib retest"]
    )
    thesis = (
        "Despite bullish M5 OB nearby, shorting bearish M5 OB fib — "
        "HTF is advisory only."
    )
    suggestion = Suggestion.from_dict(
        {
            "action": "spot_sell",
            "size": 0.5,
            "entry": 1918.0,
            "stop_loss": 1925.0,
            "take_profits": [1900.0],
            "rationale": compose_rationale(thesis, context),
        }
    )
    text = build_rationale_message(suggestion, "Paper PnL: n/a")
    assert "SPOT_SELL" in text
    assert "Why this trade:" in text
    assert "Market context:" in text
    assert text.index("Why this trade:") < text.index("Market context:")
    assert "Rationale:" not in text
    assert "Paper PnL: n/a" in text
    assert "Entry: 1,918.00" in text


def test_friendly_card_caption_short_and_pcts():
    suggestion = Suggestion(
        action="spot_sell",
        size=250.0,
        entry=65087.87,
        stop_loss=65723.40,
        take_profits=[64238.08, 63702.01],
        risk_reward=1.34,
        rationale="[Watchdog — m5_ob_fib]\n\nSetup.",
        product_id="BTC-USD",
    )
    original = suggestion.rationale
    caption = build_caption(
        suggestion,
        display_summary_text="Higher-timeframe bearish structure lines up with M5.",
        offer_id="20260721T120000Z_BTC",
    )
    assert suggestion.rationale == original
    assert caption.startswith("BTC Spot Sell")
    assert "Potential entry near $65,087.87" in caption
    assert "price move" in caption
    assert "Open a demo account" in caption or "Agent size" in caption
    assert "Accept within" in caption
    assert "Why this trade" not in caption
    assert "Market context" not in caption
    assert len(caption) <= 1024


def test_scale_in_card_wording():
    suggestion = Suggestion(
        action="spot_sell",
        size=250.0,
        entry=66215.18,
        stop_loss=66674.29,
        take_profits=[64059.61],
        risk_reward=1.5,
        rationale="[Watchdog — m5_ob_fib_add]\n\nScale-in at M5 OB fib 0.718.",
        product_id="BTC-USD",
        entry_tranche="0.718",
    )
    caption = build_caption(suggestion)
    assert "Adding near $66,215.18" in caption


def test_card_does_not_auto_send_full_rationale():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    suggestion = Suggestion(
        action="spot_buy",
        size=100.0,
        entry=2000.0,
        stop_loss=1900.0,
        take_profits=[2200.0],
        risk_reward=2.0,
        rationale=compose_rationale(
            "Long thesis here with lots of detail.",
            "Market context:\n• alert",
        ),
        product_id="ETH-USD",
    )
    asyncio.run(
        send_suggestion_to_chat(
            bot,
            123,
            suggestion,
            [],
            "Paper PnL: n/a",
            offer_id="20260721T120000Z_ETH",
            display_summary_text="Bullish structure aligns with M5 fib.",
        )
    )
    bot.send_message.assert_called_once()
    text = bot.send_message.call_args.kwargs["text"]
    assert "Why this trade" not in text
    assert "ETH Spot Buy" in text
    assert "Bullish structure aligns with M5 fib." in text


def test_display_summary_fallback_never_mutates_rationale():
    suggestion = Suggestion(
        action="spot_buy",
        size=100.0,
        entry=2000.0,
        stop_loss=1900.0,
        take_profits=[2200.0],
        rationale="Canonical audited thesis stays intact.",
        product_id="ETH-USD",
    )
    original = suggestion.rationale
    with patch.object(display_summary, "generate_llm_setup_blurb", return_value=None):
        summary = display_summary.generate_display_summary(suggestion)
    assert suggestion.rationale == original
    assert "order-block" in summary.lower() or "structure" in summary.lower()
    assert summary != original


def test_validate_llm_summary_rejects_numbers():
    assert display_summary._validate_llm_summary("Looks good at 65000.") is None
    ok = display_summary._validate_llm_summary(
        "Bearish structure on the higher timeframe lines up with a fib retest."
    )
    assert ok is not None
    assert "Bearish" in ok


def test_price_move_pcts_short():
    suggestion = Suggestion(
        action="spot_sell",
        size=1.0,
        entry=100.0,
        stop_loss=110.0,
        take_profits=[90.0],
        product_id="BTC-USD",
    )
    pcts = display_summary.price_move_pcts(suggestion)
    assert pcts is not None
    assert abs(pcts["tp_pct"] - 10.0) < 1e-9
    assert abs(pcts["sl_pct"] - 10.0) < 1e-9
