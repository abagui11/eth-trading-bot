"""Bot runtime configuration (non-secret tunables)."""

from __future__ import annotations

# Maximum simultaneous open paper positions. When full, oldest position is
# closed at market (FIFO) to make room for a new trade signal.
MAX_OPEN_TRADES = 20

# When True, hourly DMs go only to subscribers on real trade actions (not no_trade).
BROADCAST_ONLY_TRADES = True
