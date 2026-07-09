"""Fetch spot/perp volume, funding, dominance, and miner breakeven proxies."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

import config
from metrics.cache import get_or_fetch

logger = logging.getLogger(__name__)

_BINANCE_FAPI = "https://fapi.binance.com"
_COINGECKO = "https://api.coingecko.com/api/v3"
_HASHRATE_INDEX = "https://api.hashrateindex.com/v1/hashprice/current"
_BLOCKCHAIN_STATS = "https://api.blockchain.info/stats"


@dataclass
class SpotVolume:
    volume_24h_base: float
    volume_24h_quote: float


@dataclass
class PerpVolume:
    symbol: str
    volume_24h_quote: float


@dataclass
class FundingSnapshot:
    symbol: str
    current_rate_pct: float
    next_funding_time: str | None
    avg_7d_pct: float | None
    min_7d_pct: float | None
    max_7d_pct: float | None


@dataclass
class DominanceSnapshot:
    btc_dominance_pct: float
    usdt_dominance_pct: float | None
    total_market_cap_usd: float | None


@dataclass
class MinerBreakevenSnapshot:
    hashprice_usd_per_ph_per_day: float | None
    estimated_breakeven_usd: float | None
    btc_spot_usd: float | None
    method: str
    note: str


def _get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_spot_volume() -> SpotVolume:
    def _fetch() -> SpotVolume:
        url = f"{config.MARKET_DATA_API}/products/ETH-USD/stats"
        data = _get_json(url)
        base_vol = float(data.get("volume", 0) or 0)
        last = float(data.get("last", 0) or 0)
        return SpotVolume(volume_24h_base=base_vol, volume_24h_quote=base_vol * last)

    return get_or_fetch("spot_volume_eth", _fetch)


def fetch_perp_volume(symbol: str = "ETHUSDT") -> PerpVolume:
    def _fetch() -> PerpVolume:
        data = _get_json(f"{_BINANCE_FAPI}/fapi/v1/ticker/24hr", {"symbol": symbol})
        return PerpVolume(
            symbol=symbol,
            volume_24h_quote=float(data.get("quoteVolume", 0) or 0),
        )

    return get_or_fetch(f"perp_volume_{symbol}", _fetch)


def fetch_funding(symbol: str = "ETHUSDT") -> FundingSnapshot:
    def _fetch() -> FundingSnapshot:
        premium = _get_json(f"{_BINANCE_FAPI}/fapi/v1/premiumIndex", {"symbol": symbol})
        current = float(premium.get("lastFundingRate", 0) or 0) * 100.0
        next_time = premium.get("nextFundingTime")
        next_str = str(next_time) if next_time else None

        history = _get_json(
            f"{_BINANCE_FAPI}/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": 21},
        )
        rates = [float(row.get("fundingRate", 0) or 0) * 100.0 for row in history]
        avg_7d = sum(rates) / len(rates) if rates else None
        return FundingSnapshot(
            symbol=symbol,
            current_rate_pct=current,
            next_funding_time=next_str,
            avg_7d_pct=avg_7d,
            min_7d_pct=min(rates) if rates else None,
            max_7d_pct=max(rates) if rates else None,
        )

    return get_or_fetch(f"funding_{symbol}", _fetch)


def fetch_dominance() -> DominanceSnapshot:
    def _fetch() -> DominanceSnapshot:
        global_data = _get_json(f"{_COINGECKO}/global")
        g = global_data.get("data") or {}
        btc_dom = float((g.get("market_cap_percentage") or {}).get("btc", 0) or 0)
        total_mcap = float(g.get("total_market_cap", {}).get("usd", 0) or 0)

        usdt_dom: float | None = None
        try:
            tether = _get_json(f"{_COINGECKO}/coins/tether")
            usdt_mcap = float((tether.get("market_data") or {}).get("market_cap", {}).get("usd", 0) or 0)
            if total_mcap > 0 and usdt_mcap > 0:
                usdt_dom = (usdt_mcap / total_mcap) * 100.0
        except Exception:
            logger.warning("USDT dominance fetch failed", exc_info=True)

        return DominanceSnapshot(
            btc_dominance_pct=btc_dom,
            usdt_dominance_pct=usdt_dom,
            total_market_cap_usd=total_mcap if total_mcap > 0 else None,
        )

    return get_or_fetch("dominance", _fetch)


def _fetch_btc_spot_usd() -> float | None:
    try:
        url = f"{config.MARKET_DATA_API}/products/BTC-USD"
        data = _get_json(url)
        product = data.get("product") or data
        price = product.get("price")
        if price is not None:
            return float(price)
    except Exception:
        logger.warning("BTC spot fetch failed", exc_info=True)
    return None


def fetch_miner_breakeven() -> MinerBreakevenSnapshot:
    def _fetch() -> MinerBreakevenSnapshot:
        btc_spot = _fetch_btc_spot_usd()
        hashprice: float | None = None
        method = "hashprice_proxy"
        note = "Approximate — based on public hashprice data and a simplified cost model."

        try:
            data = _get_json(_HASHRATE_INDEX)
            # API shape: {"hashprice": {"USD": {"PH": {"day": 123.45}}}}
            hp = data.get("hashprice") or data
            if isinstance(hp, dict):
                usd = hp.get("USD") or hp.get("usd") or {}
                ph = usd.get("PH") or usd.get("ph") or {}
                day_val = ph.get("day") or ph.get("Day")
                if day_val is not None:
                    hashprice = float(day_val)
        except Exception:
            logger.warning("Hashrate Index hashprice fetch failed", exc_info=True)

        if hashprice is None:
            try:
                stats = _get_json(_BLOCKCHAIN_STATS)
                # Fallback: very rough proxy using hash rate and BTC price
                # Not a true breakeven — labeled clearly in report
                method = "blockchain_stats_fallback"
                note = (
                    "Hashprice API unavailable. Using blockchain.info hash rate "
                    "as a rough context proxy only — not a precise breakeven."
                )
                hash_rate = float(stats.get("hash_rate", 0) or 0)
                if btc_spot and hash_rate > 0:
                    # Placeholder: no true breakeven without electricity assumptions
                    hashprice = None
            except Exception:
                logger.warning("Blockchain stats fallback failed", exc_info=True)

        estimated_breakeven: float | None = None
        if hashprice is not None and hashprice > 0 and btc_spot:
            # Simplified: miners broadly underwater when hashprice (revenue/PH/day)
            # implies block reward economics below operating cost band.
            # Use inverse relationship: higher hashprice => higher cost tolerance.
            # Breakeven proxy ≈ spot * (baseline_hashprice / current_hashprice)
            baseline_hashprice = 80.0  # USD/PH/day reference band
            ratio = baseline_hashprice / hashprice
            estimated_breakeven = btc_spot * max(0.5, min(1.5, ratio))

        if hashprice is None and btc_spot is None:
            return MinerBreakevenSnapshot(
                hashprice_usd_per_ph_per_day=None,
                estimated_breakeven_usd=None,
                btc_spot_usd=None,
                method="unavailable",
                note="Miner breakeven data temporarily unavailable.",
            )

        return MinerBreakevenSnapshot(
            hashprice_usd_per_ph_per_day=hashprice,
            estimated_breakeven_usd=estimated_breakeven,
            btc_spot_usd=btc_spot,
            method=method,
            note=note,
        )

    return get_or_fetch("miner_breakeven", _fetch)
