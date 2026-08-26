"""Coinbase derivatives client — Deribit-powered gateway (NOT the retired INTX API).

JSON-RPC 2.0 over HTTP at ``https://drb.coinbase.com/api/v2``. Auth: sign a
short-lived CDP JWT locally, exchange it via ``public/auth`` with
``grant_type=coinbase_cdp`` for a bearer access token (~15 min), and refresh
before expiry. Instruments are Deribit-style, e.g. ``ETH_USDC-PERPETUAL``.

Spot market data is unaffected and stays on api.coinbase.com.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://drb.coinbase.com/api/v2"

# Coinbase product -> gateway perpetual instrument.
INSTRUMENT_MAP: dict[str, str] = {
    "ETH-USD": "ETH_USDC-PERPETUAL",
    "BTC-USD": "BTC_USDC-PERPETUAL",
}


class GatewayError(RuntimeError):
    """JSON-RPC error or transport failure from the derivatives gateway."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _build_cdp_jwt() -> str:
    """Sign a short-lived CDP JWT from the configured API key.

    Supports both EC (ES256) and Ed25519 (EdDSA) CDP keys. Requires PyJWT +
    cryptography (see requirements.txt).
    """
    import jwt as pyjwt  # imported lazily so paper-only deploys don't need it

    key_name = config.COINBASE_CDP_API_KEY_NAME
    private_key = config.COINBASE_CDP_PRIVATE_KEY
    if not key_name or not private_key:
        raise GatewayError(
            "COINBASE_CDP_API_KEY_NAME / COINBASE_CDP_PRIVATE_KEY unset — "
            "create a CDP key with View + Trade (NO Transfer) and add it to .env"
        )

    now = int(time.time())
    payload = {
        "sub": key_name,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
    }
    headers = {"kid": key_name, "nonce": secrets.token_hex(16)}

    if "BEGIN" in private_key:  # PEM → EC key (ES256)
        pem = private_key.replace("\\n", "\n")
        return pyjwt.encode(payload, pem, algorithm="ES256", headers=headers)

    # Base64 raw Ed25519 seed (new-style CDP keys)
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    raw = base64.b64decode(private_key)
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    return pyjwt.encode(payload, key, algorithm="EdDSA", headers=headers)


class DerivGateway:
    """Minimal JSON-RPC client with cached bearer auth."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (
            base_url or config.COINBASE_DERIV_API_URL or DEFAULT_BASE_URL
        ).rstrip("/")
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._rpc_id = 0

    # -- transport ----------------------------------------------------------

    def _post(self, method: str, params: dict[str, Any], token: str | None) -> Any:
        self._rpc_id += 1
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        res = requests.post(
            f"{self.base_url}/{method}",
            json={
                "jsonrpc": "2.0",
                "id": self._rpc_id,
                "method": method,
                "params": params,
            },
            headers=headers,
            timeout=15,
        )
        try:
            body = res.json()
        except ValueError as exc:
            raise GatewayError(
                f"{method}: non-JSON response HTTP {res.status_code}"
            ) from exc
        if "error" in body and body["error"]:
            err = body["error"]
            raise GatewayError(
                f"{method}: {err.get('message', 'unknown')} ({err.get('code')})",
                code=err.get("code"),
            )
        return body.get("result")

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        result = self._post(
            "public/auth",
            {"grant_type": "coinbase_cdp", "token": _build_cdp_jwt()},
            token=None,
        )
        token = (result or {}).get("access_token")
        if not token:
            raise GatewayError("public/auth returned no access_token")
        self._token = token
        self._token_expiry = time.time() + float(
            (result or {}).get("expires_in") or 900
        )
        return token

    def call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        params = params or {}
        if method.startswith("private/"):
            return self._post(method, params, token=self._ensure_token())
        return self._post(method, params, token=None)

    # -- convenience --------------------------------------------------------

    def auth_check(self) -> bool:
        self._ensure_token()
        return True

    def get_instruments(self, currency: str = "USDC") -> list[dict[str, Any]]:
        return (
            self.call(
                "public/get_instruments",
                {"currency": currency, "kind": "future", "expired": False},
            )
            or []
        )

    def get_instrument(self, instrument: str) -> dict[str, Any]:
        return self.call("public/get_instrument", {"instrument_name": instrument})

    def get_ticker(self, instrument: str) -> dict[str, Any]:
        return self.call("public/ticker", {"instrument_name": instrument})

    def get_account_summary(self, currency: str = "USDC") -> dict[str, Any]:
        return self.call(
            "private/get_account_summary", {"currency": currency}
        )

    def get_position(self, instrument: str) -> dict[str, Any]:
        return self.call("private/get_position", {"instrument_name": instrument})

    def place_market_order(
        self,
        *,
        instrument: str,
        side: str,  # 'buy' | 'sell'
        amount: float,
        label: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        method = "private/buy" if side == "buy" else "private/sell"
        params: dict[str, Any] = {
            "instrument_name": instrument,
            "amount": amount,
            "type": "market",
            "label": label[:64],
        }
        if reduce_only:
            params["reduce_only"] = True
        return self.call(method, params)

    def place_stop_market(
        self,
        *,
        instrument: str,
        side: str,  # closing side: 'sell' closes a long, 'buy' closes a short
        amount: float,
        trigger_price: float,
        label: str,
    ) -> dict[str, Any]:
        """Reduce-only stop-market resting on the exchange."""
        method = "private/buy" if side == "buy" else "private/sell"
        return self.call(
            method,
            {
                "instrument_name": instrument,
                "amount": amount,
                "type": "stop_market",
                "trigger_price": trigger_price,
                "trigger": "mark_price",
                "reduce_only": True,
                "label": label[:64],
            },
        )

    def cancel_all_by_instrument(self, instrument: str) -> Any:
        return self.call(
            "private/cancel_all_by_instrument", {"instrument_name": instrument}
        )

    def close_position(self, instrument: str) -> dict[str, Any]:
        return self.call(
            "private/close_position",
            {"instrument_name": instrument, "type": "market"},
        )


_gateway: DerivGateway | None = None


def get_gateway() -> DerivGateway:
    global _gateway
    if _gateway is None:
        _gateway = DerivGateway()
    return _gateway
