"""BTC and USDT dominance research report."""

from __future__ import annotations

from metrics import fetch
from research_reports.format import ResearchReport


def build_dominance_report() -> ResearchReport:
    try:
        snap = fetch.fetch_dominance()
    except Exception as exc:
        return ResearchReport(
            topic="dominance",
            title="Dominance",
            headline="Dominance data temporarily unavailable.",
            sections=[("Error", [f"• {exc}"])],
            interpretation=["Retry later or check CoinGecko API connectivity."],
            sources=["CoinGecko API"],
        )

    metrics = [f"• BTC dominance: {snap.btc_dominance_pct:.2f}%"]
    if snap.usdt_dominance_pct is not None:
        metrics.append(f"• USDT dominance: {snap.usdt_dominance_pct:.2f}%")
    else:
        metrics.append("• USDT dominance: unavailable")
    if snap.total_market_cap_usd:
        tmc = snap.total_market_cap_usd
        if tmc >= 1e12:
            metrics.append(f"• Total crypto market cap: ${tmc / 1e12:.2f}T")
        else:
            metrics.append(f"• Total crypto market cap: ${tmc / 1e9:.0f}B")

    headline = f"BTC.D {snap.btc_dominance_pct:.1f}%"
    if snap.usdt_dominance_pct is not None:
        headline += f" | USDT.D {snap.usdt_dominance_pct:.1f}%"

    interpretation = [
        "Rising BTC.D often coincides with risk-off rotation into BTC from alts.",
        "Rising USDT.D can signal stablecoin hoarding / reduced risk appetite.",
        "Dominance shifts are context — ETH setups still need H12/H1 structure.",
    ]

    return ResearchReport(
        topic="dominance",
        title="Dominance",
        headline=headline,
        sections=[("Market structure", metrics)],
        interpretation=interpretation,
        sources=["CoinGecko API"],
    )
