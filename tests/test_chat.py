"""Tests for chat snapshot grounding and per-user pool context."""

from __future__ import annotations

from unittest.mock import patch

import analyze
import chat


def test_build_context_includes_snapshot_summary():
    snapshot = {
        "cycle_id": "20260702T120000Z",
        "snapshot": {
            "spot": 1615.0,
            "summary_text": "=== Programmatic market context ===\nCurrent spot: $1,615.00",
            "alerts": [],
            "h4_sfps": [],
            "m5_sfps": [],
            "live_invalidated_sfps": [],
            "order_blocks": [],
            "htf_zones": [],
            "key_levels_near": [],
            "setup_tags": [],
            "is_ranging": False,
            "range_break": None,
        },
        "marked_chart_paths": {"H4": "/tmp/fake_h4.png"},
    }

    with patch("chat.audit.get_latest_snapshot", return_value=snapshot), \
         patch("chat.ledger.get_latest_suggestion", return_value=None), \
         patch("chat.ledger.get_latest_trade_suggestion", return_value=None), \
         patch("chat.ledger.get_latest", return_value=[]), \
         patch("chat.ledger.search_rationale", return_value=[]):
        text, _chart_path, snapshot_charts = chat._build_context(1615.0, "What is the bias?")

    assert "Authoritative cycle snapshot" in text
    assert "Programmatic market context" in text
    assert snapshot_charts.get("H4") == "/tmp/fake_h4.png"
    assert "Open paper positions" not in text
    assert "Paper PnL" not in text
    assert "Closed paper trades" not in text


def test_build_context_excludes_house_paper_and_includes_user_account():
    portfolio = {
        "ok": True,
        "wallet_usd": 200.0,
        "deployed_usd": 300.0,
        "cash_usd": 500.0,
        "total_usd": 500.0,
        "reserved_usd": 0.0,
        "realized_pnl_usd": 12.5,
        "unrealized_pnl_usd": -3.0,
        "open_stakes": [],
        "closed_stakes": [],
        "kalshi_open": [],
        "kalshi_closed": [],
        "frozen": False,
    }
    wallet = {"address": "0xabc123", "status": "verified"}

    with patch("chat.bot_config.POOL_ENABLED", True), \
         patch("chat.audit.get_latest_snapshot", return_value=None), \
         patch("chat.ledger.get_latest_suggestion", return_value=None), \
         patch("chat.ledger.get_latest_trade_suggestion", return_value=None), \
         patch("chat.ledger.get_latest", return_value=[]), \
         patch("chat.ledger.search_rationale", return_value=[]), \
         patch("pool.access_status", return_value="approved"), \
         patch("pool.get_wallet", return_value=wallet), \
         patch("pool.get_wallet_change_request", return_value=None), \
         patch("pool.portfolio", return_value=portfolio), \
         patch("pool.strategy_subscriptions", return_value=["mill"]), \
         patch("pool.allocations", return_value={"mill": 300.0}):
        text, _chart, _charts = chat._build_context(
            2000.0, "is my wallet registered?", telegram_id=4242,
        )

    assert "=== Your account ===" in text
    assert "Access: approved" in text
    assert "0xabc123" in text
    assert "verified" in text
    assert "Trade Mill" in text or "mill" in text
    assert "$300.00" in text
    assert "Open paper positions" not in text
    assert "Paper PnL" not in text


def test_format_user_account_pending_access():
    with patch("chat.bot_config.POOL_ENABLED", True), \
         patch("pool.access_status", return_value="pending"):
        text = chat._format_user_account_context(99)
    assert "=== Your account ===" in text
    assert "pending" in text.lower()
    assert "Paper PnL" not in text


def test_format_user_account_approved_no_wallet():
    portfolio = {
        "ok": True,
        "wallet_usd": 100.0,
        "deployed_usd": 0.0,
        "cash_usd": 100.0,
        "total_usd": 100.0,
        "reserved_usd": 0.0,
        "realized_pnl_usd": 0.0,
        "unrealized_pnl_usd": 0.0,
        "open_stakes": [],
        "closed_stakes": [],
        "kalshi_open": [],
        "kalshi_closed": [],
    }
    with patch("chat.bot_config.POOL_ENABLED", True), \
         patch("pool.access_status", return_value="approved"), \
         patch("pool.get_wallet", return_value=None), \
         patch("pool.get_wallet_change_request", return_value=None), \
         patch("pool.portfolio", return_value=portfolio), \
         patch("pool.strategy_subscriptions", return_value=[]), \
         patch("pool.allocations", return_value={}):
        text = chat._format_user_account_context(7)
    assert "not registered" in text.lower()
    assert "Access: approved" in text
    assert "Subscriptions: none" in text


def test_format_user_account_pool_disabled():
    with patch("chat.bot_config.POOL_ENABLED", False):
        text = chat._format_user_account_context(1)
    assert "Pool product is off" in text
    assert "Paper PnL" not in text


def test_system_suffix_is_deploy_product():
    assert "deploy-into-strategies" in chat._SYSTEM_SUFFIX


def test_account_context_carries_trade_history_and_what_if():
    """'What did my past trades look like' / 'what did I miss' answers come
    from the account block: dated settled trades with entries and P&L, lane
    lifetime totals, and the recorded autopilot counterfactual."""
    portfolio = {
        "ok": True,
        "wallet_usd": 800.0,
        "deployed_usd": 200.0,
        "cash_usd": 1000.0,
        "total_usd": 1000.0,
        "reserved_usd": 0.0,
        "realized_pnl_usd": -5.6,
        "unrealized_pnl_usd": 0.0,
        "open_stakes": [],
        "closed_stakes": [],
        "kalshi_open": [],
        "kalshi_closed": [],
        "frozen": False,
    }
    settled_rows = [
        {
            "status": "settled", "strategy": "kalshi_wick",
            "market_ticker": "KXBTCD-26OCT0718-T1", "side": "yes",
            "contracts": 13, "entry_cents": 70.0, "pnl_usd": 3.70,
            "created_at": "2026-10-07T18:05:00Z",
            "settled_at": "2026-10-07T18:15:00Z",
        },
        {
            "status": "settled", "strategy": "kalshi_wick",
            "market_ticker": "KXETHD-26OCT0715-T2", "side": "no",
            "contracts": 13, "entry_cents": 70.0, "pnl_usd": -9.30,
            "created_at": "2026-10-07T15:05:00Z",
            "settled_at": "2026-10-07T15:15:00Z",
        },
    ]
    what_if = {
        "ok": True, "strategy": "kalshi_wick", "since": "2026-10-02T14:00:00Z",
        "alloc_usd": 200.0, "sized_trades": 40, "actual_trades": 2,
        "actual_pnl_usd": -5.60, "actual_return_pct": -2.8,
        "est_pnl_usd": 12.40, "est_return_pct": 6.2,
    }
    with patch("chat.bot_config.POOL_ENABLED", True), \
         patch("pool.access_status", return_value="approved"), \
         patch("pool.get_wallet", return_value=None), \
         patch("pool.get_wallet_change_request", return_value=None), \
         patch("pool.portfolio", return_value=portfolio), \
         patch("pool.strategy_subscriptions", return_value=["kalshi_wick"]), \
         patch("pool.allocations", return_value={"kalshi_wick": 200.0}), \
         patch("pool.kalshi_positions_for", return_value=settled_rows), \
         patch("pool.kalshi_lane_realized",
               return_value={"settled": 2, "pnl_usd": -5.60}), \
         patch("counterfactual.autopilot_what_if", return_value=what_if):
        text = chat._format_user_account_context(7)

    # Dated per-trade history with entry and P&L, winners and losers alike.
    assert "Your settled Kalshi trades" in text
    assert "2026-10-07T18:15 KXBTCD-26OCT0718-T1 YES x13 @ 70c" in text
    assert "$+3.70" in text
    assert "$-9.30" in text
    # Lifetime totals so the capped list never misleads the overall figure.
    assert "2 settled, $-5.60 lifetime" in text
    # The recorded what-if, caveat attached.
    assert "Autopilot what-if" in text
    assert "~$+12.40" in text
    assert "Estimate" in text


def test_account_facts_is_the_same_block_the_model_sees():
    with patch("chat.bot_config.POOL_ENABLED", False):
        assert chat.account_facts(1) == chat._format_user_account_context(1)
    assert "paper portfolio" in chat._SYSTEM_SUFFIX.lower() or "paper book" in chat._SYSTEM_SUFFIX.lower()
    assert "do not invent" in chat._SYSTEM_SUFFIX.lower() or "Do not invent" in chat._SYSTEM_SUFFIX
    assert "Open paper positions" not in chat._SYSTEM_SUFFIX
    assert "Paper portfolio performance" not in chat._SYSTEM_SUFFIX


def test_build_vision_content_skips_missing_h1(tmp_path, monkeypatch):
    """Chat passes H4/M5 only; build_vision_content must not KeyError on H1."""
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"png")

    monkeypatch.setattr(analyze, "_encode_image", lambda _path: "base64")

    blocks = analyze.build_vision_content(
        chart_paths={"H4": str(chart), "M5": str(chart)},
        include_patterns=False,
    )
    labels = [b["text"] for b in blocks if b.get("type") == "text"]
    assert "--- Live H4 chart ---" in labels
    assert "--- Live M5 chart ---" in labels
    assert "--- Live H1 chart ---" not in labels
