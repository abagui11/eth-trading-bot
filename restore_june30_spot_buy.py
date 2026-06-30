#!/usr/bin/env python3
"""Restore the June 30 2026 spot_buy (cycle 152620Z) to ledger + paper."""

from __future__ import annotations

import config
import ledger
import paper
import research
from models import Suggestion

CYCLE_ID = "20260630T152620Z"
OPENED_AT = "2026-06-30T15:26:20Z"
CHART_PATH = ",".join(
    str(p)
    for name in (
        "20260630T152620Z_H12_structure.png",
        "20260630T152620Z_H1_entry.png",
    )
    for p in [config.CHARTS_DIR / name]
    if p.exists()
)

RATIONALE = """\
H12 structure: ETH has been in a clear HTF downtrend since May (series of LH/LL from ~2200 down to ~1547). However, price has now compressed into the active bullish H12 OB (1554.47–1586.51, displaced 2026-06-28T08:00), which sits directly on top of the Weekly Open (1569.40) and Monday Low (1547.77). This confluence creates a strong demand zone. The 24h range is 1547.08–1624.55 (4.9% wide, ranging=True), and price has not broken below the range — Monday Low 1547.77 held as support. Two confirmed bullish H1 SFPs printed at 1560.23 on 2026-06-28T23:00 and 2026-06-29T00:00, both reversed — textbook SFP structure matching sfp_examples.png. A bearish H1 SFP at 1587.09 on 2026-06-29T12:00 reversed price back into the OB, which is consistent with price sweeping the Monday Mid (1591.38) before returning to the OB demand zone. H4 chart confirms price is compressing inside the bullish H12 OB box (green, 1554.47–1586.51) with small-bodied candles and elevated green volume on the Jun 29 bounce — similar to the strategy_example.png setup where price consolidates inside OB before pushing higher. H1 chart shows price sitting between Weekly Open (1569.40) and Monday Mid (1591.38), oscillating inside the 24h range. Entry is split: 0.5 units at 0.618 fib of H12 OB (~1572) and 0.5 units at 0.786 fib (~1566); blended entry ~1572. SL is set 0.25% below Monday Low / 24h range low at 1547.77 → SL = 1543.0. TP1 = Daily Open 1610.60, TP2 = Prev Week Mid 1643.91, TP3 = H12 BRKR/OB confluence ~1677.68. R/R = (1610.60 - 1572) / (1572 - 1543) = 38.6 / 29 ≈ 1.33 to TP1; blended across 3 TPs = 2.27. HTF bias is cautiously long from OB with SFP confirmation; LTF confirms ranging with bullish SFPs off OB low. Setup matches OB + SFP entry per trading_setup.png and strategy_example.png. Risk: HTF trend remains bearish — this is a counter-trend long from OB support; invalidation if H12 closes below 1543.

(Restored manually — original cycle broadcast but ledger row was missing and paper was overwritten by backfill.)\
"""


def main() -> None:
    entry = 1572.0
    stop_loss = 1543.0
    take_profits = [1610.60, 1643.91, 1677.68]
    suggested_size = 0.32
    risk_reward = 2.27

    risk_usd = config.PAPER_PORTFOLIO_VALUE * 0.01
    sl_pct = abs(entry - stop_loss) / entry
    eth_qty = (risk_usd / sl_pct) / entry

    spot = research.get_spot_price()

    suggestion = Suggestion(
        action="spot_buy",
        size=suggested_size,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=take_profits,
        risk_reward=risk_reward,
        rationale=RATIONALE,
    )

    existing = ledger.get_suggestion_by_cycle_id(CYCLE_ID)
    if existing is None:
        ledger.append(
            suggestion,
            cycle_id=CYCLE_ID,
            price_at_suggestion=entry,
            chart_path=CHART_PATH,
            ts=OPENED_AT,
            setup_tags="ranging,h1_sfp_bullish,h12_ob",
        )
        print(f"Ledger: appended spot_buy for {CYCLE_ID}")
    else:
        print(f"Ledger: suggestion for {CYCLE_ID} already exists (id={existing['id']})")

    paper.restore_open_position(
        action="spot_buy",
        entry=entry,
        eth_qty=eth_qty,
        stop_loss=stop_loss,
        take_profits=take_profits,
        risk_reward=risk_reward,
        suggested_size=suggested_size,
        opened_at=OPENED_AT,
        open_cycle_id=CYCLE_ID,
        spot_price=spot,
        force=True,
    )
    print(f"Paper: restored long {eth_qty:.4f} ETH @ {entry:,.2f}")
    print()
    print(paper.format_position_detail(spot))
    print()
    print(paper.format_pnl_footer(spot))


if __name__ == "__main__":
    main()
