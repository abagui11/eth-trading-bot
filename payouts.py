"""Coinbase client for moving money OFF the venue. Separate on purpose.

This is deliberately not a method on `DerivGateway`. The trading gateway signs
with a key that can trade and cannot withdraw; this signs with a key that can
withdraw and cannot trade. Splitting them means neither credential alone can
both take a position and move the proceeds out, and an agent bug in the
trading path has no reachable code path to a withdrawal.

Sends go through the v2 API (`POST /api/v2/accounts/{id}/transactions` with
`type=send`), which is the only Coinbase surface that pays out to an external
address; Advanced Trade has no withdrawal endpoint.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

API_HOST = "api.coinbase.com"
_TIMEOUT = 30


class PayoutError(RuntimeError):
    """Refused or failed transfer. Never assume the money did not move."""

    def __init__(self, message: str, *, submitted: bool = False) -> None:
        super().__init__(message)
        # True when the request reached Coinbase and the outcome is unknown --
        # a timeout after send, say. The caller must reconcile rather than
        # retry, because a blind retry can pay twice.
        self.submitted = submitted


def signing_key(private_key: str) -> tuple[Any, str]:
    """(key, JWT algorithm), detected from the key's own format.

    The CDP portal defaults to Ed25519 and labels ECDSA "Legacy SDKs", so
    pinning one algorithm would either force the deprecated choice or break on
    the default. Both are accepted instead: a PEM signs ES256, and a CDP
    Ed25519 key arrives as base64 of 64 bytes — 32-byte seed followed by the
    public half — of which only the seed is the private key.
    """
    text = private_key.replace("\\n", "\n").strip()
    if "BEGIN" in text:
        return text, "ES256"

    import base64

    from cryptography.hazmat.primitives.asymmetric import ed25519

    try:
        raw = base64.b64decode(text, validate=True)
    except Exception as exc:
        raise PayoutError("transfer key is neither a PEM nor base64") from exc
    if len(raw) not in (32, 64):
        raise PayoutError(
            f"unrecognised Ed25519 key length ({len(raw)} bytes; expected 32 or 64)"
        )
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32]), "EdDSA"


def _build_jwt(method: str, path: str) -> str:
    """Short-lived CDP JWT signed with the TRANSFER key, uri-bound."""
    import jwt as pyjwt

    key_name = config.COINBASE_TRANSFER_KEY_NAME
    private_key = config.COINBASE_TRANSFER_PRIVATE_KEY
    if not key_name or not private_key:
        raise PayoutError(
            "COINBASE_TRANSFER_KEY_NAME / COINBASE_TRANSFER_PRIVATE_KEY unset — "
            "create a SECOND CDP key with View + Transfer (NO Trade), then run "
            "deploy/_install_transfer_key.sh"
        )
    key, algorithm = signing_key(private_key)
    now = int(time.time())
    return pyjwt.encode(
        {
            "sub": key_name,
            "iss": "cdp",
            "nbf": now,
            "exp": now + 120,
            "uri": f"{method} {API_HOST}{path}",
        },
        key,
        algorithm=algorithm,
        headers={"kid": key_name, "nonce": secrets.token_hex(16)},
    )


def _request(
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
            f"https://{API_HOST}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params=params,
            json=body,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        # A write that timed out may still have executed at the far end.
        raise PayoutError(
            f"{method} {path}: {exc}", submitted=(method == "POST")
        ) from exc

    if res.status_code >= 400:
        raise PayoutError(f"{method} {path}: HTTP {res.status_code} {res.text[:300]}")
    try:
        return res.json()
    except ValueError as exc:
        raise PayoutError(f"{method} {path}: non-JSON response") from exc


def key_permissions() -> dict[str, Any]:
    """What this key is allowed to do, straight from Coinbase."""
    return _request("GET", "/api/v3/brokerage/key_permissions")


def list_accounts() -> list[dict[str, Any]]:
    res = _request("GET", "/api/v2/accounts", params={"limit": 100})
    out = []
    for acct in res.get("data") or []:
        cur = acct.get("currency")
        code = cur.get("code") if isinstance(cur, dict) else cur
        bal = acct.get("balance") or {}
        out.append({
            "id": str(acct.get("id")),
            "currency": str(code or ""),
            "balance": float(bal.get("amount") or 0),
            "name": str(acct.get("name") or ""),
        })
    return out


def account_transactions(account_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Account-level history. 404s for a key without transfer rights."""
    res = _request(
        "GET", f"/api/v2/accounts/{account_id}/transactions",
        params={"limit": limit},
    )
    return list(res.get("data") or [])


def usdc_account() -> dict[str, Any]:
    for acct in list_accounts():
        if acct["currency"] == "USDC":
            return acct
    raise PayoutError("no USDC account visible to the transfer key")


# Fixed namespace for deriving Coinbase idem keys from our own references.
# Must never change: it is what makes the mapping from a withdrawal id to an
# idempotency key reproducible across restarts and redeploys.
_IDEM_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def idem_uuid(ref: str) -> str:
    """Turn our reference into the UUID Coinbase demands, deterministically.

    Coinbase rejects a non-UUID idem key outright (`Invalid input parameter.
    Param: Idem`). The obvious fix -- generate a fresh uuid4 per attempt --
    satisfies the format and silently destroys the thing the key is for: a
    retry would carry a different key and pay a second time. uuid5 hashes our
    own reference into a valid UUID, so one logical withdrawal always maps to
    one idem key, forever.
    """
    try:
        return str(uuid.UUID(ref))          # already a UUID, use it as-is
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_IDEM_NAMESPACE, ref))


def send(
    *,
    account_id: str,
    to_address: str,
    amount_usd: float,
    ref: str,
    network: str = "ethereum",
) -> dict[str, Any]:
    """Pay `amount_usd` USDC out to `to_address`. Returns Coinbase's record.

    **There is no venue-side idempotency.** Coinbase rejects `idem` on this
    endpoint outright (`Invalid input parameter. Param: Idem`) and only
    accepts the bare `type/to/amount/currency/network` body, as does
    `description`. That is not a detail -- it means a resend is a second
    payment with nothing at the far end to collapse it, so **this must never
    be retried automatically**. `ref` is ours, for the log and the ledger
    only; it is not sent.

    The caller's obligation, therefore: record the intent to pay *before*
    calling this, and on an ambiguous failure (`PayoutError.submitted`)
    reconcile against the balance rather than calling again.

    `amount_usd` is what the recipient gets. The account is debited more than
    that: the network fee is added on top, so the returned `debited_usd` is
    the real cost and the one the ledger has to charge. Measured at $0.148 on
    a $2.00 Ethereum send -- flat rather than proportional, so it is a large
    share of a small withdrawal and rounding error on a big one.

    The amount goes out as a string. Floats are not safe to hand a payments
    API, and here the rounding error is somebody's money.
    """
    if amount_usd <= 0:
        raise PayoutError(f"refusing to send {amount_usd}")

    body = {
        "type": "send",
        "to": to_address,
        "amount": f"{amount_usd:.2f}",
        "currency": "USDC",
        "network": network,
    }
    logger.info(
        "payout: sending $%.2f USDC to %s (ref %s)", amount_usd, to_address, ref
    )
    res = _request(
        "POST", f"/api/v2/accounts/{account_id}/transactions", body=body
    )
    data = res.get("data") or {}
    net = data.get("network") or {}
    debited = abs(float((data.get("amount") or {}).get("amount") or 0))
    return {
        "id": str(data.get("id") or ""),
        "status": str(data.get("status") or ""),
        "network_status": str(net.get("status") or ""),
        "txid": net.get("hash"),
        "sent_usd": float(amount_usd),
        "debited_usd": debited,
        "fee_usd": round(max(debited - float(amount_usd), 0.0), 6),
        "raw": data,
    }


def get_transaction(account_id: str, tx_id: str) -> dict[str, Any]:
    """Read one transfer back. **404s for this key type — do not rely on it.**

    Kept because the endpoint is the documented one and may open up, but every
    read of it currently fails, so a payout's progress cannot be followed this
    way. Confirmation has to come from the balance moving or from a chain
    lookup of the destination.
    """
    res = _request("GET", f"/api/v2/accounts/{account_id}/transactions/{tx_id}")
    data = res.get("data") or {}
    net = data.get("network") or {}
    return {
        "id": str(data.get("id") or ""),
        "status": str(data.get("status") or ""),
        "network_status": str(net.get("status") or ""),
        "txid": net.get("hash"),
        "raw": data,
    }


def usdc_balance() -> float:
    """Spot USDC, the pot payouts leave from.

    With transaction reads unavailable, a balance that moved by the expected
    amount is the evidence a send actually happened.
    """
    return usdc_account()["balance"]


def new_idem() -> str:
    """Only for callers with no durable id of their own (probes, manual sends)."""
    return uuid.uuid4().hex
