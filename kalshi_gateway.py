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
    # Prefer the fixed-point dollars field (migration 2026); legacy `balance`
    # is still integer cents when present.
    dollars = payload.get("balance_dollars")
    if dollars is not None and dollars != "":
        try:
            return float(dollars)
        except (TypeError, ValueError):
            pass
    return float(payload.get("balance") or 0) / 100.0


def get_market(ticker: str) -> dict[str, Any]:
    """One market: prices, status, and result once settled."""
    payload = _request("GET", f"/markets/{ticker}")
    market = payload.get("market") or {}
    if not market:
        raise KalshiError(f"market {ticker}: empty reply")
    return market


def _dollars_to_cents(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value) * 100.0, 4)
    except (TypeError, ValueError):
        return None


def ask_cents(market: dict[str, Any], side: str) -> int | None:
    """Current cost to BUY `side`, in whole cents, or None when unquoted.

    Kalshi's 2026 fixed-point migration dropped integer ``yes_ask`` /
    ``no_ask`` in favour of ``*_ask_dollars``. Prefer dollars; fall back to
    the opposite bid (NO ask ≈ 100 − YES bid) when the direct ask is empty;
    still accept legacy integer cents if an older payload shows up.
    """
    side_l = str(side).lower()
    if side_l not in ("yes", "no"):
        return None

    direct_dollars = market.get(f"{side_l}_ask_dollars")
    cents = _dollars_to_cents(direct_dollars)
    if cents is None:
        opposite = "no" if side_l == "yes" else "yes"
        opp_bid = _dollars_to_cents(market.get(f"{opposite}_bid_dollars"))
        if opp_bid is not None:
            cents = round(100.0 - opp_bid, 4)
    if cents is None:
        legacy = market.get(f"{side_l}_ask")
        try:
            cents = float(legacy) if legacy is not None else None
        except (TypeError, ValueError):
            cents = None
    if cents is None:
        return None
    whole = int(round(cents))
    return whole if 1 <= whole <= 99 else None


def _fp_count(contracts: int) -> str:
    return f"{max(0, int(contracts)):.2f}"


def _fp_dollars_from_cents(cents: int | float) -> str:
    return f"{max(0.0, float(cents)) / 100.0:.4f}"


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
    *, client_order_id: str | None = None, take_cents: int = 2,
) -> dict[str, Any]:
    """Marketable limit buy of `contracts` × `side` via Create Order V2.

    V2 quotes the YES book only: buy YES → ``side=bid`` at YES price; buy NO
    → ``side=ask`` at YES price (= 100¢ − NO). ``take_cents`` worsens the
    limit so an IOC can cross the spread. Legacy ``/portfolio/orders`` returns
    HTTP 410 (deprecated 2026).
    """
    if side not in ("yes", "no"):
        raise KalshiError(f"bad side {side!r}")
    take = max(0, int(take_cents))
    paid = min(99, max(1, int(price_cents) + take))
    if side == "yes":
        book_side = "bid"
        yes_cents = paid
    else:
        book_side = "ask"
        yes_cents = 100 - paid
    yes_cents = min(99, max(1, yes_cents))
    body: dict[str, Any] = {
        "ticker": ticker,
        "client_order_id": client_order_id or uuid.uuid4().hex,
        "side": book_side,
        "count": _fp_count(contracts),
        "price": _fp_dollars_from_cents(yes_cents),
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
    }
    payload = _request("POST", "/portfolio/events/orders", json_body=body)
    order = payload.get("order") or payload
    if not isinstance(order, dict) or not order.get("order_id"):
        raise KalshiError(f"order on {ticker}: no order_id in reply")
    return order


def remaining_contracts(order: dict[str, Any]) -> float:
    """Unfilled size on an order — prefers ``remaining_count_fp``."""
    fp = order.get("remaining_count_fp")
    if fp is not None and fp != "":
        try:
            return max(0.0, float(fp))
        except (TypeError, ValueError):
            pass
    # V2 also emits fill_count + initial_count; treat fully filled as done.
    try:
        fill = float(order.get("fill_count_fp") or order.get("fill_count") or 0)
        initial = float(
            order.get("initial_count_fp") or order.get("initial_count") or 0
        )
        if initial > 0 and fill + 1e-9 >= initial:
            return 0.0
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, float(order.get("remaining_count") or 0))
    except (TypeError, ValueError):
        return 0.0


def order_is_terminal(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or "").lower()
    if status in ("executed", "filled", "canceled", "cancelled"):
        return True
    return remaining_contracts(order) <= 0


def summary_from_order(order: dict[str, Any], side: str) -> dict[str, Any] | None:
    """Build a fill summary from a Create/Get Order payload (no fills GET).

    IOC V2 creates often return filled state in the create body while
    ``GET /portfolio/orders/{id}`` still 404s for a beat — booking must not
    wait on that read.
    """
    side_l = str(side).lower()
    try:
        contracts = float(
            order.get("fill_count_fp") or order.get("fill_count") or 0
        )
    except (TypeError, ValueError):
        contracts = 0.0
    if contracts <= 0:
        return None

    dollars_key = "yes_price_dollars" if side_l == "yes" else "no_price_dollars"
    avg = _dollars_to_cents(order.get(dollars_key))
    if avg is None:
        # V2 average_fill_price is YES dollars when present.
        yes_avg = _dollars_to_cents(order.get("average_fill_price"))
        if yes_avg is not None:
            avg = yes_avg if side_l == "yes" else round(100.0 - yes_avg, 4)
    if avg is None:
        return None

    fee = 0.0
    for key in ("taker_fees_dollars", "maker_fees_dollars", "fees_dollars"):
        raw = order.get(key)
        if raw is None or raw == "":
            continue
        try:
            fee += float(raw)
        except (TypeError, ValueError):
            pass

    fill_cost = None
    for key in ("taker_fill_cost_dollars", "maker_fill_cost_dollars", "fill_cost_dollars"):
        raw = order.get(key)
        if raw is None or raw == "":
            continue
        try:
            fill_cost = float(raw)
            break
        except (TypeError, ValueError):
            continue
    if fill_cost is None:
        fill_cost = contracts * (float(avg) / 100.0)

    return {
        "contracts": int(round(contracts)),
        "avg_cents": round(float(avg), 2),
        "cost_usd": round(fill_cost + fee, 2),
        "fee_usd": round(fee, 4),
        "order_id": str(order.get("order_id") or ""),
    }


def get_order(order_id: str) -> dict[str, Any]:
    payload = _request("GET", f"/portfolio/orders/{order_id}")
    return payload.get("order") or payload or {}


def get_order_retry(
    order_id: str, *, tries: int = 5, sleep_sec: float = 0.4,
) -> dict[str, Any]:
    """GET an order, tolerating brief 404s after a V2 create."""
    last: Exception | None = None
    for i in range(max(1, int(tries))):
        try:
            return get_order(order_id)
        except KalshiError as exc:
            last = exc
            if "404" not in str(exc) and "not_found" not in str(exc).lower():
                raise
            if i + 1 < tries:
                time.sleep(sleep_sec)
    assert last is not None
    raise last


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


def find_order_by_client_id(client_order_id: str) -> dict[str, Any] | None:
    """Scan recent orders for a client_order_id (recovery when GET-by-id 404s)."""
    if not client_order_id:
        return None
    payload = _request("GET", "/portfolio/orders", params={"limit": 50})
    for order in payload.get("orders") or []:
        if str(order.get("client_order_id") or "") == str(client_order_id):
            return order
    return None


def fill_summary(order_id: str, side: str) -> dict[str, Any]:
    """Aggregate fills for one order: contracts, average price, cost.

    Tries fills first (often readable before GET-by-id after a V2 create),
    then the order object. Venue fee fields win over the formula.

    Raises KalshiError when *neither* read answered: a zero summary is a
    claim ("this order filled nothing") that releases the caller's reserve,
    and an unreadable venue has not earned it — contracts may exist. Zero is
    only returned when at least one endpoint actually said so.
    """
    fills: list[dict[str, Any]] = []
    fills_readable = True
    try:
        fills = get_fills(order_id)
    except KalshiError:
        fills = []
        fills_readable = False

    if fills:
        contracts = 0.0
        notional_cents = 0.0
        fee_usd = 0.0
        fee_from_venue = False
        dollars_key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
        legacy_key = "yes_price" if side == "yes" else "no_price"
        for fill in fills:
            raw_count = fill.get("count_fp")
            if raw_count is None or raw_count == "":
                raw_count = fill.get("count") or 0
            try:
                count = float(raw_count)
            except (TypeError, ValueError):
                count = 0.0
            price = _dollars_to_cents(fill.get(dollars_key))
            if price is None:
                try:
                    price = float(fill.get(legacy_key) or 0)
                except (TypeError, ValueError):
                    price = 0.0
            contracts += count
            notional_cents += count * float(price)
            raw_fee = fill.get("fee_cost")
            if raw_fee is not None and raw_fee != "":
                try:
                    fee_usd += float(raw_fee)
                    fee_from_venue = True
                except (TypeError, ValueError):
                    pass
        if contracts > 0:
            avg = notional_cents / contracts
            if not fee_from_venue:
                fee_usd = taker_fee_usd(int(round(avg)), int(round(contracts)))
            return {
                "contracts": int(round(contracts)),
                "avg_cents": round(avg, 2),
                "cost_usd": round(notional_cents / 100.0 + fee_usd, 2),
                "fee_usd": round(fee_usd, 4),
            }

    order_readable = False
    try:
        order = get_order_retry(order_id, tries=3, sleep_sec=0.3)
        order_readable = True
        from_order = summary_from_order(order, side)
        if from_order is not None:
            return from_order
    except KalshiError:
        pass

    if not fills_readable and not order_readable:
        raise KalshiError(
            f"fill state unreadable for {order_id}: fills and order "
            "endpoints both failed"
        )
    return {"contracts": 0, "avg_cents": 0.0, "cost_usd": 0.0, "fee_usd": 0.0}


# Compat alias — callers historically used the plural; keep both so a typo
# cannot strand a filled order in 'placing' again.
fills_summary = fill_summary

