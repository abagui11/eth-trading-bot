"""BTC miner breakeven estimate research report."""

from __future__ import annotations

from metrics import fetch
from research_reports.format import ResearchReport


def build_miner_report() -> ResearchReport:
    try:
        snap = fetch.fetch_miner_breakeven()
    except Exception as exc:
        return ResearchReport(
            topic="miner",
            title="Miner Breakeven (est.)",
            headline="Miner breakeven data temporarily unavailable.",
            sections=[("Error", [f"• {exc}"])],
            interpretation=["Retry later."],
            sources=["Hashrate Index API", "Coinbase BTC-USD"],
        )

    metrics: list[str] = []
    if snap.btc_spot_usd:
        metrics.append(f"• BTC spot: ${snap.btc_spot_usd:,.0f}")
    if snap.hashprice_usd_per_ph_per_day is not None:
        metrics.append(
            f"• Hashprice: ${snap.hashprice_usd_per_ph_per_day:,.2f} / PH / day"
        )
    if snap.estimated_breakeven_usd is not None:
        metrics.append(f"• Estimated breakeven (approx.): ${snap.estimated_breakeven_usd:,.0f}")
        if snap.btc_spot_usd:
            diff_pct = ((snap.btc_spot_usd - snap.estimated_breakeven_usd) / snap.estimated_breakeven_usd) * 100
            metrics.append(f"• Spot vs est. breakeven: {diff_pct:+.1f}%")
    else:
        metrics.append("• Estimated breakeven: unavailable (insufficient hashprice data)")

    metrics.append(f"• Method: {snap.method}")

    if snap.estimated_breakeven_usd and snap.btc_spot_usd:
        if snap.btc_spot_usd < snap.estimated_breakeven_usd:
            headline = f"BTC ${snap.btc_spot_usd:,.0f} — below approximate miner breakeven band"
        else:
            headline = f"BTC ${snap.btc_spot_usd:,.0f} — above approximate miner breakeven band"
    else:
        headline = "BTC miner economics (approximate)"

    interpretation = [
        snap.note,
        "Miner stress can add macro tail risk to crypto — supplementary to ETH chart structure.",
    ]

    return ResearchReport(
        topic="miner",
        title="Miner Breakeven (est.)",
        headline=headline,
        sections=[("Mining economics", metrics)],
        interpretation=interpretation,
        sources=["Hashrate Index API", "Coinbase BTC-USD", "blockchain.info (fallback)"],
    )
