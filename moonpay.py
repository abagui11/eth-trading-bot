"""MoonPay Commerce (hel.io) deposit client — USDC on Base per Telegram user.

Creates a depositCustomer for each user, stores their EVM deposit address, and
verifies deposit webhooks. Requires MOONPAY_* keys in config; when unset the
Fund button shows a friendly not-configured message.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any
from urllib.parse import urlencode

import requests

import config

logger = logging.getLogger(__name__)

_TIMEOUT = 30


def configured() -> bool:
    return bool(
        config.MOONPAY_PUBLIC_KEY
        and config.MOONPAY_SECRET_KEY
        and config.MOONPAY_DEPOSIT_ID
        and config.MOONPAY_RECIPIENT_PUBLIC_KEY
    )


def customer_id_for(telegram_id: int) -> str:
    return f"tg_{int(telegram_id)}"


def widget_url(*, customer_token: str | None = None, customer_id: str | None = None) -> str | None:
    tmpl = config.MOONPAY_WIDGET_URL_TEMPLATE
    if not tmpl or not config.MOONPAY_DEPOSIT_ID:
        return None
    return tmpl.format(
        deposit_id=config.MOONPAY_DEPOSIT_ID,
        customer_token=customer_token or "",
        customer_id=customer_id or "",
    )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.MOONPAY_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def _url(path: str, *, extra_params: dict[str, str] | None = None) -> str:
    params = {"apiKey": config.MOONPAY_PUBLIC_KEY or ""}
    if extra_params:
        params.update(extra_params)
    return f"{config.MOONPAY_API_BASE}{path}?{urlencode(params)}"


def _pick_evm_address(payload: dict[str, Any] | list[Any]) -> tuple[str | None, str | None]:
    """Return (evm_public_key, customer_token) from a create/retrieve response."""
    row: dict[str, Any]
    if isinstance(payload, list):
        row = payload[0] if payload else {}
    elif isinstance(payload, dict):
        row = payload
    else:
        return None, None
    token = str(row.get("token") or "") or None
    for wallet in row.get("depositWallets") or []:
        engine = (wallet.get("blockchainEngine") or {}).get("type") or ""
        if str(engine).upper() == "EVM":
            pk = str(wallet.get("publicKey") or "").strip()
            if pk:
                return pk, token
    # Fallback: recipientPublicKeys (legacy / alternate shape)
    keys = row.get("recipientPublicKeys") or []
    if keys:
        return str(keys[0]), token
    return None, token


def create_or_get_customer(telegram_id: int) -> dict[str, Any]:
    """Provision a deposit customer; return address + tokens.

    Shape: {ok, address, customer_id, customer_token, widget_url, reason?}
    """
    if not configured():
        return {"ok": False, "reason": "not_configured"}

    cid = customer_id_for(telegram_id)
    body = {
        "customerId": cid,
        "depositId": config.MOONPAY_DEPOSIT_ID,
        "recipientPublicKeys": [config.MOONPAY_RECIPIENT_PUBLIC_KEY],
        "blockchainEngineTypes": ["EVM"],
        "defaultOnrampAmount": int(max(20, float(
            __import__("bot_config").POOL_MIN_DEPOSIT_USD
        ))),
        "additionalJSON": json.dumps({"telegram_id": int(telegram_id)}),
    }
    try:
        resp = requests.post(
            _url("/v1/deposit-customers/api-key"),
            headers=_headers(),
            json=body,
            timeout=_TIMEOUT,
        )
        if resp.status_code in (409, 400):
            # Already exists — try retrieve by customer token/id via search.
            logger.info(
                "moonpay: create returned %s for %s — attempting retrieve",
                resp.status_code, cid,
            )
            retrieved = _retrieve_customer(cid)
            if retrieved.get("ok"):
                return retrieved
        resp.raise_for_status()
        address, token = _pick_evm_address(resp.json())
        if not address:
            logger.error("moonpay: create ok but no EVM address for %s: %s", cid, resp.text[:300])
            return {"ok": False, "reason": "no_address"}
        return {
            "ok": True,
            "address": address,
            "customer_id": cid,
            "customer_token": token,
            "widget_url": widget_url(customer_token=token, customer_id=cid),
            "raw": resp.json(),
        }
    except Exception as exc:
        logger.exception("moonpay: create_or_get_customer failed for %s", telegram_id)
        return {"ok": False, "reason": "api_error", "detail": str(exc)}


def _retrieve_customer(customer_id: str) -> dict[str, Any]:
    """Best-effort lookup of an existing deposit customer by merchant id."""
    deposit_id = config.MOONPAY_DEPOSIT_ID
    if not deposit_id:
        return {"ok": False, "reason": "not_configured"}
    # Helio retrieve-by-token needs the UUID token; without it we list deposit
    # transactions / customers is not always public. Try the customer endpoint
    # with customerId as a query when available.
    try:
        resp = requests.get(
            _url(f"/v1/deposit-customers/api-key/{deposit_id}/{customer_id}"),
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if resp.ok:
            address, token = _pick_evm_address(resp.json())
            if address:
                return {
                    "ok": True,
                    "address": address,
                    "customer_id": customer_id,
                    "customer_token": token,
                    "widget_url": widget_url(customer_token=token, customer_id=customer_id),
                }
    except Exception:
        logger.exception("moonpay: retrieve customer failed for %s", customer_id)
    return {"ok": False, "reason": "not_found"}


def verify_webhook_signature(body: bytes | str, signature: str | None) -> bool:
    secret = config.MOONPAY_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    if isinstance(body, str):
        body_bytes = body.encode("utf-8")
    else:
        body_bytes = body
    expected = hmac.new(
        secret.encode("utf-8"), body_bytes, hashlib.sha256
    ).hexdigest()
    try:
        return hmac.compare_digest(expected, signature.strip().lower()) or hmac.compare_digest(
            expected, signature.strip()
        )
    except Exception:
        return False


def parse_deposit_amount_usd(payload: dict[str, Any]) -> float:
    """Convert Helio smallest-unit amount to USD float (USDC = 6 decimals)."""
    currency = payload.get("currency") or {}
    decimals = int(currency.get("decimals") or 6)
    symbol = str(currency.get("symbol") or "").upper()
    raw = payload.get("amount")
    if raw is None:
        return 0.0
    try:
        amount = float(raw) / (10 ** decimals)
    except (TypeError, ValueError):
        return 0.0
    # Prefer USDC; if settlement is USD-denominated already, amount stands.
    if symbol in ("USDC", "USD", ""):
        return round(amount, 2)
    # Fall back to originalAmountInUSD when present (often micro-dollars / 1e6).
    usd_raw = payload.get("originalAmountInUSD")
    if usd_raw is not None:
        try:
            return round(float(usd_raw) / 1_000_000.0, 2)
        except (TypeError, ValueError):
            pass
    return round(amount, 2)


def list_recent_deposit_txs(*, limit: int = 50) -> list[dict[str, Any]]:
    """Poll Helio for recent deposit transactions (watchdog backup)."""
    if not configured():
        return []
    deposit_id = config.MOONPAY_DEPOSIT_ID
    try:
        resp = requests.get(
            _url(f"/v1/deposit/{deposit_id}/transactions"),
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            logger.warning("moonpay: list txs refused: %s", resp.text[:200])
            return []
        data = resp.json()
        if isinstance(data, list):
            return data[:limit]
        if isinstance(data, dict):
            return list(data.get("data") or data.get("transactions") or [])[:limit]
    except Exception:
        logger.exception("moonpay: list_recent_deposit_txs failed")
    return []
