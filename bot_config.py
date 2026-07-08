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

# Paper position size guardrails (ETH), applied after fixed-fraction sizing.
MIN_ETH_QTY = 0.25
MAX_ETH_QTY = 2.0

# Minimum OB zone width as % of mid price (H1 rule; applied to all timeframes).
OB_MIN_WIDTH_PCT = 1.25

# Label for the current paper epoch (shown on dashboard after reset).
PAPER_EPOCH_LABEL = "5k_usd"

# Sub-hourly programmatic entry scanner (charts + no LLM).
WATCHDOG_ENABLED = True
WATCHDOG_INTERVAL_SEC = 180  # 3 minutes (valid range: 60–300)
WATCHDOG_COOLDOWN_SEC = 6 * 3600  # suppress repeat trigger on same H1 OB

# Macro headline context (RSS + webhook advisory layer).
MACRO_CONTEXT_ENABLED = True
MACRO_POLL_INTERVAL_SEC = 300  # 5 minutes
MACRO_MIN_SEVERITY_INJECT = 3
MACRO_PULSE_MIN_SEVERITY = 4
MACRO_WATCHDOG_GATE_MIN_SEVERITY = 4
MACRO_DEFAULT_TTL_HOURS = 24
MACRO_LLM_PROMOTE_THRESHOLD = 40  # keyword_score 0-100 before Haiku classify
