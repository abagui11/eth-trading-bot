"""Tests for monitor agent deterministic verification."""

from __future__ import annotations

from critic import (
    AuditFinding,
    RefineResult,
    build_signals_block,
    compose_rationale,
    findings_require_retry,
    refine_suggestion,
    sanitize_rationale,
    split_rationale,
    verify_deterministic,
)
from models import Suggestion
from patterns.htf_structure import HTFZone
from patterns.key_levels import KeyLevel
from patterns.market_context import MarketContext
from patterns.order_block import OrderBlock
from patterns.range_24h import Range24h
from patterns.zone_resolver import ZoneSnapshot


def _base_context(**overrides) -> MarketContext:
    zone = HTFZone(
        "order_block",
        "bullish",
        1554.47,
        1586.51,
        "2026-06-28T10:00:00Z",
    )
    ctx = MarketContext(
        range_24h=Range24h(
            high=1600.0,
            low=1500.0,
            mid=1550.0,
            width_pct=6.0,
            is_ranging=True,
            bars_in_range=20,
            start_ts="2026-06-28T00:00:00Z",
            end_ts="2026-06-29T00:00:00Z",
        ),
        is_ranging=True,
        range_break=None,
        spot=1569.0,
        zone_snapshot=ZoneSnapshot(
            spot=1569.0,
            zones_containing_price=[zone],
            primary_bullish=zone,
            primary_bearish=None,
            nearest_bearish_above=None,
            nearest_bullish_below=None,
            bearish_retest_low=1580.0,
            bearish_retest_high=1590.0,
        ),
        setup_state=None,
        order_blocks=[
            OrderBlock(
                direction="bullish",
                low=1570.0,
                high=1590.0,
                start_ts="2026-06-28T08:00:00Z",
                end_ts="2026-06-28T08:00:00Z",
                displacement_ts="2026-06-28T12:00:00Z",
            )
        ],
        htf_zones=[zone],
        key_levels_near=[
            KeyLevel(price=1569.40, label="Weekly Open", color="#D4AF37"),
        ],
        summary_text="test context",
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def test_m5_ob_mislabel_detects_h4_bounds():
    ctx = _base_context()
    text = "Entry on M5 OB 1554.47-1586.51 fib retest."
    findings = verify_deterministic(text, ctx)
    codes = {f.code for f in findings}
    assert "M5_OB_MISLABEL" in codes


def test_h4_sfp_not_found_when_none_in_snapshot():
    ctx = _base_context(h4_sfps=[])
    text = "H4 bullish SFP at Monday Low supports long bias."
    findings = verify_deterministic(text, ctx)
    assert any(f.code == "H4_SFP_NOT_FOUND" for f in findings)


def test_key_level_mismatch():
    ctx = _base_context()
    text = "Price rejected at Weekly Open 1,600.00."
    findings = verify_deterministic(text, ctx)
    assert any(f.code == "KEY_LEVEL_MISMATCH" for f in findings)


def test_retest_filled_conflict():
    ctx = _base_context()
    text = "Still waiting for a rally into the bearish retest zone."
    findings = verify_deterministic(text, ctx)
    assert any(f.code == "RETEST_STATUS_CONFLICT" for f in findings)


def test_no_false_positive_on_negated_sfp():
    ctx = _base_context(h4_sfps=[], m5_sfps=[])
    text = "No H4 SFP in the recent window — wait for structure."
    findings = verify_deterministic(text, ctx)
    assert not any(f.code.endswith("SFP_NOT_FOUND") for f in findings)


def test_valid_m5_ob_passes():
    ctx = _base_context()
    text = "M5 OB 1570-1590 fib entry aligns with H4 bullish OB."
    findings = verify_deterministic(text, ctx)
    assert not any(f.code == "M5_OB_MISLABEL" for f in findings)


def test_json_h4_as_m5_ob_via_suggestion():
    ctx = _base_context()
    suggestion = Suggestion.from_dict(
        {
            "action": "spot_buy",
            "size": 0.5,
            "entry": 1574.0,
            "stop_loss": 1550.0,
            "take_profits": [1600.0],
            "risk_reward": 2.0,
            "order_block": {
                "low": 1554.47,
                "high": 1586.51,
                "start_ts": "2026-06-28T10:00:00Z",
                "end_ts": "2026-06-28T10:00:00Z",
            },
        }
    )
    findings = verify_deterministic("H4 bullish bias.", ctx, suggestion=suggestion)
    assert any(f.code == "JSON_H4_AS_M5_OB" for f in findings)


def test_split_and_compose_rationale():
    signals = "Signals: 24h range established: 1,550-1,630"
    llm = "HTF bearish. No valid M5 SFP in window."
    full = compose_rationale(llm, signals)
    body, block = split_rationale(full)
    assert block == signals
    assert body == llm


def test_alert_text_not_audited_when_split():
    ctx = _base_context()
    signals = build_signals_block(
        ["Price in bearish M5 OB fib zone 1,580.00-1,590.00"]
    )
    llm = "HTF structure bearish on H4. Waiting for setup."
    full = compose_rationale(llm, signals)
    llm_body, _ = split_rationale(full)
    findings = verify_deterministic(llm_body, ctx)
    assert not any(f.code == "M5_OB_MISLABEL" for f in findings)


def test_findings_require_retry_on_critical_codes():
    findings = [
        AuditFinding(code="M5_SFP_NOT_FOUND", message="test"),
        AuditFinding(code="ENTRY_NOT_IN_RATIONALE", message="warn", severity="warning"),
    ]
    assert findings_require_retry(findings)


def test_findings_require_retry_false_on_warnings_only():
    findings = [
        AuditFinding(code="M5_OB_NOT_FOUND", message="test", severity="warning"),
    ]
    assert not findings_require_retry(findings)


def test_no_false_positive_on_fib_ratio_in_text():
    ctx = _base_context()
    text = "Entry requires fib 0.618-0.786 retest inside M5 OB."
    findings = verify_deterministic(text, ctx)
    assert not any(f.code == "M5_OB_NOT_FOUND" for f in findings)


def test_no_false_positive_on_small_key_level_numbers():
    ctx = _base_context()
    text = "Width 6.0% from Monday High area near 1,635."
    findings = verify_deterministic(text, ctx)
    assert not any(f.code == "KEY_LEVEL_MISMATCH" for f in findings)


def test_findings_require_retry_on_llm_hallucination():
    findings = [
        AuditFinding(code="LLM_HALLUCINATION", message="wrong spot"),
    ]
    assert findings_require_retry(findings)


def test_sanitize_rationale_uses_snapshot_only():
    ctx = _base_context(h4_sfps=[], m5_sfps=[])
    text = sanitize_rationale(ctx)
    assert "No valid M5 SFP" in text
    assert "M5 OB 1,569" not in text
    assert "1,554.47" not in text or "Primary H4" in text


def test_refine_suggestion_downgrades_failed_trade(monkeypatch):
    ctx = _base_context()
    trade = Suggestion.from_dict(
        {
            "action": "spot_buy",
            "size": 0.5,
            "entry": 1574.0,
            "stop_loss": 1550.0,
            "take_profits": [1600.0],
            "risk_reward": 2.0,
            "rationale": "Entry on M5 OB 1554.47-1586.51 fib retest.",
            "order_block": {
                "low": 1554.47,
                "high": 1586.51,
                "start_ts": "2026-06-28T10:00:00Z",
                "end_ts": "2026-06-28T10:00:00Z",
            },
            "structure_chart": "H4",
            "entry_chart": "M5",
        }
    )

    def _fake_propose(*_args, **_kwargs):
        return trade

    monkeypatch.setattr("critic.analyze.propose_trade", _fake_propose)
    monkeypatch.setattr("critic.verify_llm", lambda *_a, **_k: ([], []))
    monkeypatch.setattr("critic.bot_config.MAX_REFINE_PASSES", 0)

    result = refine_suggestion(trade, ctx, {}, "guide", run_llm_critic=False)
    assert isinstance(result, RefineResult)
    assert result.downgraded is True
    assert result.suggestion.action == "no_trade"
    assert result.suggestion.order_block is None
