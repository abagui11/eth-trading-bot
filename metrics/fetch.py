"""Fetch spot/perp volume, funding, dominance, and miner breakeven proxies."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import requests

import config
import research
from metrics.cache import get_or_fetch

logger = logging.getLogger(__name__)

T = TypeVar("T")

_BINANCE_FAPI = "https://fapi.binance.com"
_BYBIT_API = "https://api.bybit.com"
_COINGECKO = "https://api.coingecko.com/api/v3"
_HASHRATE_INDEX_URLS = (
    "https://api.hashrateindex.com/v1/hashprice/current",
    "https://data.hashrateindex.com/v1/hashprice/current",
)
_BLOCKCHAIN_STATS = "https://api.blockchain.info/stats"

_BLOCK_REWARD_BTC = 3.125
_BLOCKS_PER_DAY = 144
_BASELINE_HASHPRICE_USD_PH_DAY = 80.0


@dataclass
class SpotVolume:
    volume_24h_base: float
    volume_24h_quote: float
    source: str = "coinbase_h1"


@dataclass
class PerpVolume:
    symbol: str
    volume_24h_quote: float
    source: str = "binance"


@dataclass
class FundingSnapshot:
    symbol: str
    current_rate_pct: float
    next_funding_time: str | None
    avg_7d_pct: float | None
    min_7d_pct: float | None
    max_7d_pct: float | None
    source: str = "binance"


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


def _first_success(fetchers: list[Callable[[], T]], label: str) -> T:
    errors: list[str] = []
    for fn in fetchers:
        try:
            return fn()
        except Exception as exc:
            errors.append(str(exc))
            logger.warning("%s fetcher failed: %s", label, exc)
    raise RuntimeError(f"All {label} fetchers failed: {'; '.join(errors)}")


def _spot_volume_from_h1() -> SpotVolume:
    bars = research.get_ohlc("H1", limit=24)
    if not bars:
        raise RuntimeError("No H1 bars for spot volume")
    base_vol = sum(float(b["volume"]) for b in bars)
    quote_vol = sum(float(b["volume"]) * float(b["close"]) for b in bars)
    return SpotVolume(
        volume_24h_base=base_vol,
        volume_24h_quote=quote_vol,
        source="coinbase_h1",
    )


def fetch_spot_volume() -> SpotVolume:
    return get_or_fetch("spot_volume_eth", _spot_volume_from_h1)


def _perp_volume_binance(symbol: str = "ETHUSDT") -> PerpVolume:
    data = _get_json(f"{_BINANCE_FAPI}/fapi/v1/ticker/24hr", {"symbol": symbol})
    return PerpVolume(
        symbol=symbol,
        volume_24h_quote=float(data.get("quoteVolume", 0) or 0),
        source="binance",
    )


def _perp_volume_bybit(symbol: str = "ETHUSDT") -> PerpVolume:
    data = _get_json(
        f"{_BYBIT_API}/v5/market/tickers",
        {"category": "linear", "symbol": symbol},
    )
    rows = (data.get("result") or {}).get("list") or []
    if not rows:
        raise RuntimeError("Bybit ticker list empty")
    row = rows[0]
    turnover = float(row.get("turnover24h") or row.get("volume24h") or 0)
    return PerpVolume(symbol=symbol, volume_24h_quote=turnover, source="bybit")


def fetch_perp_volume(symbol: str = "ETHUSDT") -> PerpVolume:
    def _fetch() -> PerpVolume:
        return _first_success(
            [lambda: _perp_volume_binance(symbol), lambda: _perp_volume_bybit(symbol)],
            "perp_volume",
        )

    return get_or_fetch(f"perp_volume_{symbol}", _fetch)


def _funding_binance(symbol: str = "ETHUSDT") -> FundingSnapshot:
    premium = _get_json(f"{_BINANCE_FAPI}/fapi/v1/premiumIndex", {"symbol": symbol})
    current = float(premium.get("lastFundingRate", 0) or 0) * 100.0
    next_time = premium.get("nextFundingTime")
    history = _get_json(
        f"{_BINANCE_FAPI}/fapi/v1/fundingRate",
        {"symbol": symbol, "limit": 21},
    )
    rates = [float(row.get("fundingRate", 0) or 0) * 100.0 for row in history]
    return FundingSnapshot(
        symbol=symbol,
        current_rate_pct=current,
        next_funding_time=str(next_time) if next_time else None,
        avg_7d_pct=sum(rates) / len(rates) if rates else None,
        min_7d_pct=min(rates) if rates else None,
        max_7d_pct=max(rates) if rates else None,
        source="binance",
    )


def _funding_bybit(symbol: str = "ETHUSDT") -> FundingSnapshot:
    ticker = _get_json(
        f"{_BYBIT_API}/v5/market/tickers",
        {"category": "linear", "symbol": symbol},
    )
    rows = (ticker.get("result") or {}).get("list") or []
    if not rows:
        raise RuntimeError("Bybit ticker list empty")
    row = rows[0]
    current = float(row.get("fundingRate", 0) or 0) * 100.0
    next_time = row.get("nextFundingTime")

    history = _get_json(
        f"{_BYBIT_API}/v5/market/funding/history",
        {"category": "linear", "symbol": symbol, "limit": 21},
    )
    hist_rows = (history.get("result") or {}).get("list") or []
    rates = [float(r.get("fundingRate", 0) or 0) * 100.0 for r in hist_rows]
    return FundingSnapshot(
        symbol=symbol,
        current_rate_pct=current,
        next_funding_time=str(next_time) if next_time else None,
        avg_7d_pct=sum(rates) / len(rates) if rates else None,
        min_7d_pct=min(rates) if rates else None,
        max_7d_pct=max(rates) if rates else None,
        source="bybit",
    )


def fetch_funding(symbol: str = "ETHUSDT") -> FundingSnapshot:
    def _fetch() -> FundingSnapshot:
        return _first_success(
            [lambda: _funding_binance(symbol), lambda: _funding_bybit(symbol)],
            "funding",
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


def _parse_hashprice_payload(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None
    hp = data.get("hashprice") or data.get("data") or data
    if not isinstance(hp, dict):
        return None
    usd = hp.get("USD") or hp.get("usd") or hp
    if isinstance(usd, (int, float)):
        return float(usd)
    if isinstance(usd, dict):
        ph = usd.get("PH") or usd.get("ph") or {}
        if isinstance(ph, dict):
            day_val = ph.get("day") or ph.get("Day")
            if day_val is not None:
                return float(day_val)
        for key in ("day", "Day", "daily"):
            if key in usd and usd[key] is not None:
                return float(usd[key])
    return None


def _hashprice_from_blockchain_stats(btc_spot: float | None) -> tuple[float | None, str]:
    """Estimate USD/PH/day from network hash rate and block rewards."""
    stats = _get_json(_BLOCKCHAIN_STATS)
    hash_rate_gh = float(stats.get("hash_rate", 0) or 0)
    if hash_rate_gh <= 0 or not btc_spot:
        return None, "blockchain_stats_fallback"
    network_ph = hash_rate_gh / 1e6  # GH/s -> PH/s
    if network_ph <= 0:
        return None, "blockchain_stats_fallback"
    daily_revenue_usd = _BLOCK_REWARD_BTC * _BLOCKS_PER_DAY * btc_spot
    hashprice = daily_revenue_usd / network_ph
    return hashprice, "blockchain_network_revenue"


def fetch_miner_breakeven() -> MinerBreakevenSnapshot:
    def _fetch() -> MinerBreakevenSnapshot:
        btc_spot = _fetch_btc_spot_usd()
        hashprice: float | None = None
        method = "hashprice_proxy"
        note = "Approximate — based on public hashprice or network revenue model."

        for url in _HASHRATE_INDEX_URLS:
            try:
                hashprice = _parse_hashprice_payload(_get_json(url))
                if hashprice is not None:
                    method = "hashrate_index"
                    break
            except Exception:
                logger.warning("Hashrate Index fetch failed for %s", url, exc_info=True)

        if hashprice is None:
            try:
                hashprice, method = _hashprice_from_blockchain_stats(btc_spot)
                if hashprice is not None:
                    note = (
                        "Hashprice API unavailable. Estimated from blockchain.info "
                        "network hash rate and block rewards — approximate only."
                    )
            except Exception:
                logger.warning("Blockchain hashprice estimate failed", exc_info=True)

        estimated_breakeven: float | None = None
        if hashprice is not None and hashprice > 0 and btc_spot:
            ratio = _BASELINE_HASHPRICE_USD_PH_DAY / hashprice
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
