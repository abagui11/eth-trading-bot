"""Telegram welcome copy and inline keyboards for beta onboarding."""

from __future__ import annotations

from datetime import datetime

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

# Button-first main menu (BONKbot-style). menu:<surface>
CB_MENU_PREFIX = "menu:"
CB_MENU_FUND = "menu:fund"
CB_MENU_WALLET = "menu:wallet"
CB_MENU_STRATEGIES = "menu:strategies"
CB_MENU_PORTFOLIO = "menu:portfolio"
CB_MENU_BRAIN = "menu:brain"
CB_MENU_HELP = "menu:help"
CB_MENU_REFRESH = "menu:refresh"
CB_MENU_HOME = "menu:home"
CB_MENU_BACK = "menu:back"

# Wallet sub-actions
CB_WALLET_DEPOSIT = "menu:wallet:deposit"
CB_WALLET_WITHDRAW_ALL = "menu:wallet:withdraw_all"
CB_WALLET_WITHDRAW_X = "menu:wallet:withdraw_x"

# Brain sub-menu
CB_BRAIN_PREFIX = "menu:brain:"
CB_BRAIN_READ = "menu:brain:read"
CB_BRAIN_CHARTS = "menu:brain:charts"
CB_BRAIN_ICT = "menu:brain:ict"
CB_BRAIN_CYCLE = "menu:brain:cycle"
CB_BRAIN_NEWS = "menu:brain:news"
CB_BRAIN_ASK = "menu:brain:ask"

# Strategy picker
CB_STRAT_PREFIX = "menu:strat:"

WELCOME_MESSAGE = (
    "Welcome to Eva.\n\n"
    "Market intelligence and live strategy books — ICT, Trade Mill, and "
    "Kalshi lanes — in one place.\n\n"
    "Tap Fund to deposit USDC, then Strategies to deploy. Not financial advice."
)

BOT_DESCRIPTION = (
    "Blazingly-fast trading intelligence at your fingertips with Eva. "
    "Use /start to open the main menu — fund with USDC, deploy into strategies, "
    "and follow Eva's live read.\n\n"
    "Powered by Republic Technologies\n"
    "https://eva.finance/"
)

BOT_SHORT_DESCRIPTION = (
    "Eva — market intelligence & live strategy books. Powered by Republic Technologies."
)

RESEARCH_HELP = (
    "Research — tap Brain on the main menu, or ask Eva in plain English.\n\n"
    "Topics: digest, funding, volume, dominance, macro, asian_session."
)

HELP_MESSAGE = (
    "Eva is Republic Technologies' market intelligence layer — structure, "
    "cycle, and news — with live strategy books built on top.\n\n"
    f"Questions? {config.EVA_SUPPORT_EMAIL}\n"
    f"Website: {config.EVA_WEBSITE_URL}\n\n"
    "Use the buttons below — Fund, Wallet, Strategies, Portfolio, Brain."
)

# Pool-admin cheatsheet for /admin. Kept here so a demo operator can read it
# without opening CLOUD.md mid-recording.
ADMIN_HELP_MESSAGE = (
    "Admin commands (pool operators only):\n\n"
    "Onboarding\n"
    "/users — roster of telegram ids + usernames + cash\n"
    "  (Admit/Deny also arrives as a card when someone new messages)\n"
    "/credit <id> <usd> [note] — fund an approved account\n"
    "/debit <id> <usd> [note] — pull cash back\n"
    "/resetdemo <id> [confirm] — wipe between demo takes\n"
    "/unsubscribe <id> — remove an account entirely\n\n"
    "Live product demo\n"
    "/democard scan — which mill ideas would fill right now\n"
    "/democard real [id] — send a fillable live mill card\n"
    "/democard mint [id] — mint a fresh idea and send it live\n"
    "/democard [id] [btc|eth] [long|short] — demo card "
    "(does not trade)\n\n"
    "Money ops\n"
    "/assign <coinbase_tx_id> <id> — claim an orphan deposit\n"
    "/payouts [resume] — payout queue / halt switch\n"
    "/sweep — MoonPay-credited capital on Coinbase\n\n"
    "Button flows (arrive as cards, not commands)\n"
    "Admit/Deny · deposit Credit/Reject · withdrawal Approve/Deny · "
    "wallet-change review"
)


def main_keyboard() -> InlineKeyboardMarkup:
    """Primary button-first home keyboard (pool or demo)."""
    if bot_config.POOL_ENABLED:
        return pool_main_keyboard()
    rows = [
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


def pool_main_keyboard() -> InlineKeyboardMarkup:
    """BONKbot-style home: Fund / Wallet / Strategies / Portfolio / Brain / Help."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Fund", callback_data=CB_MENU_FUND),
                InlineKeyboardButton("Wallet", callback_data=CB_MENU_WALLET),
            ],
            [
                InlineKeyboardButton("Strategies", callback_data=CB_MENU_STRATEGIES),
                InlineKeyboardButton("Portfolio", callback_data=CB_MENU_PORTFOLIO),
            ],
            [
                InlineKeyboardButton("Brain", callback_data=CB_MENU_BRAIN),
                InlineKeyboardButton("Help", callback_data=CB_MENU_HELP),
            ],
            [InlineKeyboardButton("Refresh", callback_data=CB_MENU_REFRESH)],
        ]
    )


def back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("← Back", callback_data=CB_MENU_HOME)]]
    )


def wallet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Deposit USDC", callback_data=CB_WALLET_DEPOSIT),
            ],
            [
                InlineKeyboardButton(
                    "Withdraw all", callback_data=CB_WALLET_WITHDRAW_ALL
                ),
                InlineKeyboardButton(
                    "Withdraw X USDC", callback_data=CB_WALLET_WITHDRAW_X
                ),
            ],
            [InlineKeyboardButton("← Back", callback_data=CB_MENU_HOME)],
        ]
    )


def brain_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Today's Read", callback_data=CB_BRAIN_READ),
                InlineKeyboardButton("Charts", callback_data=CB_BRAIN_CHARTS),
            ],
            [
                InlineKeyboardButton("ICT Table", callback_data=CB_BRAIN_ICT),
                InlineKeyboardButton("Cycle", callback_data=CB_BRAIN_CYCLE),
            ],
            [
                InlineKeyboardButton("News", callback_data=CB_BRAIN_NEWS),
                InlineKeyboardButton("Ask Eva", callback_data=CB_BRAIN_ASK),
            ],
            [InlineKeyboardButton("← Back", callback_data=CB_MENU_HOME)],
        ]
    )


def strategies_keyboard() -> InlineKeyboardMarkup:
    import strategy_catalog

    rows = [
        [
            InlineKeyboardButton(
                strategy_catalog.STRATEGIES[key].label,
                callback_data=f"{CB_STRAT_PREFIX}{key}",
            )
        ]
        for key in strategy_catalog.ORDER
    ]
    rows.append([InlineKeyboardButton("← Back", callback_data=CB_MENU_HOME)])
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


def idea_live_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    """The real mill card's buttons — Accept here places a real trade.

    `bot.on_callback` services `idea:accept:<id>` / `idea:reject:<id>` because
    the mill shares this bot's token and cannot poll for its own updates.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Accept", callback_data=f"idea:accept:{int(idea_id)}"
                ),
                InlineKeyboardButton(
                    "Reject", callback_data=f"idea:reject:{int(idea_id)}"
                ),
            ]
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
CB_POOL_WALLET_PREFIX = "poolwal:"     # poolwal:approve:<row_id> / poolwal:reject:<row_id>
CB_POOL_WITHDRAW_PREFIX = "poolwd:"    # poolwd:approve:<id> / poolwd:reject:<id>
CB_POOL_UNSUB_PREFIX = "poolunsub:"    # poolunsub:yes:<id> / poolunsub:no:<id>
CB_POOL_PORTFOLIO = "pool:portfolio"
CB_POOL_DEPOSIT = "pool:deposit"
# Demo card Accept. A separate prefix on purpose: it carries a ref no
# executor ever reads, which is what makes pressing Accept structurally
# incapable of placing an order. See bot._pool_demo_accept.
CB_POOL_DEMO_PREFIX = "pooldemo:"      # pooldemo:yes:<token> / pooldemo:no:<token>

PENDING_APPROVAL_MESSAGE = (
    "Thanks for your interest in Eva.\n\n"
    "Access is invite-only right now. Your request has been sent for review — "
    "you will get a message here the moment you are admitted."
)

DENIED_MESSAGE = "Access is invite-only right now, and we can't add you today."

POOL_WELCOME_MESSAGE = (
    "Welcome to Eva — you're in.\n\n"
    "Tap the buttons below to Fund, check Wallet, deploy into Strategies, "
    "or open Portfolio. Brain shows what Eva sees right now.\n\n"
    "Each Accept risks about "
    f"{bot_config.POOL_RISK_PCT * 100:.1f}% of your deployment to that "
    "strategy — not your full balance.\n\n"
    "Trading involves substantial risk of loss. Not financial advice."
)


# /subscribe flow. sub:choose:<key> picks a strategy; sub:alloc:<key>:<pct>
# allocates that percent of available cash; sub:skip:<key> subscribes without
# capital. Keys are strategy_catalog wire keys.
CB_SUB_PREFIX = "sub:"

# Percent-of-available presets on the allocation prompt.
SUB_ALLOC_PRESETS = (25, 50, 100)


def subscribe_keyboard() -> InlineKeyboardMarkup:
    import strategy_catalog

    rows = [
        [InlineKeyboardButton(
            strategy_catalog.STRATEGIES[key].label,
            callback_data=f"{CB_SUB_PREFIX}choose:{key}",
        )]
        for key in strategy_catalog.ORDER
    ]
    return InlineKeyboardMarkup(rows)


def alloc_keyboard(strategy_key: str) -> InlineKeyboardMarkup:
    presets = [
        InlineKeyboardButton(
            f"{pct}% of available",
            callback_data=f"{CB_SUB_PREFIX}alloc:{strategy_key}:{pct}",
        )
        for pct in SUB_ALLOC_PRESETS
    ]
    return InlineKeyboardMarkup(
        [
            presets,
            [InlineKeyboardButton(
                "Not now", callback_data=f"{CB_SUB_PREFIX}skip:{strategy_key}"
            )],
            [InlineKeyboardButton("← Back", callback_data=CB_MENU_STRATEGIES)],
        ]
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


def pool_admin_wallet_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Approve", callback_data=f"{CB_POOL_WALLET_PREFIX}approve:{request_id}"
                ),
                InlineKeyboardButton(
                    "Reject", callback_data=f"{CB_POOL_WALLET_PREFIX}reject:{request_id}"
                ),
            ]
        ]
    )


def pool_admin_unsubscribe_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """Confirm/cancel for an account removal.

    The target id rides in the callback data rather than in any per-chat
    state, so the button cannot be made to delete a different account than
    the card it is attached to describes.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Remove account",
                    callback_data=f"{CB_POOL_UNSUB_PREFIX}yes:{telegram_id}",
                ),
                InlineKeyboardButton(
                    "Cancel",
                    callback_data=f"{CB_POOL_UNSUB_PREFIX}no:{telegram_id}",
                ),
            ]
        ]
    )


def pool_admin_withdrawal_keyboard(withdrawal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Send",
                    callback_data=f"{CB_POOL_WITHDRAW_PREFIX}approve:{withdrawal_id}",
                ),
                InlineKeyboardButton(
                    "Reject",
                    callback_data=f"{CB_POOL_WITHDRAW_PREFIX}reject:{withdrawal_id}",
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


def pool_demo_keyboard(token: str) -> InlineKeyboardMarkup:
    """Accept/Reject for a demo card. Same shape as the real trade keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Accept", callback_data=f"{CB_POOL_DEMO_PREFIX}yes:{token}"
                ),
                InlineKeyboardButton(
                    "Reject", callback_data=f"{CB_POOL_DEMO_PREFIX}no:{token}"
                ),
            ]
        ]
    )


def pool_account_keyboard() -> InlineKeyboardMarkup:
    """Alias for the button-first home keyboard."""
    return pool_main_keyboard()


def format_pool_welcome(*, wallet_usd: float = 0.0) -> str:
    """Short /start welcome for the live pool (progressive disclosure)."""
    lines = [
        "Welcome to Eva — the intelligence layer for crypto markets.",
        "Powered by Republic Technologies.",
        "",
    ]
    if wallet_usd <= 0:
        lines.append("You have no USDC yet — tap Fund to get started.")
    else:
        lines.append(
            f"Wallet: ${wallet_usd:,.2f} USDC available. "
            "Tap Strategies to deploy, or Portfolio for the full picture."
        )
    lines += [
        "",
        "Each Accept risks about "
        f"{bot_config.POOL_RISK_PCT * 100:.1f}% of what you deployed into "
        "that strategy — not your full balance.",
        "",
        "Trading involves substantial risk of loss. Not financial advice.",
    ]
    return "\n".join(lines)


def format_fill_celebration(
    *,
    strategy_label: str,
    side: str,
    entry: float,
    stop: float | None,
    targets: list[float] | None,
    risk_usd: float,
    notional_usd: float | None = None,
    resting: bool = False,
) -> str:
    """Punchy confirmation after an Accept lands (fill or resting limit)."""
    side_u = (side or "").upper()
    if resting:
        lines = [
            f"You're in — {strategy_label}",
            "",
            f"{side_u} resting limit at ${float(entry):,.2f}.",
            f"Sized at ${float(risk_usd):,.2f} risk "
            f"({bot_config.POOL_RISK_PCT * 100:.1f}%).",
        ]
        if stop is not None:
            try:
                lines.append(f"Stop ${float(stop):,.2f}.")
            except (TypeError, ValueError):
                pass
        lines += [
            "",
            "I'll DM you the moment it fills. Tap Portfolio any time.",
        ]
        return "\n".join(lines)

    lines = [
        f"Filled — {strategy_label}",
        "",
        f"{side_u} @ ${float(entry):,.2f}",
    ]
    if stop is not None:
        try:
            lines.append(f"Stop ${float(stop):,.2f}")
        except (TypeError, ValueError):
            pass
    if targets:
        tp_bits = []
        for t in targets[:3]:
            try:
                tp_bits.append(f"${float(t):,.2f}")
            except (TypeError, ValueError):
                continue
        if tp_bits:
            lines.append(f"Targets {', '.join(tp_bits)}")
    size_bit = f"${float(risk_usd):,.2f} at risk"
    if notional_usd is not None:
        size_bit = f"${float(notional_usd):,.2f} notional · " + size_bit
    lines += ["", size_bit, "", "Exits are automatic. Tap Portfolio any time."]
    return "\n".join(lines)


def format_fund_moonpay(
    *,
    address: str | None,
    widget_url: str | None = None,
    configured: bool = True,
) -> str:
    """Fund button copy — personal Base USDC deposit address."""
    minimum = float(bot_config.POOL_MIN_DEPOSIT_USD)
    if not configured or not address:
        return "\n".join([
            "Fund your wallet\n",
            "USDC deposits on Base are being wired up. "
            f"Message {config.EVA_SUPPORT_EMAIL} if you need a manual credit, "
            "or try again shortly.",
            "",
            f"Minimum once live: ${minimum:,.0f} USDC on Base.",
        ])
    lines = [
        "Fund your wallet\n",
        "Send USDC on Base to your personal deposit address (tap to copy):",
        f"`{address}`",
        "",
        f"Network: Base · Asset: USDC only · Minimum: ${minimum:,.0f}",
        "",
        "Once it settles, your balance updates here automatically — "
        "tap Refresh or Wallet.",
    ]
    if widget_url:
        lines += ["", f"Or buy USDC with a card: {widget_url}"]
    return "\n".join(lines)


def format_wallet_surface(
    *,
    address: str | None,
    wallet_usd: float,
    deployed_usd: float,
    reserved_usd: float = 0.0,
) -> str:
    total = wallet_usd + deployed_usd
    lines = [
        "Your wallet\n",
        (
            f"Address: `{address}`"
            if address
            else "Address: not provisioned yet — tap Deposit USDC."
        ),
        "",
        f"Wallet (undeployed): ${wallet_usd:,.2f} USDC",
        f"Deployed in strategies: ${deployed_usd:,.2f} USDC",
        f"Total: ${total:,.2f} USDC",
    ]
    if reserved_usd > 0:
        lines.append(f"In open trades / reserved: ${reserved_usd:,.2f}")
    lines += [
        "",
        "Deposit USDC on Base, or withdraw back to an address you control.",
    ]
    return "\n".join(lines)


def format_deposit_instructions(
    *, has_pending: bool = False, wallet: str | None = None
) -> str:
    """Legacy Coinbase+txhash path — kept for admin / fallback copy."""
    address = config.POOL_DEPOSIT_ADDRESS or "(deposit address not configured — ask the admin)"
    # Never guess the chain: USDC sent to this address on a network we do not
    # control it on is unrecoverable.
    network = config.POOL_DEPOSIT_CHAIN or "confirm with the admin before sending"
    minimum = float(bot_config.POOL_MIN_DEPOSIT_USD)
    risk_pct = float(bot_config.POOL_RISK_PCT) * 100
    example = 1000.0
    example_risk = example * float(bot_config.POOL_RISK_PCT)

    if wallet is None:
        return "\n".join([
            "Fund your account\n",
            "Prefer the Fund button — it gives you a personal USDC address on Base.",
            "",
            "Legacy path (admin fallback): register a payout wallet with",
            "   /wallet 0x<your address>",
            "then /deposit shows where to send.",
        ])

    lines = [
        "Fund your account (legacy)\n",
        "Prefer the Fund button for MoonPay USDC on Base.\n",
        "Sizes stay intentionally small while we solidify the strategy. "
        "Depositing $1,000 does *not* put $1,000 into the next trade — each "
        f"Accept risks about {risk_pct:.1f}% of your deployment.\n",
        f"Example: ${example:,.0f} available → about ${example_risk:,.2f} at "
        "risk if that trade is stopped out. The rest stays available for "
        "other Accepts or sits in cash.\n",
        f"1. Send USDC *from your registered wallet*:\n`{wallet}`",
        f"2. To this address:\n`{address}`",
        f"   Network: *{network}*. USDC only.",
        f"3. Minimum: ${minimum:,.0f}",
        "4. Tell me the amount *and the transaction hash*:\n"
        "   /deposit 1000 0x<transaction hash>",
        "",
        "*The hash is what credits you.* We watch the exchange for it and "
        "credit your balance automatically the moment your transfer settles.",
    ]
    if has_pending:
        lines.append("")
        lines.append("You already have a deposit request pending review.")
    return "\n".join(lines)


def _friendly_utc(stamp: str) -> str:
    """`2026-09-16T20:00:00Z` → `16 Sep 20:00 UTC`, or the input if odd."""
    try:
        when = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return str(stamp)
    return when.strftime("%d %b %H:%M UTC")


def format_wallet_status(
    wallet: dict | None, *, change: dict | None = None
) -> str:
    """What /wallet shows: the payout address and how settled it is."""
    if wallet is None:
        return "\n".join([
            "Your payout wallet\n",
            "You haven't registered one yet. For withdrawals, send:",
            "   /wallet 0x<your address>",
            "",
            "Funding uses the Fund button (USDC on Base) — no registration needed.",
        ])

    verified = str(wallet.get("status")) == "verified"
    lines = [
        "Your payout wallet\n",
        f"`{wallet['address']}`",
        "",
    ]
    if verified:
        lines.append(
            "Confirmed — we've seen a deposit arrive from this address, so "
            "we know you control it. Withdrawals return here."
        )
    else:
        # Say plainly that the address is unproven rather than implying the
        # payout path is ready: a tester who assumes it is set could be
        # surprised at exactly the wrong moment.
        lines.append(
            "Registered, not yet confirmed. It's confirmed the first time a "
            "deposit arrives from it — that's what proves the wallet is "
            "yours, and withdrawals can only go to a confirmed address."
        )
    held = wallet.get("payouts_blocked_until")
    if held:
        lines += [
            "",
            f"Withdrawals to this address are on hold until {_friendly_utc(held)} "
            "(24h after an address change, as a safety measure).",
        ]
    if change:
        lines += [
            "",
            f"Change pending admin review: `{change['address']}`",
        ]
    else:
        lines += [
            "",
            "To change it, send /wallet with the new address. Changes need "
            "admin review and hold withdrawals for "
            f"{float(bot_config.POOL_WALLET_COOLDOWN_HOURS):.0f}h — if "
            "someone ever gets into your Telegram, that delay is what stops "
            "them redirecting your money.",
        ]
    # What they can do next, said differently depending on whether the
    # address is proven — a tester who assumes withdrawals are ready and
    # finds out otherwise learns it at the worst possible moment.
    if verified:
        lines += [
            "",
            f"Withdrawals are open. Minimum "
            f"${float(bot_config.POOL_MIN_WITHDRAWAL_USD):,.0f}, usually in "
            "your wallet within a few minutes, and we message you with the "
            "transaction once it lands. /withdraw to see what's available.",
        ]
    else:
        lines += [
            "",
            "Withdrawals open once this address is confirmed, which happens "
            "automatically the first time a deposit arrives from it.",
        ]
    return "\n".join(lines)


def format_portfolio(p: dict) -> str:
    """Telegram text for Portfolio — wallet, deployments, PnL, open trades."""
    if not p.get("ok"):
        return (
            "No pool account yet. Once you're admitted, tap Fund to deposit USDC."
        )
    wallet = float(
        p["wallet_usd"] if p.get("wallet_usd") is not None
        else max(0.0, float(p["cash_usd"]) - float(p.get("deployed_usd") or 0))
    )
    deployed = float(p.get("deployed_usd") or 0)
    cash = float(p["cash_usd"])
    total = float(p.get("total_usd") or (wallet + deployed))
    realized = float(p.get("realized_pnl_usd") or 0)
    unrealized = float(p.get("unrealized_pnl_usd") or 0)
    deposited = float(p.get("deposited_usd") or 0)
    pnl_total = realized + unrealized
    pnl_pct = (pnl_total / deposited * 100.0) if deposited > 0 else 0.0

    lines = [
        "Your portfolio\n",
        f"Total size: ${total:,.2f}",
        f"  Wallet: ${wallet:,.2f}",
        f"  Deployed: ${deployed:,.2f}",
    ]
    if float(p.get("reserved_usd") or 0) > 0:
        lines.append(
            f"  Reserved in trades: ${float(p['reserved_usd']):,.2f}"
        )
    lines.append(
        f"PnL: ${pnl_total:+,.2f} ({pnl_pct:+.2f}%) · "
        f"realized ${realized:+,.2f} · unrealized ${unrealized:+,.2f}"
    )

    by_strat = p.get("deployments") or {}
    if by_strat:
        lines.append("")
        lines.append("Your deployments:")
        for key, amt in by_strat.items():
            if float(amt) <= 0:
                continue
            try:
                import strategy_catalog
                label = strategy_catalog.STRATEGIES[key].label
            except Exception:
                label = key
            lines.append(f"• {label}: ${float(amt):,.2f} (yours)")

    opens = p.get("open_stakes") or []
    if opens:
        lines.append("")
        lines.append(f"Outstanding trades ({len(opens)}):")
        for s in opens:
            product = bot_config.product_label(str(s.get("product_id") or ""))
            unreal = s.get("unrealized_usd")
            unreal_bit = f" · now ${float(unreal):+,.2f}" if unreal is not None else ""
            lines.append(
                f"• {product} {s.get('side')} — "
                f"${float(s['cost_usd']):,.2f} "
                f"({float(s['share_frac']) * 100:.1f}%) · "
                f"risk ${float(s['risk_usd']):,.2f}{unreal_bit}"
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
        if cash <= 0 and deployed <= 0:
            lines.append("No funds yet — tap Fund to get started.")
        else:
            lines.append(
                "No open trades. Accept a card after deploying into a strategy."
            )
    if p.get("frozen"):
        lines.append("")
        lines.append(
            "Note: new trade joins are paused while we verify the books. "
            "Your balance is safe and exits keep booking."
        )
    return "\n".join(lines)
