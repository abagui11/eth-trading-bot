#!/usr/bin/env python3
"""Backfill the June 27 2026 deriv_sell trade (missed broadcast)."""

from __future__ import annotations

import config
import ledger
import paper
import research
from models import Suggestion

CYCLE_ID = "20260627T172925Z"  # 1:29 PM ET June 27, 2026
OPENED_AT = "2026-06-27T17:29:25Z"

RATIONALE = """\
Signals: Price inside bearish H1 OB (1,568.23-1,583.82) - potential short setup

H12 structure is clearly bearish: series of LH/LL from May highs (~2300) all the way down to current ~1570, no sign of reversal. The June bounce to ~1820 formed a lower high and price has now broken down again to fresh lows near 1510. HTF bias is firmly short. H4 confirms: after the Jun 15 rally peak (~1845) price made a clear LH at ~1770 on Jun 22, then broke down sharply on Jun 24-25 with high volume displacement candles. Current H4 structure is bearish with price consolidating in the 1540-1590 zone following the breakdown. H1 shows price rallied from the ~1510 24h range low back up into the 1568-1584 bearish OB zone. Multiple bearish H1 SFPs printed at 1580-1584 on Jun 26 (07:00, 16:00, 17:00) — all pending confirmation but consistent with rejection from OB. 24h range is 1510-1591.67, ranging=True, width 5.3%. Price is currently near the top of this range (~1576) inside the bearish H1 OB (1568.23-1583.82). The OB aligns with the programmatic bearish OB 1535.73-1590.59 (displacement Jun 26 02:00) and the larger bearish OB 1510-1583.82. Entry at 1576 (0.618 retracement of OB range 1568-1584 = ~1574, using 1576 as market). SL at 1592 = 0.25% above the OB high / recent swing high at 1583.82, rounded to 1592 to clear the swing. Stop % = (1592-1576)/1576 = 1.015%. Position size = (1000 * 0.01) / 0.01015 = 0.985 ETH, capped at 0.64 for conservatism given ranging conditions and SFPs still pending. TP1 at 1545 (recent H1 swing low cluster), TP2 at 1515 (near 24h range low 1510), TP3 at 1490 (extension below range low targeting next liquidity). R/R: avg TP ~1517 vs entry 1576, reward ~59 pts; risk 16 pts = ~3.7R on full target, blended R/R ~2.19 across 3 TPs. Structure conflict check: HTF bearish, H4 bearish, H1 short setup inside OB with SFPs — all aligned. OB and SFP criteria met. R/R above 1.5. Executing deriv_sell suggestion.

(Resent manually — original broadcast failed.)\
"""


def main() -> None:
    entry = 1576.0
    stop_loss = 1592.0
    take_profits = [1545.0, 1515.0, 1490.0]
    suggested_size = 0.64
    risk_reward = 2.19

    # Same sizing formula as paper.update()
    risk_usd = config.PAPER_PORTFOLIO_VALUE * 0.01
    sl_pct = abs(entry - stop_loss) / entry
    notional = risk_usd / sl_pct
    eth_qty = notional / entry

    spot = research.get_spot_price()

    suggestion = Suggestion(
        action="deriv_sell",
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
            chart_path="",
            ts=OPENED_AT,
            setup_tags="bearish_ob,h1_sfp,range_24h",
        )
        print(f"Ledger: appended suggestion for {CYCLE_ID}")
    else:
        print(f"Ledger: suggestion for {CYCLE_ID} already exists (id={existing['id']})")

    paper.restore_open_position(
        action="deriv_sell",
        entry=entry,
        eth_qty=eth_qty,
        stop_loss=stop_loss,
        take_profits=take_profits,
        risk_reward=risk_reward,
        suggested_size=suggested_size,
        opened_at=OPENED_AT,
        open_cycle_id=CYCLE_ID,
        spot_price=spot,
    )
    print(f"Paper: restored short {eth_qty:.4f} ETH @ {entry:,.2f}")
    print()
    print(paper.format_position_detail(spot))
    print()
    print(paper.format_pnl_footer(spot))


if __name__ == "__main__":
    main()
