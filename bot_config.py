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

# Paper position size bounds (ETH) after 1% risk sizing.
MIN_ETH_QTY = 0.25
MAX_ETH_QTY = 1.0

# Label for the current paper epoch (shown on dashboard after reset).
PAPER_EPOCH_LABEL = "5k_usd"

# Sub-hourly programmatic entry scanner (charts + no LLM).
WATCHDOG_ENABLED = True
WATCHDOG_INTERVAL_SEC = 180  # 3 minutes (valid range: 60–300)
WATCHDOG_COOLDOWN_SEC = 6 * 3600  # suppress repeat trigger on same H1 OB
