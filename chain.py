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

Two sources, same answers. Etherscan v2 is the indexer of record where its
plan covers the chain; a plain JSON-RPC endpoint (``eth_getLogs`` on the USDC
``Transfer`` event, ``eth_call balanceOf``, ``eth_getTransactionReceipt``)
takes over where it does not — the free plan dropped Base in Sept 2026 and
the deposit watcher must not depend on a billing tier.
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

CHAIN_NAMES: dict[int, str] = {1: "Ethereum", 8453: "Base"}
# Approximate block time, for turning a time window into a block lookback.
_BLOCK_SECONDS: dict[int, float] = {1: 12.0, 8453: 2.0}


def chain_name(chain_id: int) -> str:
    return CHAIN_NAMES.get(int(chain_id), f"chain {chain_id}")

# USDC per chain. Pinned rather than configurable: a wrong token address here
# would verify deposits against a token nobody sent, and an attacker-issued
# token is trivially mintable. Mainnet validated against our own historical
# deposits in deploy/_verify_chain.py; Base is Circle's canonical native USDC.
USDC_CONTRACT = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"   # Ethereum mainnet
USDC_CONTRACTS: dict[int, str] = {
    1: USDC_CONTRACT,
    8453: "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",         # Base
}
USDC_DECIMALS = 6


def usdc_contract(chain_id: int) -> str:
    contract = USDC_CONTRACTS.get(int(chain_id))
    if not contract:
        raise ChainError(f"no pinned USDC contract for chain {chain_id}")
    return contract

_TIMEOUT = 20
_RETRIES = 3
_RETRY_SLEEP = 2.0

# Deep enough that a reorg undoing a credited deposit is not a practical
# concern on mainnet, shallow enough not to keep a tester waiting.
MIN_CONFIRMATIONS = 12


class ChainError(RuntimeError):
    """Lookup failed. Treated as "unknown", never as "did not happen"."""


def configured() -> bool:
    """Etherscan credentials present (the indexer path)."""
    return bool(getattr(config, "ETHERSCAN_API_KEY", None))


def rpc_url(chain_id: int) -> str | None:
    """JSON-RPC endpoint for a chain, or None when none is configured."""
    return {
        1: getattr(config, "ETH_RPC_URL", None),
        8453: getattr(config, "BASE_RPC_URL", None),
    }.get(int(chain_id))


# Chains Etherscan has refused with "not supported for this chain" — a plan
# limit, not an outage — and when to try it again. Without this the sweep
# would burn a doomed indexer call every minute before falling back.
_etherscan_unsupported: dict[int, float] = {}
_UNSUPPORTED_RETRY_SECONDS = 6 * 3600


def etherscan_available(chain_id: int = CHAIN_ID) -> bool:
    if not configured():
        return False
    until = _etherscan_unsupported.get(int(chain_id))
    return until is None or time.time() >= until


def readable(chain_id: int = CHAIN_ID) -> bool:
    """Can this chain be read at all, by either source?"""
    return etherscan_available(chain_id) or bool(rpc_url(chain_id))


def _request(params: dict[str, Any], *, chain_id: int = CHAIN_ID) -> Any:
    if not configured():
        raise ChainError("ETHERSCAN_API_KEY unset — add it to .env")

    params = {**params, "chainid": int(chain_id), "apikey": config.ETHERSCAN_API_KEY}
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
        if "not supported for this chain" in str(result).lower():
            _etherscan_unsupported[int(chain_id)] = (
                time.time() + _UNSUPPORTED_RETRY_SECONDS
            )
            logger.warning(
                "etherscan does not serve %s on this plan — using RPC for %dh",
                chain_name(chain_id), _UNSUPPORTED_RETRY_SECONDS // 3600,
            )
        raise ChainError(f"{message}: {str(result)[:200]}")

    raise ChainError(f"failed after {_RETRIES} attempts: {last}")


# ---------------------------------------------------------------------------
# JSON-RPC source
# ---------------------------------------------------------------------------

def _rpc(method: str, params: list[Any], *, chain_id: int) -> Any:
    url = rpc_url(chain_id)
    if not url:
        raise ChainError(f"no RPC endpoint configured for {chain_name(chain_id)}")
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            res = requests.post(url, json=body, timeout=_TIMEOUT)
            if res.status_code >= 400:
                raise ChainError(f"HTTP {res.status_code}: {res.text[:200]}")
            payload = res.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            time.sleep(_RETRY_SLEEP * (attempt + 1))
            continue
        if isinstance(payload, dict) and payload.get("error"):
            # A range/limit refusal is deterministic; retrying cannot help.
            raise ChainError(f"rpc {method}: {str(payload['error'])[:200]}")
        return payload.get("result") if isinstance(payload, dict) else None
    raise ChainError(f"rpc {method} failed after {_RETRIES} attempts: {last}")


def _hex_int(value: Any) -> int:
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return 0


def _pad_address(address: str) -> str:
    return "0x" + "0" * 24 + address.strip().lower()[2:]


def latest_block(chain_id: int) -> int:
    return _hex_int(_rpc("eth_blockNumber", [], chain_id=chain_id))


def blocks_for_seconds(seconds: float, chain_id: int) -> int:
    return max(1, int(seconds / _BLOCK_SECONDS.get(int(chain_id), 12.0)))


def _row_from_log(log: dict[str, Any], *, latest: int) -> dict[str, Any] | None:
    topics = log.get("topics") or []
    if len(topics) < 3 or str(topics[0]).lower() != _TRANSFER_TOPIC:
        return None
    block = _hex_int(log.get("blockNumber"))
    return {
        "txid": str(log.get("transactionHash") or "").lower(),
        "from": "0x" + str(topics[1])[-40:].lower(),
        "to": "0x" + str(topics[2])[-40:].lower(),
        "amount_usd": _hex_int(log.get("data") or "0x0") / 10 ** USDC_DECIMALS,
        "confirmations": max(0, latest - block + 1) if block else 0,
        "timestamp": 0,   # not in the log; nothing downstream of the sweep needs it
        "block": block,
        "symbol": "USDC",
    }


def scan_inbound_usdc(
    address: str,
    *,
    chain_id: int,
    from_block: int | None = None,
    lookback_seconds: float = 6 * 3600,
) -> tuple[list[dict[str, Any]], int]:
    """USDC ``Transfer`` events INTO `address` from `from_block` to the tip.

    Returns ``(rows, latest_block)`` so the caller can persist a cursor. With
    no cursor the scan starts `lookback_seconds` ago. Chunked to the public
    endpoint's ``eth_getLogs`` range cap. Filtered at the node on the USDC
    contract and the ``to`` topic — the same airdrop-proofing as the
    Etherscan path.
    """
    latest = latest_block(chain_id)
    if from_block is None:
        from_block = max(0, latest - blocks_for_seconds(lookback_seconds, chain_id))
    from_block = max(0, min(int(from_block), latest))
    step = max(1, int(getattr(config, "RPC_LOG_RANGE", 500)))
    contract = usdc_contract(chain_id)
    padded = _pad_address(address)

    rows: list[dict[str, Any]] = []
    start = from_block
    while start <= latest:
        end = min(start + step - 1, latest)
        logs = _rpc("eth_getLogs", [{
            "fromBlock": hex(start), "toBlock": hex(end),
            "address": contract, "topics": [_TRANSFER_TOPIC, None, padded],
        }], chain_id=chain_id)
        for log in logs or []:
            row = _row_from_log(log, latest=latest)
            if row and row["txid"] and row["to"] == address.strip().lower():
                rows.append(row)
        start = end + 1
    rows.sort(key=lambda r: r["block"], reverse=True)
    return rows, latest


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


def usdc_transfers(
    address: str, *, limit: int = 100, chain_id: int = CHAIN_ID
) -> list[dict[str, Any]]:
    """Recent USDC transfers touching `address`, newest first.

    Filtered to the USDC contract at the API, so a worthless token airdropped
    to the deposit address cannot be mistaken for a deposit -- the classic way
    a naive watcher credits somebody for nothing.
    """
    result = _request({
        "module": "account",
        "action": "tokentx",
        "contractaddress": usdc_contract(chain_id),
        "address": address,
        "page": 1,
        "offset": limit,
        "sort": "desc",
    }, chain_id=chain_id)
    if not isinstance(result, list):
        raise ChainError(f"unexpected tokentx payload: {str(result)[:200]}")
    return [_row(e) for e in result]


def inbound_usdc(
    address: str, *, limit: int = 100, chain_id: int = CHAIN_ID
) -> list[dict[str, Any]]:
    """Transfers INTO `address`. Direction is decided by `to`, not by sign."""
    want = address.strip().lower()
    return [
        t for t in usdc_transfers(address, limit=limit, chain_id=chain_id)
        if t["to"] == want
    ]


def recent_inbound_usdc(
    address: str,
    *,
    chain_id: int,
    cursor_block: int | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int | None]:
    """Inbound transfers for a deposit watcher, from whichever source serves
    this chain. Returns ``(rows, cursor)``: the cursor is the block to resume
    an RPC scan from next time (None when Etherscan answered, which pages by
    count and needs none). Rows carry the same shape from either source.
    """
    if etherscan_available(chain_id):
        try:
            return inbound_usdc(address, limit=limit, chain_id=chain_id), None
        except ChainError as exc:
            if not rpc_url(chain_id):
                raise
            logger.warning("etherscan read failed for %s — trying RPC (%s)",
                            chain_name(chain_id), str(exc)[:120])
    if not rpc_url(chain_id):
        raise ChainError(f"no readable source for {chain_name(chain_id)}")
    rows, latest = scan_inbound_usdc(address, chain_id=chain_id,
                                     from_block=cursor_block)
    # Resume a little behind the tip so a short reorg cannot hide a transfer;
    # the watcher is idempotent by txid, so re-seeing rows is harmless.
    return rows, max(0, latest - 2 * MIN_CONFIRMATIONS)


def _usdc_balance_rpc(address: str, *, chain_id: int) -> float:
    data = "0x70a08231" + _pad_address(address)[2:]      # balanceOf(address)
    result = _rpc("eth_call", [{"to": usdc_contract(chain_id), "data": data},
                               "latest"], chain_id=chain_id)
    if result in (None, "", "0x"):
        raise ChainError(f"unexpected balanceOf payload: {result!r}")
    return _hex_int(result) / (10 ** USDC_DECIMALS)


def usdc_balance(address: str, *, chain_id: int = CHAIN_ID) -> float:
    """Current USDC balance of `address`, in dollars.

    Read for the treasury view and the reconcile total. A lookup failure
    raises rather than returning 0: "the wallet is empty" and "we could not
    read the wallet" must never collapse into the same answer, because the
    reconciler would treat the second as missing client money.
    """
    if not etherscan_available(chain_id):
        return _usdc_balance_rpc(address, chain_id=chain_id)
    try:
        result = _request({
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": usdc_contract(chain_id),
            "address": address,
            "tag": "latest",
        }, chain_id=chain_id)
    except ChainError:
        if not rpc_url(chain_id):
            raise
        return _usdc_balance_rpc(address, chain_id=chain_id)
    try:
        return int(str(result)) / (10 ** USDC_DECIMALS)
    except (TypeError, ValueError) as exc:
        raise ChainError(f"unexpected tokenbalance payload: {str(result)[:200]}") from exc


def find_transfer(
    txid: str, *, to_address: str | None = None, chain_id: int = CHAIN_ID
) -> dict[str, Any] | None:
    """The USDC transfer inside one transaction, or None if there isn't one.

    Looks the hash up against the destination's transfer list rather than
    parsing receipt logs by hand: same authoritative source, far less ABI
    decoding to get subtly wrong.
    """
    clean = str(txid or "").strip().lower()
    if not clean.startswith("0x") or len(clean) != 66:
        return None

    if not etherscan_available(chain_id):
        return _find_transfer_rpc(clean, to_address=to_address, chain_id=chain_id)

    if to_address:
        for transfer in usdc_transfers(to_address, limit=100, chain_id=chain_id):
            if transfer["txid"] == clean:
                return transfer
        return None

    receipt = _request({
        "module": "proxy", "action": "eth_getTransactionReceipt", "txhash": clean,
    }, chain_id=chain_id)
    if not isinstance(receipt, dict):
        return None
    return _from_receipt(receipt, clean, chain_id=chain_id)


def _find_transfer_rpc(
    txid: str, *, to_address: str | None, chain_id: int
) -> dict[str, Any] | None:
    """Receipt-based lookup over JSON-RPC, with confirmations filled in."""
    receipt = _rpc("eth_getTransactionReceipt", [txid], chain_id=chain_id)
    if not isinstance(receipt, dict):
        return None
    transfer = _from_receipt(receipt, txid, chain_id=chain_id)
    if transfer is None:
        return None
    if to_address and transfer["to"] != to_address.strip().lower():
        return None
    if transfer["block"]:
        transfer["confirmations"] = max(
            0, latest_block(chain_id) - transfer["block"] + 1
        )
    return transfer


# Transfer(address,address,uint256)
_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


def _from_receipt(
    receipt: dict[str, Any], txid: str, *, chain_id: int = CHAIN_ID
) -> dict[str, Any] | None:
    """Pull the USDC Transfer event out of a receipt.

    The event log, not the transaction's `from`: if a tester funds from a
    smart-contract wallet or a multisig, `tx.from` is a relayer and the real
    token sender only appears here. Verifying against `tx.from` would refuse
    honest users and could credit the wrong one.
    """
    for log in receipt.get("logs") or []:
        if str(log.get("address", "")).lower() != usdc_contract(chain_id):
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
    txid: str,
    *,
    to_address: str,
    min_confirmations: int = MIN_CONFIRMATIONS,
    chain_id: int = CHAIN_ID,
) -> dict[str, Any]:
    """Who sent this deposit, how much, and is it deep enough to trust?

    Returns ``ok`` only when the transfer is found, landed at `to_address`,
    and is buried past `min_confirmations`. A lookup failure comes back as
    ``error`` rather than a negative: "we could not check" and "it did not
    happen" must never collapse into the same answer, because one of them
    would silently refuse an honest tester's proof.
    """
    try:
        transfer = find_transfer(txid, to_address=to_address, chain_id=chain_id)
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
