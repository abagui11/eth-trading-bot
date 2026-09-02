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

# Annotated close-chart for Eva (HQ) live trades. One Haiku call per close;
# numbers always come from the ledger. Off skips generation entirely.
CASE_STUDY_ENABLED = True
USE_LLM_CASE_STUDY = True

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

# How often the full LLM trade cycle runs. Slots are wall-clock aligned, so
# 1800 fires on :00 and :30. Halving this doubles idea flow and LLM spend; how
# many of those ideas can be *held* is bounded by LIVE_MAX_OPEN_HQ, so the
# practical effect is that a freed slot refills within 30 minutes instead of an
# hour. Keep at or above IDEA_EXPIRY_MINUTES so a card cannot outlive its cycle.
CYCLE_INTERVAL_SEC = 1800  # 30 minutes

# How long an untouched paper entry waits for its pullback before being
# dropped. Eva plans limits into an M5 order block, so a plan is only valid
# while the structure that justified it is; a level that has not been traded in
# this long is stale, not patient. A fresh suggestion for the same product
# supersedes the pending one regardless, so this is a backstop for products
# that stop being suggested at all.
PAPER_PENDING_EXPIRY_HOURS: float = 4.0

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
LIVE_TRADE_DEPLOY_PCT = 0.50         # fallback notional when no clip is set
LIVE_MAX_OPEN_HQ = 4                 # skip new ideas when full (no FIFO kill)
LIVE_MAX_PER_PRODUCT_HQ = 2          # concurrent positions in one product
LIVE_DAILY_LOSS_LIMIT_USD = 160.0    # 8% of sleeve → halt until next UTC day
# Notional ceiling, not a risk ceiling — per-trade risk is bounded by
# LIVE_HQ_RISK_PCT. Four concurrent clips at their widest (two tight-stop ETH
# at 4 nanos, ~$960 each, plus two BTC at ~$800) is ~$3,520, which needs 1.76x.
# Below that the clip is trimmed to fit rather than refused, so too low a value
# shows up as undersized clips rather than skips. 2x stays the hard cap.
LIVE_MAX_LEVERAGE = 1.8              # notional ≤ sleeve × this (hard cap 2x)
LIVE_SCALE_IN_ENABLED = False        # 0.718 adds are paper-only on live
# Live qty floors per product = one CDE nano contract (orders are whole
# contracts; anything smaller cannot execute). ETH 0.1 ≈ $250, BTC 0.01 ≈ $800.
LIVE_PRODUCT_QTY_FLOORS: dict[str, float] = {
    "ETH-USD": 0.1,
    "BTC-USD": 0.01,
}
# An HQ clip is however many whole nano contracts fit this much risk, measured
# against the price the market order actually fills at rather than the planned
# entry. Eva's entries are pullbacks into an M5 order block, so the fill
# routinely sits away from an untouched stop; sizing off the plan let the clip
# risk 1.39x what was intended (2.20x worst) across the first 15 HQ positions.
#
# Risk-based sizing is also what makes the stop study's numbers transferable.
# At constant dollar risk a 1.5x stop is a 0.67x position, so widening a stop
# costs upside rather than adding exposure. Size the clip any other way and a
# wider stop is simply more risk, and the measured R-multiples do not carry
# over. Levels are never moved to fit the budget; only the clip changes.
#
# 0.7% of the sleeve ($14) sizes an ETH clip at roughly 2-6 nanos depending on
# stop distance. Raised from 0.5% on 2026-09-02 because one BTC nano — its
# smallest tradeable size — risks $5.33-$15.43 across the recorded book, so a
# $10 budget refused BTC as `risk_cap` whenever its stop ran wider than 1,000
# points. At $14 only the 1,543-point outlier is still refused.
#
# This treats a symptom. The cause is that one BTC nano is ~39% of sleeve
# notional and cannot ladder, so BTC's risk granularity is coarser than a
# $2,000 sleeve can express. See section 10 of PROJECT_STATE.
LIVE_HQ_RISK_PCT: float = 0.007
# Watchdog live execution is gated separately from paper execute.
WATCHDOG_LIVE_ENABLED = False
WATCHDOG_LIVE_META_KEY = "watchdog_live_enabled"

# Volume-mill live sleeve (same Coinbase account, internal partition).
# Every mill clip is exactly one CDE nano contract (LIVE_PRODUCT_QTY_FLOORS):
# 0.1 ETH or 0.01 BTC. Notional is then qty × mark; a contract that no longer
# fits the sleeve is rejected by the exposure check. Capital (sleeve + open
# count + daily loss) is the limiter — not a daily fill count. A closed clip
# frees its capital.
LIVE_MILL_SLEEVE_USD = 1400.0
LIVE_MILL_MAX_OPEN = 3
LIVE_MILL_MAX_FILLS_PER_DAY = 0      # 0 = no daily fill cap
LIVE_MILL_DAILY_LOSS_LIMIT_USD = 112.0  # 8% of sleeve, same ratio as HQ

# Objective: keep a mill clip open at all times. When the sleeve is EMPTY the
# next sized idea at or above this confidence self-fills (FIFO — the first
# qualifying mint wins the slot). Once one clip is open the remaining slots
# are reserved for manual Accepts, so the auto path can never crowd them out.
LIVE_MILL_AUTO_FILL_ENABLED = True
LIVE_MILL_AUTO_MIN_CONFIDENCE = 0.5

# Telegram ids whose Accept fills a real clip, bypassing the conviction gate.
# Everyone else's Accept stays paper-only (user_paper_trades).
LIVE_MILL_FILL_TELEGRAM_IDS: tuple[int, ...] = (8282981740, 2037245798)

# --- Accept-time revalidation -------------------------------------------
# An idea is priced when it is minted and filled whenever someone taps Accept,
# which can be minutes later. The levels are re-checked against the live mark
# before any money moves: targets the market has already taken are dropped, and
# a setup the drift has ruined is refused rather than filled at a worse price.
LIVE_REVALIDATE_ON_FILL: bool = True
# How far price may run past the entry, in units of the planned risk, before an
# Accept counts as chasing. Holding the stop still while the entry drifts would
# quietly turn a 1R trade into a 2R one on a fixed-notional clip.
LIVE_MAX_CHASE_R: float = 0.5
# Reward:risk floor for the re-anchored plan, measured against the average of
# the targets still ahead — not TP1, which a scale-out ladder puts close in on
# purpose. This is a backstop against ideas that were poor to begin with.
LIVE_MIN_FILL_RR: float = 1.0
# A target closer than this to the mark is not worth resting an order against.
LIVE_TP_MIN_EDGE_PCT: float = 0.1

# --- Idea lifecycle -----------------------------------------------------
# How long a posted card stays acceptable. Past this it is marked expired, so
# silence becomes an explicit pass instead of an offer that never closes, and a
# late Accept is refused rather than filling a stale setup. 0 disables.
IDEA_EXPIRY_MINUTES: int = 15
# When a mill clip closes, replay the recent backlog to refill the sleeve.
# Auto-fill otherwise only ever fires at the moment an idea is minted, so a
# closed clip left the sleeve idle until the next mint happened to land.
LIVE_MILL_REOFFER_ENABLED: bool = True
# How far back the sweep looks. Deliberately longer than IDEA_EXPIRY_MINUTES:
# expiry governs what a person may still tap Accept on, where a stale card is
# judged by eye, whereas the sweep re-prices every candidate against the live
# mark first — revalidation, not the clock, is what keeps it honest. At the
# mill's bursty ~20-30 fillable ideas a day, a 15-minute lookback would leave
# the sweep with nothing to replay in most windows.
LIVE_MILL_REOFFER_MAX_AGE_MIN: int = 120

# Every live open/close/halt is pushed to these chats on top of
# TELEGRAM_ADMIN_CHAT_ID. Both sleeves now fill without a human in the loop, so
# a real fill must never be discoverable only by reading the journal.
LIVE_ALERT_TELEGRAM_IDS: tuple[int, ...] = LIVE_MILL_FILL_TELEGRAM_IDS
LIVE_FILL_ALERTS_ENABLED = True

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
# Source is the mill volume paper book (ideas subscribers actually follow),
# not Eva HQ paper. MILL_PAPER_EPOCH_START cuts off the pre-restart mill.
DAILY_PERFORMANCE_POST_ENABLED = True
DAILY_DIGEST_HOUR_UTC = 21  # 21:00 UTC ≈ 5pm ET
DAILY_DIGEST_SOURCE = "mill"
MILL_PAPER_EPOCH_START = "2026-09-01"  # UTC; volume paper opened_at >= this


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