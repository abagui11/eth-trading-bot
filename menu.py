"""Button-first menu surfaces for the Eva Telegram bot.

Called from bot.on_callback for menu:* callbacks. Keeps the progressive-
disclosure copy and MoonPay / wallet / strategies / brain wiring in one place.
"""

from __future__ import annotations

import logging
from typing import Any

import bot_config
import brain_report
import config
import moonpay
import pool
import research
import strategy_catalog
import telegram_ui

logger = logging.getLogger(__name__)

# context.user_data key when the user tapped Withdraw X and should type an amount.
AWAITING_WITHDRAW = "awaiting_withdraw_amount"
AWAITING_DEPLOY = "awaiting_deploy_amount"  # value = strategy key


def _wallet_bits(user_id: int) -> dict[str, Any]:
    bal = pool.wallet_balance(user_id)
    row = pool.get_moonpay_customer(user_id)
    return {
        **bal,
        "address": (row or {}).get("deposit_address"),
        "customer_token": (row or {}).get("customer_token"),
    }


def ensure_deposit_address(user_id: int) -> dict[str, Any]:
    """Return {ok, address, widget_url, configured} provisioning if needed."""
    existing = pool.get_moonpay_customer(user_id)
    if existing and existing.get("deposit_address"):
        return {
            "ok": True,
            "configured": moonpay.configured(),
            "address": existing["deposit_address"],
            "widget_url": moonpay.widget_url(
                customer_token=existing.get("customer_token"),
                customer_id=existing.get("customer_id"),
            ),
        }
    if not moonpay.configured():
        return {"ok": False, "configured": False, "address": None, "widget_url": None}
    result = moonpay.create_or_get_customer(user_id)
    if not result.get("ok") or not result.get("address"):
        return {
            "ok": False,
            "configured": True,
            "address": None,
            "widget_url": None,
            "reason": result.get("reason"),
        }
    pool.upsert_moonpay_customer(
        user_id,
        customer_id=str(result["customer_id"]),
        deposit_address=str(result["address"]),
        customer_token=result.get("customer_token"),
    )
    return {
        "ok": True,
        "configured": True,
        "address": result["address"],
        "widget_url": result.get("widget_url"),
    }


def home_text(user_id: int) -> str:
    bal = pool.wallet_balance(user_id)
    return telegram_ui.format_pool_welcome(wallet_usd=float(bal.get("wallet_usd") or 0))


def fund_surface(user_id: int) -> tuple[str, object]:
    prov = ensure_deposit_address(user_id)
    text = telegram_ui.format_fund_moonpay(
        address=prov.get("address"),
        widget_url=prov.get("widget_url"),
        configured=bool(prov.get("configured")) and bool(prov.get("address")),
    )
    return text, telegram_ui.back_home_keyboard()


def wallet_surface(user_id: int) -> tuple[str, object]:
    bits = _wallet_bits(user_id)
    if not bits.get("address") and moonpay.configured():
        prov = ensure_deposit_address(user_id)
        bits["address"] = prov.get("address")
    text = telegram_ui.format_wallet_surface(
        address=bits.get("address"),
        wallet_usd=float(bits.get("wallet_usd") or 0),
        deployed_usd=float(bits.get("deployed_usd") or 0),
        reserved_usd=float(bits.get("reserved_usd") or 0),
    )
    return text, telegram_ui.wallet_keyboard()


def portfolio_surface(user_id: int) -> tuple[str, object]:
    spots = research.get_spot_prices()
    text = telegram_ui.format_portfolio(pool.portfolio(user_id, spots))
    return text, telegram_ui.pool_main_keyboard()


def strategies_surface(user_id: int) -> tuple[str, object]:
    subs = set(pool.strategy_subscriptions(user_id))
    allocs = pool.allocations(user_id)
    lines = [
        "Strategies\n",
        "Pick a strategy to subscribe and deploy wallet USDC into it. "
        f"Minimum deploy ${float(bot_config.POOL_MIN_DEPLOY_USD):,.0f}.",
        "",
    ]
    for key in strategy_catalog.ORDER:
        strat = strategy_catalog.STRATEGIES[key]
        flags = []
        if key in subs:
            flags.append("subscribed")
        amt = float(allocs.get(key) or 0)
        if amt > 0:
            flags.append(f"${amt:,.0f} deployed")
        if not strat.executable:
            flags.append("feed only")
        tag = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"• {strat.label}{tag}")
        lines.append(f"  {strat.pitch}")
    return "\n".join(lines), telegram_ui.strategies_keyboard()


def strategy_detail(user_id: int, key: str) -> tuple[str, object | None]:
    if not strategy_catalog.is_valid(key):
        return "Unknown strategy.", telegram_ui.strategies_keyboard()
    strat = strategy_catalog.STRATEGIES[key]
    pool.subscribe_strategy(user_id, key)
    if not strat.executable:
        text = (
            f"{strat.label}\n\n"
            f"{strat.pitch}\n\n"
            "Idea cards only for now — accepting into this lane with real "
            "capital is coming soon. You're subscribed to the feed."
        )
        return text, telegram_ui.strategies_keyboard()

    bal = pool.wallet_balance(user_id)
    p = {
        "ok": True,
        "cash_usd": bal["cash_usd"],
        "available_usd": bal["wallet_usd"],
        "wallet_usd": bal["wallet_usd"],
    }
    current = pool.get_allocation(user_id, key)
    text = (
        f"{strat.label}\n\n"
        f"{strat.pitch}\n\n"
        f"{strat.risk_lines}\n\n"
        f"Wallet available to deploy: ${float(bal['wallet_usd']):,.2f}\n"
        f"Currently deployed here: ${current:,.2f}\n\n"
        f"How much do you want to deploy? Minimum "
        f"${float(bot_config.POOL_MIN_DEPLOY_USD):,.0f}. "
        "Or type a dollar amount."
    )
    # Reuse allocation_prompt style when available
    try:
        text = (
            f"{strat.label}\n\n"
            + strategy_catalog.allocation_prompt(key, p, current_alloc=current)
            + f"\n\nOr type a dollar amount (min "
            f"${float(bot_config.POOL_MIN_DEPLOY_USD):,.0f})."
        )
    except Exception:
        pass
    return text, telegram_ui.alloc_keyboard(key)


def help_surface() -> tuple[str, object]:
    return telegram_ui.HELP_MESSAGE, telegram_ui.pool_main_keyboard()


def brain_menu() -> tuple[str, object]:
    return (
        "Eva's brain\n\n"
        "What Eva sees right now — vision across timeframes, ICT structure, "
        "the four-year cycle, and live news.\n"
        "Charts and the ICT read refresh with every cycle (about every "
        "30 minutes).\n\n"
        "Pick a section:",
        telegram_ui.brain_keyboard(),
    )


def brain_section(section: str) -> tuple[str, list[str] | None]:
    """Return (text, optional chart paths). Never attaches a Decision trade card.

    ``text`` may be plain or HTML depending on section — use
    :func:`format_brain_html` before sending with parse_mode=HTML.
    """
    if section == "read":
        return (
            "Today's Read\n\n" + brain_report.synthesize_read(),
            None,
        )
    if section == "charts":
        paths = brain_report.vision_chart_paths()
        if not paths:
            return (
                "No vision charts yet — check back after the next cycle "
                "(about every 30 minutes).",
                None,
            )
        return brain_report.vision_charts_caption(), paths
    if section == "ict":
        return brain_report.ict_view_text(), None
    if section == "cycle":
        text = brain_report.cycle_text()
        chart = brain_report.cycle_chart_path()
        return text, [chart] if chart else None
    if section == "news":
        return brain_report.news_html(), None
    if section == "ask":
        return (
            "Ask Eva anything in plain English — just type your question here.",
            None,
        )
    return "Unknown section.", None


def format_brain_html(section: str, text: str) -> str:
    """Telegram HTML for a brain section."""
    import html as html_mod

    if section == "news":
        # news_html() already returns escaped HTML with <a> links.
        return text or ""
    if section == "ict":
        return f"<pre>{html_mod.escape(text or '')}</pre>"
    # Today's Read / Cycle / Ask — preserve paragraphs, escape safely.
    escaped = html_mod.escape(text or "")
    return escaped


def format_html_pre(text: str) -> str:
    """Wrap tabular / monospace content for Telegram HTML parse_mode."""
    import html

    escaped = html.escape(text or "")
    # Heuristic: if it looks like a table (pipes or aligned columns), use <pre>
    if "|" in escaped or "\n  " in escaped:
        return f"<pre>{escaped}</pre>"
    return escaped
