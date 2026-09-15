"""On-chain reads, for the two things Coinbase will not tell us.

Coinbase reports that a deposit arrived but not **who sent it**, and will not
let us read a payout's status back at all. Both gaps are answered by the same
place the money actually moved: the chain.

What this buys:

* **Sender attribution.** A tester's wallet is only proven when funds arrive
  from it. Without that, "we only pay you back to your own wallet" is a
  promise with nothing behind it, and a hijacked Telegram account could be
  paid out to an address its real owner never controlled.
* **Settlement confirmation.** A payout is confirmed by seeing it land at the
  destination, rather than inferred from our own balance dropping.

Reads only. Nothing here can move funds, and it holds no key that could.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

API_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = 1

# USDC on Ethereum mainnet. Pinned rather than configurable: a wrong token
# address here would verify deposits against a token nobody sent, and an
# attacker-issued token is trivially mintable. Validated against our own
# historical deposits in deploy/_verify_chain.py.
USDC_CONTRACT = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_DECIMALS = 6

_TIMEOUT = 20
_RETRIES = 3
_RETRY_SLEEP = 2.0

# Deep enough that a reorg undoing a credited deposit is not a practical
# concern on mainnet, shallow enough not to keep a tester waiting.
MIN_CONFIRMATIONS = 12


class ChainError(RuntimeError):
    """Lookup failed. Treated as "unknown", never as "did not happen"."""


def configured() -> bool:
    return bool(getattr(config, "ETHERSCAN_API_KEY", None))


def _request(params: dict[str, Any]) -> Any:
    if not configured():
        raise ChainError("ETHERSCAN_API_KEY unset — add it to .env")

    params = {**params, "chainid": CHAIN_ID, "apikey": config.ETHERSCAN_API_KEY}
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            res = requests.get(API_URL, params=params, timeout=_TIMEOUT)
            if res.status_code >= 400:
                raise ChainError(f"HTTP {res.status_code}: {res.text[:200]}")
            payload = res.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            time.sleep(_RETRY_SLEEP * (attempt + 1))
            continue

        status = str(payload.get("status", ""))
        message = str(payload.get("message", ""))
        result = payload.get("result")

        if status == "1":
            return result
        # "No transactions found" is an empty answer, not a failure. Treating
        # it as an error would make a quiet address look like an outage.
        if "no transactions found" in message.lower():
            return []
        # Rate limits are transient; everything else is not worth retrying.
        if "rate limit" in str(result).lower() or "rate limit" in message.lower():
            last = ChainError(f"rate limited: {message}")
            time.sleep(_RETRY_SLEEP * (attempt + 1))
            continue
        if params.get("module") == "proxy":
            return result          # proxy endpoints do not use status/message
        raise ChainError(f"{message}: {str(result)[:200]}")

    raise ChainError(f"failed after {_RETRIES} attempts: {last}")


def _to_usd(raw_value: str, decimals: int = USDC_DECIMALS) -> float:
    try:
        return int(raw_value) / (10 ** int(decimals))
    except (TypeError, ValueError):
        return 0.0


def _row(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "txid": str(entry.get("hash") or "").lower(),
        "from": str(entry.get("from") or "").lower(),
        "to": str(entry.get("to") or "").lower(),
        "amount_usd": _to_usd(
            entry.get("value"), entry.get("tokenDecimal") or USDC_DECIMALS
        ),
        "confirmations": int(entry.get("confirmations") or 0),
        "timestamp": int(entry.get("timeStamp") or 0),
        "block": int(entry.get("blockNumber") or 0),
        "symbol": str(entry.get("tokenSymbol") or ""),
    }


def usdc_transfers(address: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Recent USDC transfers touching `address`, newest first.

    Filtered to the USDC contract at the API, so a worthless token airdropped
    to the deposit address cannot be mistaken for a deposit -- the classic way
    a naive watcher credits somebody for nothing.
    """
    result = _request({
        "module": "account",
        "action": "tokentx",
        "contractaddress": USDC_CONTRACT,
        "address": address,
        "page": 1,
        "offset": limit,
        "sort": "desc",
    })
    if not isinstance(result, list):
        raise ChainError(f"unexpected tokentx payload: {str(result)[:200]}")
    return [_row(e) for e in result]


def inbound_usdc(address: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Transfers INTO `address`. Direction is decided by `to`, not by sign."""
    want = address.strip().lower()
    return [t for t in usdc_transfers(address, limit=limit) if t["to"] == want]


def find_transfer(txid: str, *, to_address: str | None = None) -> dict[str, Any] | None:
    """The USDC transfer inside one transaction, or None if there isn't one.

    Looks the hash up against the destination's transfer list rather than
    parsing receipt logs by hand: same authoritative source, far less ABI
    decoding to get subtly wrong.
    """
    clean = str(txid or "").strip().lower()
    if not clean.startswith("0x") or len(clean) != 66:
        return None
    if to_address:
        for transfer in usdc_transfers(to_address, limit=100):
            if transfer["txid"] == clean:
                return transfer
        return None

    receipt = _request({
        "module": "proxy", "action": "eth_getTransactionReceipt", "txhash": clean,
    })
    if not isinstance(receipt, dict):
        return None
    return _from_receipt(receipt, clean)


# Transfer(address,address,uint256)
_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


def _from_receipt(receipt: dict[str, Any], txid: str) -> dict[str, Any] | None:
    """Pull the USDC Transfer event out of a receipt.

    The event log, not the transaction's `from`: if a tester funds from a
    smart-contract wallet or a multisig, `tx.from` is a relayer and the real
    token sender only appears here. Verifying against `tx.from` would refuse
    honest users and could credit the wrong one.
    """
    for log in receipt.get("logs") or []:
        if str(log.get("address", "")).lower() != USDC_CONTRACT:
            continue
        topics = log.get("topics") or []
        if len(topics) < 3 or str(topics[0]).lower() != _TRANSFER_TOPIC:
            continue
        try:
            return {
                "txid": txid,
                "from": "0x" + str(topics[1])[-40:].lower(),
                "to": "0x" + str(topics[2])[-40:].lower(),
                "amount_usd": int(str(log.get("data") or "0x0"), 16) / 10 ** USDC_DECIMALS,
                "confirmations": 0,   # not reported here; caller re-checks
                "timestamp": 0,
                "block": int(str(receipt.get("blockNumber") or "0x0"), 16),
                "symbol": "USDC",
            }
        except (ValueError, TypeError, IndexError):
            continue
    return None


def verify_deposit(
    txid: str, *, to_address: str, min_confirmations: int = MIN_CONFIRMATIONS
) -> dict[str, Any]:
    """Who sent this deposit, how much, and is it deep enough to trust?

    Returns ``ok`` only when the transfer is found, landed at `to_address`,
    and is buried past `min_confirmations`. A lookup failure comes back as
    ``error`` rather than a negative: "we could not check" and "it did not
    happen" must never collapse into the same answer, because one of them
    would silently refuse an honest tester's proof.
    """
    try:
        transfer = find_transfer(txid, to_address=to_address)
    except ChainError as exc:
        return {"ok": False, "reason": "lookup_failed", "detail": str(exc)}

    if transfer is None:
        return {"ok": False, "reason": "not_found"}
    if transfer["to"] != to_address.strip().lower():
        return {"ok": False, "reason": "wrong_destination",
                "actual_to": transfer["to"]}
    if transfer["confirmations"] < min_confirmations:
        return {"ok": False, "reason": "unconfirmed",
                "confirmations": transfer["confirmations"],
                "needed": min_confirmations, "sender": transfer["from"],
                "amount_usd": transfer["amount_usd"]}

    return {"ok": True, "sender": transfer["from"],
            "amount_usd": transfer["amount_usd"], "txid": transfer["txid"],
            "confirmations": transfer["confirmations"]}


def confirm_payout(
    to_address: str,
    amount_usd: float,
    *,
    after_timestamp: int,
    tolerance_usd: float = 0.01,
) -> dict[str, Any]:
    """Did a payout of this size actually land at the destination?

    Coinbase will not return a payout's status, so this is the only
    independent evidence it arrived. Matched on destination, amount and a time
    floor, and the floor matters: without it an older transfer of the same
    size would read as this one settling.
    """
    try:
        transfers = inbound_usdc(to_address, limit=50)
    except ChainError as exc:
        return {"ok": False, "reason": "lookup_failed", "detail": str(exc)}

    for transfer in transfers:
        if transfer["timestamp"] < after_timestamp:
            continue
        if abs(transfer["amount_usd"] - amount_usd) <= tolerance_usd:
            return {"ok": True, "txid": transfer["txid"],
                    "amount_usd": transfer["amount_usd"],
                    "confirmations": transfer["confirmations"],
                    "timestamp": transfer["timestamp"]}
    return {"ok": False, "reason": "not_seen_yet"}
