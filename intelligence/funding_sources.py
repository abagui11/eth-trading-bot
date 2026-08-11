"""Swappable perp funding-rate source adapters.

Every adapter normalizes at this boundary, so no venue-specific symbol, unit,
timestamp or interval convention leaks into the rest of the intelligence layer.
The contract is:

- input is a canonical product_id ("BTC-USD" / "ETH-USD"), never a venue symbol
- output is ``[{"ts": "YYYY-MM-DDTHH:MM:SSZ", "rate": float}]``, oldest-first
- ``rate`` is a signed decimal fraction for one funding interval (0.0001 ==
  0.01%), positive meaning longs pay shorts — the convention Binance, Bybit and
  OKX all publish natively, and the one ``intelligence.funding`` classifies on
- ``ts`` is the UTC settlement time of that print (not the interval start)

Source selection is env-driven so a venue that starts geo-blocking can be
swapped without a code change: ``FUNDING_SOURCE`` picks the primary and
``FUNDING_SOURCE_FALLBACKS`` is a comma-separated chain tried after it. Every
fallback hop is logged at WARNING and a chain that exhausts raises, so an
outage is never silently absorbed.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

# All three venues settle BTC/ETH USDT perp funding every 8h (00:00/08:00/16:00
# UTC). intelligence.funding's persistence windows are expressed in prints, so
# this is the only place the wall-clock meaning of a "period" is pinned down.
FUNDING_INTERVAL_HOURS = 8

DEFAULT_SOURCE = "okx"
DEFAULT_FALLBACKS = "bybit,binance"

_TIMEOUT = 20


class FundingSourceError(RuntimeError):
    """A funding source could not supply a usable series."""


def _iso_from_ms(raw: Any) -> str:
    """Venue ms-epoch (int or str) -> the UTC ISO stamp the store keys on."""
    return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class FundingSource:
    """Base adapter. Subclasses map canonical products onto one venue."""

    name: str = ""
    symbols: dict[str, str] = {}

    def symbol_for(self, product_id: str) -> str:
        try:
            return self.symbols[product_id]
        except KeyError:
            raise FundingSourceError(
                f"{self.name} has no symbol mapped for {product_id}"
            ) from None

    def fetch(self, product_id: str, *, limit: int = 90) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _get(self, url: str, params: dict[str, Any]) -> Any:
        response = requests.get(url, params=params, timeout=_TIMEOUT)
        response.raise_for_status()
        return response.json()


class OkxFundingSource(FundingSource):
    """OKX v5 public funding history. No API key, ~10 req/2s per IP.

    ``realizedRate`` is the rate actually charged at settlement and is the true
    analogue of Binance's historical ``fundingRate``; ``fundingRate`` on this
    endpoint is the announced rate and is used only as a fallback.
    """

    name = "okx"
    symbols = {"BTC-USD": "BTC-USDT-SWAP", "ETH-USD": "ETH-USDT-SWAP"}
    url = "https://www.okx.com/api/v5/public/funding-rate-history"
    max_limit = 100

    def fetch(self, product_id: str, *, limit: int = 90) -> list[dict[str, Any]]:
        payload = self._get(
            self.url,
            {
                "instId": self.symbol_for(product_id),
                "limit": min(limit, self.max_limit),
            },
        )
        if str(payload.get("code")) != "0":
            raise FundingSourceError(
                f"OKX error code={payload.get('code')} msg={payload.get('msg')}"
            )
        out: list[dict[str, Any]] = []
        for item in payload.get("data") or []:
            raw = item.get("realizedRate")
            if raw in (None, ""):
                raw = item.get("fundingRate")
            if raw in (None, ""):
                continue
            out.append(
                {"ts": _iso_from_ms(item["fundingTime"]), "rate": float(raw)}
            )
        out.sort(key=lambda r: r["ts"])
        return out


class BybitFundingSource(FundingSource):
    """Bybit v5 public funding history. No API key."""

    name = "bybit"
    symbols = {"BTC-USD": "BTCUSDT", "ETH-USD": "ETHUSDT"}
    url = "https://api.bybit.com/v5/market/funding/history"
    max_limit = 200

    def fetch(self, product_id: str, *, limit: int = 90) -> list[dict[str, Any]]:
        payload = self._get(
            self.url,
            {
                "category": "linear",
                "symbol": self.symbol_for(product_id),
                "limit": min(limit, self.max_limit),
            },
        )
        if int(payload.get("retCode", -1)) != 0:
            raise FundingSourceError(
                f"Bybit error retCode={payload.get('retCode')} "
                f"retMsg={payload.get('retMsg')}"
            )
        rows = (payload.get("result") or {}).get("list") or []
        out: list[dict[str, Any]] = []
        for item in rows:
            raw = item.get("fundingRate")
            if raw in (None, ""):
                continue
            out.append(
                {
                    "ts": _iso_from_ms(item["fundingRateTimestamp"]),
                    "rate": float(raw),
                }
            )
        out.sort(key=lambda r: r["ts"])
        return out


class BinanceFundingSource(FundingSource):
    """Binance USD-M funding history. Geo-blocked (HTTP 451) from some hosts."""

    name = "binance"
    symbols = {"BTC-USD": "BTCUSDT", "ETH-USD": "ETHUSDT"}
    url = "https://fapi.binance.com/fapi/v1/fundingRate"
    max_limit = 1000

    def fetch(self, product_id: str, *, limit: int = 90) -> list[dict[str, Any]]:
        payload = self._get(
            self.url,
            {
                "symbol": self.symbol_for(product_id),
                "limit": min(limit, self.max_limit),
            },
        )
        out = [
            {"ts": _iso_from_ms(item["fundingTime"]), "rate": float(item["fundingRate"])}
            for item in payload
        ]
        out.sort(key=lambda r: r["ts"])
        return out


_REGISTRY: dict[str, type[FundingSource]] = {
    OkxFundingSource.name: OkxFundingSource,
    BybitFundingSource.name: BybitFundingSource,
    BinanceFundingSource.name: BinanceFundingSource,
}


def get_source(name: str) -> FundingSource:
    try:
        return _REGISTRY[name.strip().lower()]()
    except KeyError:
        raise FundingSourceError(f"Unknown funding source '{name}'") from None


def source_chain() -> list[FundingSource]:
    """Primary source followed by its fallbacks, per env config."""
    names = [os.getenv("FUNDING_SOURCE", DEFAULT_SOURCE)]
    names += [
        n
        for n in os.getenv("FUNDING_SOURCE_FALLBACKS", DEFAULT_FALLBACKS).split(",")
    ]
    chain: list[FundingSource] = []
    seen: set[str] = set()
    for raw in names:
        key = raw.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            chain.append(get_source(key))
        except FundingSourceError:
            logger.warning("Ignoring unknown funding source '%s' in config", key)
    if not chain:
        raise FundingSourceError("No usable funding source configured")
    return chain


def fetch_funding_history(
    product_id: str,
    *,
    limit: int = 90,
    sources: list[FundingSource] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Fetch a normalized funding series for a canonical product.

    Returns ``(series, source_name)``. Raises FundingSourceError when every
    source in the chain fails, so a total outage surfaces instead of looking
    like an empty-but-healthy series.
    """
    chain = sources if sources is not None else source_chain()
    errors: list[str] = []
    for index, source in enumerate(chain):
        try:
            series = source.fetch(product_id, limit=limit)
        except Exception as exc:
            errors.append(f"{source.name}: {exc}")
            logger.warning(
                "Funding source %s failed for %s: %s", source.name, product_id, exc
            )
            continue
        if not series:
            errors.append(f"{source.name}: empty series")
            logger.warning(
                "Funding source %s returned an empty series for %s",
                source.name,
                product_id,
            )
            continue
        if index > 0:
            logger.warning(
                "Funding for %s served by fallback source %s (primary %s failed)",
                product_id,
                source.name,
                chain[0].name,
            )
        return series, source.name
    raise FundingSourceError(
        f"All funding sources failed for {product_id}: {'; '.join(errors)}"
    )
