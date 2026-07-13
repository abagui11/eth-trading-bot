"""Bot runtime configuration (non-secret tunables)."""

from __future__ import annotations

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

# Paper position size guardrails (ETH), applied after fixed-fraction sizing.
MIN_ETH_QTY = 0.25
MAX_ETH_QTY = 2.0

# Minimum OB zone width as % of mid price (applied to all timeframes).
OB_MIN_WIDTH_PCT = 1.25

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
