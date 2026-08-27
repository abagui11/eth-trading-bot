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
MAX_REFINE_PASSES = 1
RUN_LLM_CRITIC_PRE_BROADCAST = False

# Attach Trading Guide/*.png reference examples to Claude vision calls.
# Off by default — live marked charts + guide text are enough and cut ~5 images/call.
INCLUDE_PATTERN_IMAGES = False

# LLM rewrite of trade-card blurbs (else deterministic_setup_blurb).
USE_LLM_DISPLAY_SUMMARY = False

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
# Scan/log always when WATCHDOG_ENABLED; paper fills + subscriber offers only when execute is on.
# Runtime override via user_books meta key WATCHDOG_EXECUTE_META_KEY (dashboard / Telegram).
WATCHDOG_EXECUTE_ENABLED = False
WATCHDOG_EXECUTE_META_KEY = "watchdog_execute_enabled"
# When execute is on, still block short fires unless this is True (inverted M5 short module).
WATCHDOG_ALLOW_SHORTS = False
# Scale-in only when unrealized P&L >= this multiple of 1R (entry→stop distance).
SCALE_IN_MIN_R = 0.5

# --- Live execution sleeves (Coinbase US futures — CDE nano contracts) --------
# All LIVE_* values are live-only. Paper sizing (TRADE_DEPLOY_PCT=0.25,
# PRODUCT_QTY_CAPS) is untouched — never reuse paper equity for live size.
LIVE_HQ_EQUITY_USD = 2000.0          # HQ ICT margin sleeve
LIVE_TRADE_DEPLOY_PCT = 0.50         # 50% of the HQ sleeve per idea ($1,000)
LIVE_MAX_OPEN_HQ = 2                 # skip new ideas when full (no FIFO kill)
LIVE_DAILY_LOSS_LIMIT_USD = 160.0    # 8% of sleeve → halt until next UTC day
LIVE_MAX_LEVERAGE = 1.0              # notional ≤ sleeve × 1 (1x; hard cap 2x)
LIVE_SCALE_IN_ENABLED = False        # 0.718 adds are paper-only on live
# Live qty floors per product = one CDE nano contract (orders are whole
# contracts; anything smaller cannot execute). ETH 0.1 ≈ $250, BTC 0.01 ≈ $800.
LIVE_PRODUCT_QTY_FLOORS: dict[str, float] = {
    "ETH-USD": 0.1,
    "BTC-USD": 0.01,
}
# Watchdog live execution is gated separately from paper execute.
WATCHDOG_LIVE_ENABLED = False
WATCHDOG_LIVE_META_KEY = "watchdog_live_enabled"

# Volume-mill tiny live sleeve (same Coinbase account, internal partition).
LIVE_MILL_SLEEVE_USD = 400.0
LIVE_MILL_NOTIONAL_USD = 80.0        # per idea
LIVE_MILL_MAX_OPEN = 2
LIVE_MILL_MAX_FILLS_PER_DAY = 2
LIVE_MILL_DAILY_LOSS_LIMIT_USD = 80.0

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

# --- Republic Intelligence layer ---------------------------------------------
# Hourly BTC/ETH stance batch (H4/H1/M15) persisted + served on /api/v1.
INTELLIGENCE_ENABLED = True
# When True, gate high-quality (abstention-first ICT) trade cards to the
# internal allowlist (config.INTERNAL_TELEGRAM_IDS). False = HQ cards go to
# all public subscribers with Accept/Reject and a "High Quality" label.
HQ_IDEAS_INTERNAL_ONLY = False

# Perp funding regime tracker (Binance public funding prints for BTC/ETH).
FUNDING_ENABLED = True
FUNDING_INTERVAL_SEC = 3600  # refresh once per hour (prints land every 8h)
FUNDING_PRODUCTS: dict[str, str] = {
    "BTC-USD": "BTCUSDT",
    "ETH-USD": "ETHUSDT",
}
# A persistence regime requires this many consecutive same-sign prints (8h
# prints -> 9 periods = 3 days).
FUNDING_PERSIST_PERIODS = 9
# A switch only counts once the new sign holds for this many prints; anything
# flippier than that is chop/noise.
FUNDING_SWITCH_CONFIRM_PERIODS = 3

# Long-horizon (4-year cycle) thesis: refreshed daily.
LONG_THESIS_ENABLED = True
LONG_THESIS_INTERVAL_SEC = 24 * 3600

# Once-daily performance digest ("you'd be up X%" + winner breakdown),
# posted as an X thread and mirrored to Telegram subscribers. X posting
# additionally requires TWITTER_ENABLED + keys in .env.
DAILY_PERFORMANCE_POST_ENABLED = True
DAILY_DIGEST_HOUR_UTC = 21  # 21:00 UTC ≈ 5pm ET


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


def watchdog_execute_enabled() -> bool:
    """Effective watchdog paper-execution flag (config default + runtime meta override)."""
    try:
        import user_books

        raw = user_books.get_meta(WATCHDOG_EXECUTE_META_KEY)
    except Exception:
        raw = None
    if raw is None or str(raw).strip() == "":
        return bool(WATCHDOG_EXECUTE_ENABLED)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def set_watchdog_execute_enabled(enabled: bool) -> bool:
    """Persist runtime override for watchdog paper execution. Returns new value."""
    import user_books

    user_books.set_meta(WATCHDOG_EXECUTE_META_KEY, "1" if enabled else "0")
    return enabled


def watchdog_live_enabled() -> bool:
    """Watchdog LIVE execution gate — separate from paper execute, and only
    meaningful when config.EXECUTION_MODE is shadow|live."""
    try:
        import user_books

        raw = user_books.get_meta(WATCHDOG_LIVE_META_KEY)
    except Exception:
        raw = None
    if raw is None or str(raw).strip() == "":
        return bool(WATCHDOG_LIVE_ENABLED)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def set_watchdog_live_enabled(enabled: bool) -> bool:
    """Persist runtime override for watchdog LIVE execution."""
    import user_books

    user_books.set_meta(WATCHDOG_LIVE_META_KEY, "1" if enabled else "0")
    return enabled