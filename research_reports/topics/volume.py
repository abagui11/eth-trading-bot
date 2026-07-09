"""Spot vs perp volume research report."""

from __future__ import annotations

from metrics import fetch
from research_reports.format import ResearchReport


def _fmt_usd(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    return f"${value:,.0f}"


def build_volume_report() -> ResearchReport:
    errors: list[str] = []
    spot = None
    perp = None
    try:
        spot = fetch.fetch_spot_volume()
    except Exception as exc:
        errors.append(f"Spot: {exc}")
    try:
        perp = fetch.fetch_perp_volume()
    except Exception as exc:
        errors.append(f"Perp: {exc}")

    if spot is None and perp is None:
        return ResearchReport(
            topic="volume",
            title="Volume",
            headline="Volume data temporarily unavailable.",
            sections=[("Error", [f"• {e}" for e in errors])],
            interpretation=["Retry later."],
            sources=["Coinbase H1 candles", "Binance Futures API", "Bybit API"],
        )

    metrics: list[str] = []
    if spot:
        metrics.append(
            f"• Coinbase ETH-USD spot 24h: {_fmt_usd(spot.volume_24h_quote)} "
            f"({spot.volume_24h_base:,.0f} ETH, {spot.source})"
        )
    if perp:
        metrics.append(
            f"• {perp.source.title()} {perp.symbol} perp 24h: {_fmt_usd(perp.volume_24h_quote)}"
        )
    if spot and perp and perp.volume_24h_quote > 0:
        ratio = spot.volume_24h_quote / perp.volume_24h_quote
        metrics.append(f"• Spot/perp quote volume ratio: {ratio:.2f}x")
    for err in errors:
        metrics.append(f"• Warning: {err}")

    headline = "ETH spot vs perp 24h volume"
    interpretation = [
        "Higher perp volume often means more speculative leverage flow vs spot.",
        "Volume spikes alone are not entries — confirm with structure (OB, SFP, zones).",
    ]

    return ResearchReport(
        topic="volume",
        title="Volume",
        headline=headline,
        sections=[("24h volume", metrics)],
        interpretation=interpretation,
        sources=["Coinbase H1 candles", "Binance Futures API", "Bybit API"],
    )
