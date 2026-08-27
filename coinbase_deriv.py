"""Coinbase US futures client — CFTC-regulated CDE contracts via Advanced Trade.

This account is US-based: international perps (INTX / the Deribit gateway)
return PERMISSION_DENIED, so live derivatives trade the CDE nano futures on
the Advanced Trade REST API (api.coinbase.com, same CDP key as spot).

Instrument resolution is dynamic: for each logical product (ETH-USD, BTC-USD)
we pick the FUTURE product with the furthest expiry — currently the
perpetual-style contracts ETP-20DEC30-CDE (0.1 ETH) / BIP-20DEC30-CDE
(0.01 BTC) — so there is no monthly roll.

Sizing: orders are placed in whole contracts. ``amount`` args are in
underlying units (ETH/BTC); the client floors to contracts and reports the
actual filled underlying quantity back to the caller.

Stops: Advanced Trade has no stop-market order type, so stops are placed as
stop-limit GTC with the limit set 1% through the trigger (effectively a
marketable stop).
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

API_HOST = "api.coinbase.com"
_BROKERAGE = "/api/v3/brokerage"

# Logical products live execution supports. Resolution to a concrete futures
# contract happens at call time (see DerivGateway.resolve_instrument).
_LOGICAL_PRODUCTS = ("ETH-USD", "BTC-USD")

_ROOT_BY_LOGICAL = {"ETH-USD": "ETH", "BTC-USD": "BTC"}

_PRODUCT_CACHE_TTL_SEC = 3600.0


class GatewayError(RuntimeError):
    """API error or transport failure from the futures API."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _build_jwt(method: str, path: str) -> str:
    """Short-lived CDP JWT for one REST request (ES256, uri-bound)."""
    import jwt as pyjwt  # lazy so paper-only deploys don't need it

    key_name = config.COINBASE_CDP_API_KEY_NAME
    private_key = config.COINBASE_CDP_PRIVATE_KEY
    if not key_name or not private_key:
        raise GatewayError(
            "COINBASE_CDP_API_KEY_NAME / COINBASE_CDP_PRIVATE_KEY unset — "
            "create a CDP key with View + Trade (NO Transfer) and add it to .env"
        )
    pem = private_key.replace("\\n", "\n")
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": key_name,
            "iss": "cdp",
            "nbf": now,
            "exp": now + 120,
            "uri": f"{method} {API_HOST}{path}",
        },
        pem,
        algorithm="ES256",
        headers={"kid": key_name, "nonce": secrets.token_hex(16)},
    )


def _quantize(value: float, increment: str, *, up: bool) -> str:
    """Round a price to the product's price increment (as string for the API)."""
    inc = Decimal(increment or "0.01")
    q = (Decimal(str(value)) / inc).to_integral_value(
        rounding=ROUND_UP if up else ROUND_DOWN
    ) * inc
    return format(q.normalize(), "f")


class DerivGateway:
    """Minimal Advanced Trade REST client for CDE futures."""

    def __init__(self, base_url: str | None = None) -> None:
        # base_url kept for interface/tests; production host is fixed.
        self.base_url = (base_url or f"https://{API_HOST}").rstrip("/")
        self._products: dict[str, dict[str, Any]] = {}   # product_id -> product
        self._resolved: dict[str, str] = {}              # logical -> product_id
        self._products_fetched_at: float = 0.0

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = _build_jwt(method, path)
        try:
            res = requests.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise GatewayError(f"{method} {path}: transport error {exc}") from exc
        if res.status_code >= 400:
            raise GatewayError(
                f"{method} {path}: HTTP {res.status_code} {res.text[:300]}",
                code=res.status_code,
            )
        try:
            return res.json()
        except ValueError as exc:
            raise GatewayError(f"{method} {path}: non-JSON response") from exc

    # -- product / instrument resolution -------------------------------------

    def _load_products(self) -> None:
        if (
            self._products
            and time.time() - self._products_fetched_at < _PRODUCT_CACHE_TTL_SEC
        ):
            return
        res = self._request(
            "GET", f"{_BROKERAGE}/products", params={"product_type": "FUTURE"}
        )
        products = {}
        for p in res.get("products") or []:
            if p.get("trading_disabled"):
                continue
            products[p["product_id"]] = p
        if not products:
            raise GatewayError("no tradable FUTURE products returned")
        self._products = products
        self._products_fetched_at = time.time()
        self._resolved.clear()

    def resolve_instrument(self, logical: str) -> str:
        """Logical product (ETH-USD) → concrete contract (ETP-20DEC30-CDE).

        Picks the furthest-expiry contract for the root asset, which lands on
        the perpetual-style 2030 contracts and avoids monthly rolls.
        """
        self._load_products()
        cached = self._resolved.get(logical)
        if cached:
            return cached
        root = _ROOT_BY_LOGICAL.get(logical)
        if not root:
            raise GatewayError(f"no futures mapping for {logical}")
        candidates = [
            p
            for p in self._products.values()
            if (p.get("future_product_details") or {}).get("contract_root_unit")
            == root
        ]
        if not candidates:
            raise GatewayError(f"no tradable {root} futures found")
        best = max(
            candidates,
            key=lambda p: (p.get("future_product_details") or {}).get(
                "contract_expiry"
            )
            or "",
        )
        self._resolved[logical] = best["product_id"]
        logger.info("Resolved %s -> %s", logical, best["product_id"])
        return best["product_id"]

    def _product(self, product_id: str) -> dict[str, Any]:
        self._load_products()
        p = self._products.get(product_id)
        if p is None:
            # Not in FUTURE cache (e.g. expired contract still held) — fetch.
            res = self._request("GET", f"{_BROKERAGE}/products/{product_id}")
            p = res if "product_id" in res else res.get("product") or {}
            if not p:
                raise GatewayError(f"unknown product {product_id}")
            self._products[product_id] = p
        return p

    def contract_size(self, product_id: str) -> float:
        fut = self._product(product_id).get("future_product_details") or {}
        size = float(fut.get("contract_size") or 0)
        if size <= 0:
            raise GatewayError(f"{product_id}: missing contract_size")
        return size

    def _to_contracts(self, product_id: str, amount_underlying: float) -> int:
        size = self.contract_size(product_id)
        contracts = int(amount_underlying / size + 1e-9)
        if contracts < 1:
            raise GatewayError(
                f"{product_id}: {amount_underlying} below one contract ({size})"
            )
        return contracts

    # -- interface used by execute.py ----------------------------------------

    def auth_check(self) -> bool:
        perms = self._request("GET", f"{_BROKERAGE}/key_permissions")
        if not perms.get("can_trade"):
            raise GatewayError(f"key cannot trade: {perms}")
        return True

    def get_instruments(self, currency: str = "USDC") -> list[dict[str, Any]]:
        self._load_products()
        return [
            {"instrument_name": pid, **(p.get("future_product_details") or {})}
            for pid, p in self._products.items()
        ]

    def get_instrument(self, instrument: str) -> dict[str, Any]:
        return self._product(instrument)

    def get_ticker(self, instrument: str) -> dict[str, Any]:
        res = self._request("GET", f"{_BROKERAGE}/products/{instrument}")
        p = res if "product_id" in res else res.get("product") or {}
        price = float(p.get("price") or 0) or float(p.get("mid_market_price") or 0)
        return {"mark_price": price, "product": p}

    def get_account_summary(self, currency: str = "USDC") -> dict[str, Any]:
        res = self._request("GET", f"{_BROKERAGE}/cfm/balance_summary")
        bs = res.get("balance_summary") or {}

        def _val(key: str) -> float:
            return float((bs.get(key) or {}).get("value") or 0)

        return {
            "equity": _val("total_usd_balance"),
            "available_funds": _val("futures_buying_power"),
            "margin_balance": _val("cfm_usd_balance"),
            "unrealized_pnl": _val("unrealized_pnl"),
            "raw": bs,
        }

    def get_position(self, instrument: str) -> dict[str, Any]:
        """Signed size in UNDERLYING units + mark, matching the old interface."""
        try:
            res = self._request(
                "GET", f"{_BROKERAGE}/cfm/positions/{instrument}"
            )
        except GatewayError as exc:
            if exc.code == 404:
                return {"size": 0.0, "mark_price": 0.0}
            raise
        pos = res.get("position") or {}
        contracts = abs(float(pos.get("number_of_contracts") or 0))
        if contracts == 0:
            return {"size": 0.0, "mark_price": float(pos.get("current_price") or 0)}
        sign = -1.0 if "SHORT" in str(pos.get("side") or "").upper() else 1.0
        return {
            "size": sign * contracts * self.contract_size(instrument),
            "mark_price": float(pos.get("current_price") or 0),
            "avg_entry_price": float(pos.get("avg_entry_price") or 0),
            "raw": pos,
        }

    def _create_order(
        self,
        *,
        product_id: str,
        side: str,  # 'buy' | 'sell'
        order_configuration: dict[str, Any],
    ) -> str:
        body = {
            "client_order_id": str(uuid.uuid4()),
            "product_id": product_id,
            "side": side.upper(),
            "order_configuration": order_configuration,
        }
        res = self._request("POST", f"{_BROKERAGE}/orders", body=body)
        if not res.get("success"):
            err = res.get("error_response") or res.get("response") or res
            raise GatewayError(f"order rejected: {err}")
        ok = res.get("success_response") or res.get("response") or {}
        order_id = str(ok.get("order_id") or "")
        if not order_id:
            raise GatewayError(f"order accepted but no order_id: {res}")
        return order_id

    def _fetch_order(self, order_id: str) -> dict[str, Any]:
        res = self._request(
            "GET", f"{_BROKERAGE}/orders/historical/{order_id}"
        )
        return res.get("order") or {}

    def place_market_order(
        self,
        *,
        instrument: str,
        side: str,  # 'buy' | 'sell'
        amount: float,  # underlying units (ETH/BTC)
        label: str,
        reduce_only: bool = False,  # kept for interface; netting handles it
    ) -> dict[str, Any]:
        contracts = self._to_contracts(instrument, amount)
        logger.info(
            "FUTURES ORDER %s %s %d contracts (%s underlying) [%s]",
            side, instrument, contracts, amount, label,
        )
        order_id = self._create_order(
            product_id=instrument,
            side=side,
            order_configuration={
                "market_market_ioc": {"base_size": str(contracts)}
            },
        )
        # IOC market order fills (or cancels) immediately; poll briefly for
        # the actual fill price/size.
        avg_price, filled_underlying = 0.0, 0.0
        csize = self.contract_size(instrument)
        for _ in range(5):
            o = self._fetch_order(order_id)
            avg_price = float(o.get("average_filled_price") or 0)
            filled_underlying = float(o.get("filled_size") or 0) * csize
            if o.get("status") in ("FILLED", "CANCELLED", "EXPIRED", "FAILED"):
                break
            time.sleep(1.0)
        if filled_underlying <= 0:
            raise GatewayError(
                f"{instrument}: market order {order_id} did not fill"
            )
        return {
            "order": {
                "order_id": order_id,
                "average_price": avg_price,
                "filled_qty": filled_underlying,
            }
        }

    def place_stop_market(
        self,
        *,
        instrument: str,
        side: str,  # closing side: 'sell' closes a long, 'buy' closes a short
        amount: float,  # underlying units
        trigger_price: float,
        label: str,
    ) -> dict[str, Any]:
        """Resting stop — stop-limit GTC with the limit 1% through the trigger."""
        contracts = self._to_contracts(instrument, amount)
        increment = (
            self._product(instrument).get("price_increment")
            or self._product(instrument).get("quote_increment")
            or "0.01"
        )
        selling = side.lower() == "sell"
        stop_px = _quantize(trigger_price, increment, up=not selling)
        limit_px = _quantize(
            trigger_price * (0.99 if selling else 1.01), increment, up=not selling
        )
        logger.info(
            "FUTURES STOP %s %s %d contracts trigger=%s limit=%s [%s]",
            side, instrument, contracts, stop_px, limit_px, label,
        )
        order_id = self._create_order(
            product_id=instrument,
            side=side,
            order_configuration={
                "stop_limit_stop_limit_gtc": {
                    "base_size": str(contracts),
                    "limit_price": limit_px,
                    "stop_price": stop_px,
                    "stop_direction": (
                        "STOP_DIRECTION_STOP_DOWN"
                        if selling
                        else "STOP_DIRECTION_STOP_UP"
                    ),
                }
            },
        )
        return {"order": {"order_id": order_id}}

    def cancel_orders(self, order_ids: list[str]) -> Any:
        """Cancel specific orders by id — immune to open-order listing lag."""
        if not order_ids:
            return []
        out = self._request(
            "POST",
            f"{_BROKERAGE}/orders/batch_cancel",
            body={"order_ids": order_ids},
        )
        return out.get("results") or out

    def cancel_all_by_instrument(self, instrument: str) -> Any:
        res = self._request(
            "GET",
            f"{_BROKERAGE}/orders/historical/batch",
            params={"order_status": "OPEN", "product_id": instrument},
        )
        ids = [o["order_id"] for o in res.get("orders") or []]
        if not ids:
            return []
        out = self._request(
            "POST", f"{_BROKERAGE}/orders/batch_cancel", body={"order_ids": ids}
        )
        return out.get("results") or out

    def close_position(self, instrument: str) -> dict[str, Any]:
        pos = self.get_position(instrument)
        size = float(pos.get("size") or 0)
        if size == 0:
            return {"order": None, "note": "already flat"}
        try:
            res = self._request(
                "POST",
                f"{_BROKERAGE}/orders/close_position",
                body={
                    "client_order_id": str(uuid.uuid4()),
                    "product_id": instrument,
                },
            )
            if res.get("success"):
                ok = res.get("success_response") or res.get("response") or {}
                return {"order": {"order_id": str(ok.get("order_id") or "")}}
            logger.warning("close_position endpoint refused: %s — falling back", res)
        except GatewayError as exc:
            logger.warning("close_position endpoint failed (%s) — falling back", exc)
        # Fallback: opposite market order for the full position.
        return self.place_market_order(
            instrument=instrument,
            side="sell" if size > 0 else "buy",
            amount=abs(size),
            label="flatten",
        )


class _InstrumentMap:
    """Lazy logical→contract mapping with the dict surface execute.py uses.

    Resolution needs the API, so failures return None (live path skips safely).
    """

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _LOGICAL_PRODUCTS:
            return default
        try:
            return get_gateway().resolve_instrument(key)
        except Exception:
            logger.exception("Instrument resolution failed for %s", key)
            return default

    def __getitem__(self, key: str) -> str:
        resolved = self.get(key)
        if resolved is None:
            raise KeyError(key)
        return resolved

    def __contains__(self, key: str) -> bool:
        return key in _LOGICAL_PRODUCTS

    def keys(self) -> list[str]:
        return list(_LOGICAL_PRODUCTS)

    def values(self) -> list[str]:
        return [v for k in _LOGICAL_PRODUCTS if (v := self.get(k)) is not None]

    def items(self) -> list[tuple[str, str]]:
        return [
            (k, v) for k in _LOGICAL_PRODUCTS if (v := self.get(k)) is not None
        ]


INSTRUMENT_MAP = _InstrumentMap()

_gateway: DerivGateway | None = None


def get_gateway() -> DerivGateway:
    global _gateway
    if _gateway is None:
        _gateway = DerivGateway()
    return _gateway
