"""Tests for monitor agent deterministic verification."""

from __future__ import annotations

from critic import (
    AuditFinding,
    RefineResult,
    build_signals_block,
    chat_reply_is_market_commentary,
    compose_rationale,
    findings_require_retry,
    list_context_conflicts,
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
    context = "Market context:\n• 24h range established: 1,550-1,630"
    llm = "HTF bearish. No valid M5 SFP in window."
    full = compose_rationale(llm, context)
    body, block = split_rationale(full)
    assert block == context
    assert body == llm
    assert full.startswith(llm)
    assert "Market context:" in full


def test_split_legacy_signals_prefix():
    legacy = "Signals: 24h range established: 1,550-1,630\n\nHTF bearish."
    body, block = split_rationale(legacy)
    assert body == "HTF bearish."
    assert block is not None and block.startswith("Signals:")


def test_alert_text_not_audited_when_split():
    ctx = _base_context()
    context = build_signals_block(
        ["Price in bearish M5 OB fib zone 1,580.00-1,590.00"]
    )
    llm = "HTF structure bearish on H4. Waiting for setup."
    full = compose_rationale(llm, context)
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


def test_list_context_conflicts_short_vs_bullish_m5():
    ctx = _base_context(setup_tags=["m5_ob_bullish_no_fib"])
    notes = list_context_conflicts("spot_sell", ctx)
    assert any("bullish M5 OB" in n for n in notes)


def test_context_conflict_unacknowledged_on_short():
    ctx = _base_context(setup_tags=["m5_ob_bullish_no_fib"])
    suggestion = Suggestion.from_dict(
        {
            "action": "spot_sell",
            "size": 0.5,
            "entry": 1575.0,
            "stop_loss": 1595.0,
            "take_profits": [1550.0],
            "risk_reward": 1.2,
            "order_block": {
                "low": 1570.0,
                "high": 1590.0,
                "start_ts": "2026-06-28T08:00:00Z",
                "end_ts": "2026-06-28T08:00:00Z",
            },
        }
    )
    text = "Bearish M5 OB fib entry at 1575 — taking the short."
    findings = verify_deterministic(text, ctx, suggestion=suggestion)
    assert any(f.code == "CONTEXT_CONFLICT_UNACKNOWLEDGED" for f in findings)


def test_context_conflict_acknowledged_passes():
    ctx = _base_context(setup_tags=["m5_ob_bullish_no_fib"])
    suggestion = Suggestion.from_dict(
        {
            "action": "spot_sell",
            "size": 0.5,
            "entry": 1575.0,
            "stop_loss": 1595.0,
            "take_profits": [1550.0],
            "risk_reward": 1.2,
            "order_block": {
                "low": 1570.0,
                "high": 1590.0,
                "start_ts": "2026-06-28T08:00:00Z",
                "end_ts": "2026-06-28T08:00:00Z",
            },
        }
    )
    text = (
        "Despite price sitting in a bullish M5 OB, taking the short on a bearish "
        "M5 SFP — HTF is advisory only and M5 trigger takes precedence."
    )
    findings = verify_deterministic(text, ctx, suggestion=suggestion)
    assert not any(f.code == "CONTEXT_CONFLICT_UNACKNOWLEDGED" for f in findings)


def test_watchdog_rationale_skips_context_conflict():
    ctx = _base_context(setup_tags=["m5_ob_bullish_no_fib"])
    suggestion = Suggestion.from_dict(
        {
            "action": "spot_sell",
            "size": 0.5,
            "entry": 1575.0,
            "stop_loss": 1595.0,
            "take_profits": [1550.0],
            "risk_reward": 1.2,
            "order_block": {
                "low": 1570.0,
                "high": 1590.0,
                "start_ts": "2026-06-28T08:00:00Z",
                "end_ts": "2026-06-28T08:00:00Z",
            },
        }
    )
    text = "[Watchdog — m5_ob_fib_short]\n\nPrice at M5 OB fib 0.25 tranche."
    findings = verify_deterministic(text, ctx, suggestion=suggestion)
    assert not any(f.code == "CONTEXT_CONFLICT_UNACKNOWLEDGED" for f in findings)


# --- chat replies: only market commentary gets fact-checked -----------------

def test_chat_market_commentary_detection():
    assert chat_reply_is_market_commentary(
        "Price is sitting in a bullish M5 OB 1,554-1,586; no valid SFP yet."
    )
    # Account answers: hex address, balances, menu pointers — not market.
    assert not chat_reply_is_market_commentary(
        "Yes — your payout wallet 0x1234567890abcdef1234567890abcdef12345678 "
        "is registered (pending). Wallet: $1,250.00 undeployed."
    )
    assert not chat_reply_is_market_commentary(
        "You haven't registered one yet. Send /wallet 0x<your address>."
    )
    assert not chat_reply_is_market_commentary(
        "Sorry, something went wrong processing your message."
    )
    assert not chat_reply_is_market_commentary("")


def test_refine_chat_reply_hands_account_facts_to_the_critic(monkeypatch):
    """A history answer ("entry 70c…") is market-shaped enough to be audited;
    the user's account block must ride along as ground truth so true claims
    about their own trades are not flagged against the market snapshot."""
    import audit
    import critic

    captured: dict = {}

    def fake_audit_text(text, ctx, **kwargs):
        captured.update(kwargs)
        return critic.AuditVerdict(
            source="chat", user_id=42, text_excerpt=text[:40]
        )

    monkeypatch.setattr(critic, "audit_text", fake_audit_text)
    monkeypatch.setattr(
        audit, "get_snapshot",
        lambda cid: {"snapshot": {}, "suggestion": {},
                     "marked_chart_paths": {}, "cycle_id": "c1"},
    )
    monkeypatch.setattr(audit, "market_context_from_dict", lambda d: object())
    monkeypatch.setattr(audit, "suggestion_from_dict", lambda d: object())
    monkeypatch.setattr(audit, "log_chat_audit", lambda *a, **k: None)

    reply = (
        "Your last wick trade: entry 70c on KXBTCD, settled +$3.70. "
        "Lifetime on the lane: $-5.60 across 2 settled trades."
    )
    account = "=== Your account ===\nYour settled Kalshi trades..."
    out, verdict = critic.refine_chat_reply(
        42, "what did my past trades look like", reply,
        cycle_id="c1", account_context=account,
    )
    assert out == reply
    assert not verdict.sanitized
    assert captured.get("extra_ground_truth") == account


def test_verify_llm_review_includes_account_ground_truth(monkeypatch):
    """The extra ground-truth section reaches the LLM critic's prompt."""
    import critic

    sent: dict = {}

    class _Block:
        type = "text"
        text = '{"claims": []}'

    class _Resp:
        content = [_Block()]
        usage = None

    class _Messages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return _Resp()

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr(critic.anthropic, "Anthropic", _Client)
    monkeypatch.setattr(critic.analyze, "log_anthropic_usage", lambda *a, **k: None)

    ctx = _base_context()
    critic.verify_llm(
        "Your entry was 70c and it settled +$3.70.",
        ctx,
        extra_ground_truth="Your settled Kalshi trades: ...",
    )
    prompt = sent["messages"][0]["content"][0]["text"]
    assert "Authoritative user account facts" in prompt
    assert "Your settled Kalshi trades" in prompt
    # Without it, no account section appears.
    critic.verify_llm("Spot is at $1,600.", ctx)
    prompt2 = sent["messages"][0]["content"][0]["text"]
    assert "Authoritative user account facts" not in prompt2


def test_refine_chat_reply_passes_account_answers_through(monkeypatch):
    """A wallet answer must never be swapped for an SFP/OB market summary."""
    import audit
    import critic

    logged: list[tuple] = []
    monkeypatch.setattr(
        audit, "log_chat_audit",
        lambda uid, q, r, **kw: logged.append((uid, q, r)) or 1,
    )
    # Neither the snapshot lookup nor the LLM critic may run for this reply.
    monkeypatch.setattr(
        audit, "get_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("snapshot read")),
    )
    monkeypatch.setattr(
        audit, "get_latest_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("snapshot read")),
    )
    monkeypatch.setattr(
        critic, "verify_llm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM critic ran")),
    )

    reply = (
        "Yes — your payout wallet 0x1234567890abcdef1234567890abcdef12345678 "
        "is registered and verified."
    )
    out, verdict = critic.refine_chat_reply(
        42, "is my wallet registered", reply, cycle_id="c1"
    )
    assert out == reply
    assert not verdict.sanitized
    assert not verdict.has_issues
    assert logged == [(42, "is my wallet registered", reply)]

    err = "Sorry, something went wrong processing your message."
    out, verdict = critic.refine_chat_reply(42, "is my wallet registered", err)
    assert out == err
    assert not verdict.sanitized
