"""Telegram welcome copy and inline keyboards for beta onboarding."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import bot_config
import config

CB_OPEN = "ui:open"
CB_OPEN_SIZE_PREFIX = "ui:open:"
CB_METRICS = "ui:metrics"
CB_MY_BOOK = "ui:mybook"
CB_RESEARCH = "ui:research"
CB_REFRESH = "ui:refresh"

# Backward-compat alias (old Fund button callbacks / tests).
CB_FUND = CB_OPEN

CB_TRADE_YES_PREFIX = "trade:yes:"
CB_TRADE_NO_PREFIX = "trade:no:"
CB_TRADE_JOIN_PREFIX = "trade:join:"
CB_TRADE_SKIP_PREFIX = "trade:skip:"
CB_TRADE_MORE_PREFIX = "trade:more:"

WELCOME_MESSAGE = (
    "Welcome to the ETH/BTC Trading Agent (beta).\n\n"
    "This bot does NOT place real trades. It runs an ICT-style swing/day strategy "
    "on ETH and BTC (including W1 ETH/BTC relative strength).\n\n"
    "You get a personal demo paper account. Trade suggestions include Accept / Reject — "
    "only Accept puts your demo cash into a trade. The public dashboard is the "
    "agent/house journal; My book shows your personal ledger.\n\n"
    "Open account once and choose $500 / $1,000 / $2,500 "
    "(demo capital — not real funding).\n\n"
    "Use the buttons below, or /research for market studies. Not financial advice."
)

RESEARCH_HELP = (
    "Research — how to use it\n\n"
    "• /research — topic catalog (digest, funding, volume, dominance, macro, asian_session, SFP studies)\n"
    "• /research funding — run a specific topic\n"
    "• Or ask in plain English: \"What's ETH funding?\" / \"Asian session BTC\" / \"weekly SFP study\"\n\n"
    "Research is read-only context for the paper strategy; it does not move the portfolio."
)


def main_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("Open account", callback_data=CB_OPEN),
            InlineKeyboardButton("My Metrics", callback_data=CB_METRICS),
        ],
        [InlineKeyboardButton("My book", callback_data=CB_MY_BOOK)],
    ]
    dash = config.DASHBOARD_PUBLIC_URL
    if dash:
        rows.append(
            [InlineKeyboardButton("Agent journal", url=dash.rstrip("/"))]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "Journal (set DASHBOARD_PUBLIC_URL)",
                    callback_data=CB_REFRESH,
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("Research", callback_data=CB_RESEARCH),
            InlineKeyboardButton("Refresh", callback_data=CB_REFRESH),
        ]
    )
    return InlineKeyboardMarkup(rows)


def open_account_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f"${int(size):,}",
            callback_data=f"{CB_OPEN_SIZE_PREFIX}{int(size)}",
        )
        for size in bot_config.PAPER_ACCOUNT_SIZES
    ]
    return InlineKeyboardMarkup([buttons])


def trade_decision_keyboard(offer_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Accept", callback_data=f"{CB_TRADE_YES_PREFIX}{offer_id}"
                ),
                InlineKeyboardButton(
                    "Reject", callback_data=f"{CB_TRADE_NO_PREFIX}{offer_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    "See more", callback_data=f"{CB_TRADE_MORE_PREFIX}{offer_id}"
                ),
            ],
        ]
    )


def missed_connection_keyboard(offer_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Join now", callback_data=f"{CB_TRADE_JOIN_PREFIX}{offer_id}"
                ),
                InlineKeyboardButton(
                    "Still no", callback_data=f"{CB_TRADE_SKIP_PREFIX}{offer_id}"
                ),
            ]
        ]
    )


def format_metrics_message(metrics: dict) -> str:
    if not metrics.get("ok"):
        sizes = " / ".join(f"${int(s):,}" for s in bot_config.PAPER_ACCOUNT_SIZES)
        return (
            "My Metrics\n\n"
            "You have not opened a paper account yet. Tap Open account and choose "
            f"{sizes} (demo capital — not real funding)."
        )
    open_n = int(metrics.get("open_count") or 0)
    return (
        "My Metrics (personal demo)\n\n"
        f"Starting capital: ${metrics['amount_usd']:,.0f}\n"
        f"Cash: ${float(metrics.get('cash_usd') or 0):,.2f}\n"
        f"Equity: ${metrics['equity_usd']:,.2f}\n"
        f"PnL: ${metrics['pnl_usd']:+,.2f} ({metrics['pnl_pct']:+.2f}%)\n"
        f"Open positions: {open_n}\n\n"
        "Only trades you Accept (or late-join) affect this book."
    )


def format_open_account_prompt() -> str:
    sizes = " / ".join(f"${int(s):,}" for s in bot_config.PAPER_ACCOUNT_SIZES)
    return (
        "Open paper account\n\n"
        f"Choose demo starting capital: {sizes}.\n"
        "Demo capital — not real funding. Once only.\n"
        "Accept/Reject on trade cards decides whether this cash enters a trade."
    )


def format_open_account_result(result: dict) -> str:
    if not result.get("ok"):
        reason = result.get("reason") or "failed"
        if reason == "already_opened":
            amount = float(result.get("amount_usd") or result.get("starting_usd") or 0)
            return (
                "Account already open.\n\n"
                f"Starting capital: ${amount:,.0f}\n"
                f"Cash: ${float(result.get('cash_usd') or amount):,.2f}\n"
                "Tap My Metrics or My book for your ledger."
            )
        if reason == "invalid_amount":
            return "Invalid size. Use the menu buttons."
        return f"Open account failed ({reason})."
    amount = float(result.get("amount_usd") or 0)
    return (
        "Paper account opened.\n\n"
        f"Demo capital: ${amount:,.0f}\n"
        "This is not real funding — nothing left your wallet.\n"
        "When a trade card arrives, Accept to deploy cash or Reject to sit out."
    )


def format_fund_result(result: dict) -> str:
    """Backward-compatible wrapper around open-account results."""
    if result.get("reason") == "already_funded":
        result = {**result, "reason": "already_opened"}
    return format_open_account_result(result)
