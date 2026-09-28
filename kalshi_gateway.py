"""Kalshi Trade API v2 gateway — order placement for the Kalshi lanes.

The hub-side equivalent of `coinbase_deriv.DerivGateway`, deliberately thin:
sign requests, read balances/markets, place and cancel limit orders, read
fills. Strategy logic lives in `kalshi_execute`; accounting lives in `pool`.
Keeping venue-specific code behind this shape is what makes a later FalconX
migration a third gateway rather than a rewrite.

Auth: RSA-PSS(SHA256) over ``{timestamp_ms}{METHOD}{path}`` with the API key
id in a header — Kalshi's scheme, key material from the account settings page.
``KALSHI_API_KEY_ID`` / ``KALSHI_PRIVATE_KEY_PATH`` unset means the gateway is
not configured and the Kalshi lanes stay feed-only.
"""

from __future__ import annotations

import base64
import logging
import math
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

import config

logger = logging.getLogger(__name__)

_TIMEOUT = 20


class KalshiError(RuntimeError):
    """Request failed. Callers must treat this as "unknown", never as "no"."""


def configured() -> bool:
    return bool(config.KALSHI_API_KEY_ID and config.KALSHI_PRIVATE_KEY_PATH)


@lru_cache(maxsize=1)
def _private_key():
    from cryptography.hazmat.primitives import serialization

    raw = Path(str(config.KALSHI_PRIVATE_KEY_PATH)).read_bytes()
    return serialization.load_pem_private_key(raw, password=None)


def _sign(message: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signature = _private_key().sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def _request(
    method: str, endpoint: str, *, json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not configured():
        raise KalshiError("Kalshi gateway not configured (KALSHI_API_KEY_ID unset)")

    base = config.KALSHI_API_BASE
    # The signature covers the URL *path* (no query), including the
    # /trade-api/v2 prefix baked into the base URL.
    path = urlparse(base).path + endpoint
    ts_ms = str(int(time.time() * 1000))
    headers = {
        "KALSHI-ACCESS-KEY": str(config.KALSHI_API_KEY_ID),
        "KALSHI-ACCESS-SIGNATURE": _sign(f"{ts_ms}{method.upper()}{path}"),
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "Content-Type": "application/json",
    }
    try:
        res = requests.request(
            method.upper(), f"{base}{endpoint}", headers=headers,
            json=json_body, params=params, timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise KalshiError(f"{method} {endpoint}: {exc}") from exc
    if res.status_code >= 400:
        raise KalshiError(f"{method} {endpoint}: HTTP {res.status_code} {res.text[:300]}")
    try:
        return res.json() if res.text else {}
    except ValueError as exc:
        raise KalshiError(f"{method} {endpoint}: non-JSON reply") from exc


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_balance_usd() -> float:
    """Available cash on the Kalshi account, in dollars."""
    payload = _request("GET", "/portfolio/balance")
    return float(payload.get("balance") or 0) / 100.0


def get_market(ticker: str) -> dict[str, Any]:
    """One market: prices in cents, status, and result once settled."""
    payload = _request("GET", f"/markets/{ticker}")
    market = payload.get("market") or {}
    if not market:
        raise KalshiError(f"market {ticker}: empty reply")
    return market


def ask_cents(market: dict[str, Any], side: str) -> int | None:
    """Current cost to BUY `side`, in cents, or None when unquoted."""
    key = "yes_ask" if side == "yes" else "no_ask"
    value = market.get(key)
    try:
        cents = int(value)
    except (TypeError, ValueError):
        return None
    return cents if 1 <= cents <= 99 else None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def taker_fee_usd(price_cents: int, contracts: int) -> float:
    """Kalshi's published taker fee: ceil(0.07 × C × P × (1−P)) per order,
    rounded up to the cent. Used for sizing; the booked figure prefers the
    venue's own number from the fill when it reports one."""
    p = price_cents / 100.0
    raw = 0.07 * contracts * p * (1.0 - p)
    return math.ceil(raw * 100) / 100.0


def place_limit_buy(
    ticker: str, side: str, contracts: int, price_cents: int,
    *, client_order_id: str | None = None,
) -> dict[str, Any]:
    """Limit buy of `contracts` × `side` at up to `price_cents`.

    Returns the venue's order dict. `client_order_id` is the idempotency
    handle — resending with the same id cannot double-order.
    """
    if side not in ("yes", "no"):
        raise KalshiError(f"bad side {side!r}")
    body: dict[str, Any] = {
        "ticker": ticker,
        "client_order_id": client_order_id or uuid.uuid4().hex,
        "action": "buy",
        "side": side,
        "count": int(contracts),
        "type": "limit",
    }
    body["yes_price" if side == "yes" else "no_price"] = int(price_cents)
    payload = _request("POST", "/portfolio/orders", json_body=body)
    order = payload.get("order") or {}
    if not order.get("order_id"):
        raise KalshiError(f"order on {ticker}: no order_id in reply")
    return order


def get_order(order_id: str) -> dict[str, Any]:
    payload = _request("GET", f"/portfolio/orders/{order_id}")
    return payload.get("order") or {}


def cancel_order(order_id: str) -> None:
    """Cancel the resting remainder. A 404-ish failure is swallowed: the order
    having already finished is the outcome we wanted."""
    try:
        _request("DELETE", f"/portfolio/orders/{order_id}")
    except KalshiError as exc:
        logger.info("kalshi: cancel %s: %s", order_id, exc)


def get_fills(order_id: str) -> list[dict[str, Any]]:
    payload = _request("GET", "/portfolio/fills", params={"order_id": order_id})
    return list(payload.get("fills") or [])


def fill_summary(order_id: str, side: str) -> dict[str, Any]:
    """Aggregate fills for one order: contracts, average price, cost.

    Fees: computed from the published taker formula on the filled size —
    fills don't itemize fees, and the order object's fee fields vary by
    account age, so the formula is the stable answer. It rounds *up*, so the
    booked cost can only overstate by <1¢, never quietly understate.
    """
    fills = get_fills(order_id)
    contracts = 0
    notional_cents = 0
    price_key = "yes_price" if side == "yes" else "no_price"
    for fill in fills:
        count = int(fill.get("count") or 0)
        price = int(fill.get(price_key) or 0)
        contracts += count
        notional_cents += count * price
    if contracts <= 0:
        return {"contracts": 0, "avg_cents": 0.0, "cost_usd": 0.0, "fee_usd": 0.0}
    avg = notional_cents / contracts
    fee = taker_fee_usd(int(round(avg)), contracts)
    return {
        "contracts": contracts,
        "avg_cents": round(avg, 2),
        "cost_usd": round(notional_cents / 100.0 + fee, 2),
        "fee_usd": fee,
    }
