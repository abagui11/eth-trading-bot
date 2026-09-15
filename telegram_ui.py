"""Telegram welcome copy and inline keyboards for beta onboarding."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import bot_config
import config

CB_OPEN = "ui:open"
CB_OPEN_SIZE_PREFIX = "ui:open:"
CB_METRICS = "ui:metrics"
CB_MY_BOOK = "ui:mybook"
CB_FEED = "ui:feed"
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
        [
            InlineKeyboardButton("My book", callback_data=CB_MY_BOOK),
            InlineKeyboardButton("Idea feed", callback_data=CB_FEED),
        ],
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


# ---------------------------------------------------------------------------
# Tester pool — Admit flow, deposits, Account keyboard
# ---------------------------------------------------------------------------

CB_POOL_USER_PREFIX = "pooluser:"      # pooluser:approve:<id> / pooluser:deny:<id>
CB_POOL_DEPOSIT_PREFIX = "pooldep:"    # pooldep:credit:<req_id> / pooldep:deny:<req_id>
CB_POOL_PORTFOLIO = "pool:portfolio"
CB_POOL_DEPOSIT = "pool:deposit"

PENDING_APPROVAL_MESSAGE = (
    "Thanks for your interest in Eva.\n\n"
    "Access is invite-only right now. Your request has been sent for review — "
    "you will get a message here the moment you are admitted."
)

DENIED_MESSAGE = "Access is invite-only right now, and we can't add you today."

POOL_WELCOME_MESSAGE = (
    "Welcome to Eva — you're in.\n\n"
    "This is an early tester pool. We are still solidifying the strategy, so "
    "position sizes are kept deliberately small on purpose — not because your "
    "deposit is ignored, but so each Accept risks only a small slice of your "
    "cash while the book is proven.\n\n"
    "How risk works:\n"
    "• When you Accept a High Quality or mill trade card, only about "
    f"{bot_config.POOL_RISK_PCT * 100:.1f}% of your *available* cash is put "
    "at risk on that trade (same rule as the house clip).\n"
    "• Example: $1,000 available → roughly $7 at the stop if that trade is "
    "stopped out. Most of your balance stays out of that trade.\n"
    "• You fill at the same price as the house; exits (stop, targets, trail) "
    "are automatic and your share is credited as each one fills.\n\n"
    "Commands:\n"
    "• /portfolio — cash, positions, P&L\n"
    "• /deposit — how to fund (credits stay manual until we confirm on the venue)\n\n"
    "Trade cards arrive here as private messages with *your* size on them. "
    "Anything about your money stays in this chat.\n\n"
    "Trading futures involves substantial risk of loss. Not financial advice."
)


def pool_admin_access_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Admit", callback_data=f"{CB_POOL_USER_PREFIX}approve:{telegram_id}"
                ),
                InlineKeyboardButton(
                    "Deny", callback_data=f"{CB_POOL_USER_PREFIX}deny:{telegram_id}"
                ),
            ]
        ]
    )


def pool_admin_deposit_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Credit", callback_data=f"{CB_POOL_DEPOSIT_PREFIX}credit:{request_id}"
                ),
                InlineKeyboardButton(
                    "Deny", callback_data=f"{CB_POOL_DEPOSIT_PREFIX}deny:{request_id}"
                ),
            ]
        ]
    )


def pool_account_keyboard() -> InlineKeyboardMarkup:
    """Light DM home: Portfolio + Deposit."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Portfolio", callback_data=CB_POOL_PORTFOLIO),
                InlineKeyboardButton("Deposit", callback_data=CB_POOL_DEPOSIT),
            ]
        ]
    )


def format_deposit_instructions(*, has_pending: bool = False) -> str:
    address = config.POOL_DEPOSIT_ADDRESS or "(deposit address not configured — ask the admin)"
    minimum = float(bot_config.POOL_MIN_DEPOSIT_USD)
    risk_pct = float(bot_config.POOL_RISK_PCT) * 100
    example = 1000.0
    example_risk = example * float(bot_config.POOL_RISK_PCT)
    lines = [
        "Fund your account\n",
        "Sizes stay intentionally small while we solidify the strategy. "
        "Depositing $1,000 does *not* put $1,000 into the next trade — each "
        f"Accept risks about {risk_pct:.1f}% of your available cash.\n",
        f"Example: ${example:,.0f} available → about ${example_risk:,.2f} at "
        "risk if that trade is stopped out. The rest stays available for "
        "other Accepts or sits in cash.\n",
        f"1. Send USDC to:\n{address}",
        f"2. Minimum: ${minimum:,.0f}",
        "3. Then tell me the amount:  /deposit 1000  (optionally add the txid: "
        "/deposit 1000 0xabc...)",
        "",
        "Your balance is credited once the funds land on the venue and an "
        "admin confirms — you'll get a message here. Trade cards will then "
        "show the dollar risk *your* Accept would take.",
    ]
    if has_pending:
        lines.append("")
        lines.append("You already have a deposit request pending review.")
    return "\n".join(lines)


def format_portfolio(p: dict) -> str:
    """Telegram text for /portfolio — the tester's real book."""
    if not p.get("ok"):
        return (
            "No pool account yet. Once you're admitted, /deposit shows how to "
            "fund it."
        )
    lines = ["Your portfolio\n"]
    lines.append(f"Cash: ${float(p['cash_usd']):,.2f}")
    if float(p.get("reserved_usd") or 0) > 0:
        lines.append(
            f"In trades / reserved: ${float(p['reserved_usd']):,.2f} "
            f"(available ${float(p['available_usd']):,.2f})"
        )
    lines.append(f"Deposited: ${float(p['deposited_usd']):,.2f}")
    lines.append(
        f"Realized P&L: ${float(p['realized_pnl_usd']):+,.2f} · "
        f"Unrealized: ${float(p['unrealized_pnl_usd']):+,.2f}"
    )
    opens = p.get("open_stakes") or []
    if opens:
        lines.append("")
        lines.append(f"Open positions ({len(opens)}):")
        for s in opens:
            product = bot_config.product_label(str(s.get("product_id") or ""))
            unreal = s.get("unrealized_usd")
            unreal_bit = f" · now ${float(unreal):+,.2f}" if unreal is not None else ""
            lines.append(
                f"• {product} {s.get('side')} — your size "
                f"${float(s['cost_usd']):,.2f} ({float(s['share_frac']) * 100:.1f}% of "
                f"the position) · risk ${float(s['risk_usd']):,.2f}{unreal_bit}"
            )
    closed = p.get("closed_stakes") or []
    if closed:
        lines.append("")
        lines.append("Recent closed:")
        for s in closed:
            product = bot_config.product_label(str(s.get("product_id") or ""))
            lines.append(
                f"• {product} {s.get('side')} — "
                f"${float(s['realized_pnl_usd']):+,.2f}"
            )
    if not opens and not closed:
        lines.append("")
        if float(p.get("cash_usd") or 0) <= 0:
            lines.append("No funds yet — /deposit to get started.")
        else:
            lines.append(
                "No positions yet. Accept a trade card in the group to join "
                "the next one."
            )
    if p.get("frozen"):
        lines.append("")
        lines.append(
            "Note: new trade joins are paused while we verify the books. "
            "Your balance is safe and exits keep booking."
        )
    return "\n".join(lines)
