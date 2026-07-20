"""Bot runtime configuration (non-secret tunables)."""

from __future__ import annotations

# Products the hourly cycle and watchdog may trade concurrently.
TRADED_PRODUCTS: tuple[str, ...] = ("ETH-USD", "BTC-USD")
DEFAULT_PRODUCT_ID = "ETH-USD"

# Maximum simultaneous open paper positions. When full, oldest position is
# closed at market (FIFO) to make room for a new trade signal.
MAX_OPEN_TRADES = 20

# When True, hourly DMs go only to subscribers on real trade actions (not no_trade).
BROADCAST_ONLY_TRADES = True

# Pre-broadcast audit refine loop (propose_trade retries after fact-check failures).
MAX_REFINE_PASSES = 3
RUN_LLM_CRITIC_PRE_BROADCAST = True

# Fixed-fraction position sizing: each trade deploys this fraction of live paper
# equity as notional (cash + open positions marked to spot). R/R, stop, and
# take-profit logic are unaffected — this only sets trade size.
TRADE_DEPLOY_PCT = 0.25

# M5 OB fib entry band (bullish: from block low; bearish: from block high).
ENTRY_FIB_LOW = 0.25
ENTRY_FIB_HIGH = 0.50
ENTRY_FIB_TRANCHE_1 = 0.25  # 50% of base deploy at this level
ENTRY_FIB_TRANCHE_2 = 0.50  # remaining 50% of base deploy
ADD_FIB_LEVEL = 0.718  # scale-in adds another full TRADE_DEPLOY_PCT
ENTRY_TRANCHE_DEPLOY_PCT = TRADE_DEPLOY_PCT / 2  # 12.5% per tranche
ADD_DEPLOY_PCT = TRADE_DEPLOY_PCT  # +25% at 0.718 → 1.25× base exposure
FIB_LEVEL_TOLERANCE_PCT = 0.008  # looser "near" fib mark for M5 watchdog

# Paper position size guardrails per product, applied after fixed-fraction sizing.
# Legacy aliases MIN_ETH_QTY / MAX_ETH_QTY keep older call sites working.
PRODUCT_QTY_CAPS: dict[str, tuple[float, float]] = {
    "ETH-USD": (0.25, 2.0),
    "BTC-USD": (0.005, 0.05),
}
MIN_ETH_QTY = PRODUCT_QTY_CAPS["ETH-USD"][0]
MAX_ETH_QTY = PRODUCT_QTY_CAPS["ETH-USD"][1]

# Shared paper book: fake Fund deposit (placeholder for future real funding).
PAPER_CONTRIBUTION_USD = 1000.0
# Telegram user id reserved for the house seed stake in paper_contributions.
HOUSE_CONTRIBUTION_TELEGRAM_ID = 0

# Personal demo accounts (opt-in Accept/Reject). Separate from the house/agent book.
PAPER_ACCOUNT_SIZES: tuple[float, ...] = (500.0, 1000.0, 2500.0)
PAPER_ACCOUNT_DEFAULT_USD = 1000.0  # migration amount for legacy Funders
APPROVAL_WINDOW_MIN = 15
MISSED_CONNECTION_R = 0.5
# Minimum cash required to Accept / late-join a trade.
USER_MIN_DEPLOY_USD = 25.0
# One-time launch notice after personal-books migrate (ops may reset).
LAUNCH_NOTICE_SENT_KEY = "personal_books_launch_v1"

# Minimum OB zone width as % of mid price.
# HTF (H4) keeps the swing-style filter; M5 entry candles are much thinner.
# BTC H4 candles are typically narrower in % terms than ETH, so BTC uses a
# lower HTF floor while ETH keeps the original 1.25% swing filter.
OB_MIN_WIDTH_PCT = 1.25
OB_MIN_WIDTH_PCT_M5 = 0.15
PRODUCT_OB_MIN_WIDTH_PCT: dict[str, float] = {
    "ETH-USD": OB_MIN_WIDTH_PCT,
    "BTC-USD": 0.60,
}

# Label for the current paper epoch (shown on dashboard after reset).
PAPER_EPOCH_LABEL = "5k_usd"

# Sub-hourly programmatic entry scanner (charts + no LLM).
WATCHDOG_ENABLED = True
WATCHDOG_INTERVAL_SEC = 60  # 1 minute (valid range: 60–300)
WATCHDOG_COOLDOWN_SEC = 30 * 60  # 30 min — suppress repeat trigger on same M5 OB

# Macro headline context (RSS + webhook advisory layer).
MACRO_CONTEXT_ENABLED = True
MACRO_POLL_INTERVAL_SEC = 300  # 5 minutes
MACRO_MIN_SEVERITY_INJECT = 3
MACRO_PULSE_MIN_SEVERITY = 4
MACRO_WATCHDOG_GATE_MIN_SEVERITY = 4
MACRO_DEFAULT_TTL_HOURS = 24
MACRO_LLM_PROMOTE_THRESHOLD = 40  # keyword_score 0-100 before Haiku classify

# Hourly ETH price/volume z-score spike broadcasts.
ZMOVE_ENABLED = True
ZMOVE_INTERVAL_SEC = 300  # 5 minutes
ZMOVE_THRESHOLD = 2.0
ZMOVE_LOOKBACK_H = 168  # 1 week of hourly bars
ZMOVE_COOLDOWN_SEC = 2 * 60 * 60  # 2 hours per metric
ZMOVE_PRODUCT_ID = "ETH-USD"

# W1 ETH/BTC relative-strength bias injected into prompts and watchdog soft-gates.
RELATIVE_STRENGTH_ENABLED = True


def qty_caps(product_id: str) -> tuple[float, float]:
    """Return (min_qty, max_qty) for a product; fall back to ETH caps."""
    return PRODUCT_QTY_CAPS.get(product_id, PRODUCT_QTY_CAPS["ETH-USD"])


def ob_min_width_pct(product_id: str | None = None) -> float:
    """HTF OB/breaker minimum width (% of mid) for a product."""
    if not product_id:
        return OB_MIN_WIDTH_PCT
    return float(
        PRODUCT_OB_MIN_WIDTH_PCT.get(product_id, OB_MIN_WIDTH_PCT)
    )


def product_label(product_id: str) -> str:
    """Short asset label for UI copy (ETH, BTC, …)."""
    if product_id.endswith("-USD"):
        return product_id[: -len("-USD")]
    if "/" in product_id:
        return product_id
    return product_id
