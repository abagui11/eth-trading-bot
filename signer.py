"""Hot-wallet signer for the test (intake) wallet — the one place the bot
can move money on-chain, built to be hard to point anywhere wrong.

What it will do: sign and broadcast a USDC `transfer` from
`TEST_WALLET_ADDRESS` to exactly two kinds of destination:

- `send_usdc(venue, amount)` — the Coinbase or Kalshi deposit address,
  on the chain each is configured for (deploy routing).
- `send_usdc_payout(telegram_id, amount, chain_id)` — that user's
  chain-verified payout address, re-read from `pool.payout_target` here,
  not accepted from the caller (withdrawals).

There is no generic "send to address" entry point in this module on
purpose; every `to` is derived from config or the ledger.

What it refuses:

- To run at all unless the private key derives to `TEST_WALLET_ADDRESS`
  (a pasted key for the wrong wallet is silent otherwise).
- Any destination outside the two rules above, any chain without a
  pinned USDC contract and an RPC endpoint, any amount the wallet's USDC
  or native gas balance cannot cover.

Caps and journal claims live with the callers (`treasury.execute_transfer`
for routing, `pool_withdrawals` state for payouts); this module only knows
how to sign one leg.
"""

from __future__ import annotations

import logging
from typing import Any

import chain
import config

logger = logging.getLogger(__name__)

# ERC-20 transfer(address,uint256)
_TRANSFER_SELECTOR = "0xa9059cbb"
# Gas for an ERC-20 transfer is ~45–65k; a hard ceiling bounds a bad estimate.
_GAS_CEILING = 120_000
_GAS_MARGIN = 1.25
# Floor priority fee when the node will not quote one (wei).
_PRIORITY_FLOOR_WEI = {1: 1_000_000_000, 8453: 1_000_000}

EXPLORERS: dict[int, str] = {
    1: "https://etherscan.io/tx/",
    8453: "https://basescan.org/tx/",
}


class SignerError(RuntimeError):
    """The leg could not be sent. Message is operator-facing.

    `submitted` is True only when the signed tx *may* have reached the
    network (the node stopped answering mid-broadcast). Callers that cannot
    tolerate a double-send must treat that as unknown, not as failed.
    """

    def __init__(self, message: str, *, submitted: bool = False) -> None:
        super().__init__(message)
        self.submitted = submitted


def _account():
    key = getattr(config, "TEST_WALLET_PRIVATE_KEY", None)
    if not key:
        return None
    try:
        from eth_account import Account
    except ImportError:
        logger.error("signer: eth-account is not installed; signer disabled")
        return None
    try:
        return Account.from_key(key)
    except (ValueError, TypeError) as exc:
        logger.error("signer: TEST_WALLET_PRIVATE_KEY is not a valid key: %s", exc)
        return None


def status() -> dict[str, Any]:
    """Why the signer is or is not usable, for /treasury and the Send card."""
    if not getattr(config, "TEST_WALLET_PRIVATE_KEY", None):
        return {"enabled": False, "reason": "no_key"}
    if not config.TEST_WALLET_ADDRESS:
        return {"enabled": False, "reason": "no_wallet_address"}
    acct = _account()
    if acct is None:
        return {"enabled": False, "reason": "bad_key"}
    if acct.address.lower() != str(config.TEST_WALLET_ADDRESS).strip().lower():
        return {
            "enabled": False,
            "reason": "key_address_mismatch",
            "key_address": acct.address,
        }
    return {"enabled": True, "address": acct.address}


def enabled() -> bool:
    return bool(status().get("enabled"))


def destination(to_loc: str) -> dict[str, Any] | None:
    """Allowlisted (address, chain) for a venue, or None if not configured.

    The allowlist *is* the config: the two venue deposit addresses and the
    chain each lives on. A venue without both set cannot be a signer target.
    """
    if to_loc == "coinbase":
        address = config.POOL_DEPOSIT_ADDRESS
        chain_id = getattr(config, "POOL_DEPOSIT_CHAIN_ID", None)
    elif to_loc == "kalshi":
        address = getattr(config, "KALSHI_DEPOSIT_ADDRESS", None)
        chain_id = getattr(config, "KALSHI_DEPOSIT_CHAIN_ID", None)
    else:
        return None
    if not address or chain_id is None:
        return None
    address = str(address).strip()
    if not (address.startswith("0x") and len(address) == 42):
        return None
    try:
        int(address, 16)
    except ValueError:
        return None
    chain_id = int(chain_id)
    if chain_id not in chain.USDC_CONTRACTS or not chain.rpc_url(chain_id):
        return None
    return {"address": address, "chain_id": chain_id, "venue": to_loc}


def can_send(to_loc: str) -> bool:
    return enabled() and destination(to_loc) is not None


def explorer_url(txid: str, chain_id: int) -> str:
    base = EXPLORERS.get(int(chain_id))
    return f"{base}{txid}" if base else txid


# ---------------------------------------------------------------------------
# One leg
# ---------------------------------------------------------------------------

def _wei(value: Any) -> int:
    return chain._hex_int(value)


def _fees(chain_id: int) -> tuple[int, int]:
    """(maxFeePerGas, maxPriorityFeePerGas) in wei, EIP-1559."""
    block = chain._rpc("eth_getBlockByNumber", ["latest", False], chain_id=chain_id)
    base_fee = _wei((block or {}).get("baseFeePerGas")) if isinstance(block, dict) else 0
    if base_fee <= 0:
        raise SignerError(f"could not read base fee on {chain.chain_name(chain_id)}")
    try:
        priority = _wei(chain._rpc("eth_maxPriorityFeePerGas", [], chain_id=chain_id))
    except chain.ChainError:
        priority = 0
    priority = max(priority, _PRIORITY_FLOOR_WEI.get(chain_id, 1_000_000))
    max_fee = base_fee * 2 + priority
    return max_fee, priority


def _preflight(acct_address: str, to_address: str, amount_usd: float, chain_id: int) -> dict[str, Any]:
    usdc = chain._usdc_balance_rpc(acct_address, chain_id=chain_id)
    if usdc + 1e-9 < amount_usd:
        raise SignerError(
            f"test wallet holds ${usdc:,.2f} USDC on {chain.chain_name(chain_id)}, "
            f"leg needs ${amount_usd:,.2f}"
        )
    native = _wei(chain._rpc("eth_getBalance", [acct_address, "latest"], chain_id=chain_id))
    return {"usdc": usdc, "native_wei": native}


def send_usdc(to_loc: str, amount_usd: float) -> dict[str, Any]:
    """Sign and broadcast one venue leg (deploy routing). Returns txid + chain.

    Raises SignerError with an operator-readable reason on any refusal;
    never partially succeeds — the only side effect is the broadcast at the
    very end, after every check has passed.
    """
    dest = destination(to_loc)
    if dest is None:
        raise SignerError(f"{to_loc} has no allowlisted deposit address/chain")
    return _send(
        str(dest["address"]), amount_usd, chain_id=int(dest["chain_id"]),
        purpose=to_loc,
    )


def send_usdc_payout(
    telegram_id: int, amount_usd: float, *, chain_id: int
) -> dict[str, Any]:
    """Pay a withdrawal from the test wallet to the user's verified address.

    The address is read from `pool.payout_target` *here* — the same gate the
    Coinbase payout path uses (registered, chain-proven as a sender of a
    credited deposit, not in cooldown). A caller cannot pass an address in.
    """
    import pool

    target = pool.payout_target(int(telegram_id))
    if not target.get("ok"):
        raise SignerError(
            f"user {telegram_id} has no payable address ({target.get('reason')})"
        )
    if int(chain_id) not in chain.USDC_CONTRACTS or not chain.rpc_url(int(chain_id)):
        raise SignerError(f"chain {chain_id} is not a payout chain here")
    return _send(
        str(target["address"]), amount_usd, chain_id=int(chain_id),
        purpose=f"payout:{int(telegram_id)}",
    )


def _send(
    to_address: str, amount_usd: float, *, chain_id: int, purpose: str
) -> dict[str, Any]:
    """Shared body: preflight, build, sign, broadcast. Private on purpose —
    the two public entry points are the only ways to choose a `to`."""
    st = status()
    if not st.get("enabled"):
        raise SignerError(f"signer disabled: {st.get('reason')}")
    amount = round(float(amount_usd), 2)
    if amount <= 0:
        raise SignerError("amount must be positive")

    acct = _account()
    assert acct is not None
    contract = chain.usdc_contract(chain_id)
    units = int(round(amount * (10 ** chain.USDC_DECIMALS)))
    data = (
        _TRANSFER_SELECTOR
        + chain._pad_address(to_address)[2:]
        + hex(units)[2:].rjust(64, "0")
    )

    pre = _preflight(acct.address, to_address, amount, chain_id)
    try:
        estimated = _wei(chain._rpc(
            "eth_estimateGas",
            [{"from": acct.address, "to": contract, "data": data}],
            chain_id=chain_id,
        ))
    except chain.ChainError as exc:
        raise SignerError(f"gas estimate refused (would the transfer revert?): {exc}") from exc
    gas = min(_GAS_CEILING, int(estimated * _GAS_MARGIN) or _GAS_CEILING)
    max_fee, priority = _fees(chain_id)
    gas_cost = gas * max_fee
    if pre["native_wei"] < gas_cost:
        raise SignerError(
            f"not enough gas: wallet has {pre['native_wei'] / 1e18:.6f} ETH on "
            f"{chain.chain_name(chain_id)}, needs about {gas_cost / 1e18:.6f}"
        )
    nonce = _wei(chain._rpc(
        "eth_getTransactionCount", [acct.address, "pending"], chain_id=chain_id
    ))
    from eth_utils import to_checksum_address

    tx = {
        "type": 2,
        "chainId": chain_id,
        "nonce": nonce,
        "to": to_checksum_address(contract),
        "value": 0,
        "data": data,
        "gas": gas,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority,
    }
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    raw_hex = raw if isinstance(raw, str) else "0x" + bytes(raw).hex()
    try:
        txid = chain._rpc("eth_sendRawTransaction", [raw_hex], chain_id=chain_id)
    except chain.ChainError as exc:
        # A node *rejecting* the tx (nonce, funds, revert) is a clean failure.
        # The node *not answering* is not: the first attempt may have been
        # accepted and only the reply lost. Say which, so a payout caller can
        # halt instead of refunding money that may already have left.
        ambiguous = "failed after" in str(exc)
        raise SignerError(
            f"broadcast {'unconfirmed' if ambiguous else 'refused'}: {exc}",
            submitted=ambiguous,
        ) from exc
    if not isinstance(txid, str) or not txid.startswith("0x"):
        raise SignerError(f"node returned no tx hash: {txid!r}", submitted=True)
    logger.info(
        "signer: sent $%.2f USDC %s -> %s (%s) on %s tx %s",
        amount, acct.address, to_address, purpose, chain.chain_name(chain_id), txid,
    )
    return {
        "txid": txid.lower(),
        "chain_id": chain_id,
        "to_address": to_address,
        "amount_usd": amount,
        "explorer": explorer_url(txid.lower(), chain_id),
    }


# ---------------------------------------------------------------------------
# Gas top-up — USDC → ETH via Uniswap V3 SwapRouter02 when a send is short
# ---------------------------------------------------------------------------

# Canonical WETH / SwapRouter02. Pinned like USDC — a wrong router would
# approve the wrong spender.
WETH: dict[int, str] = {
    1: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    8453: "0x4200000000000000000000000000000000000006",
}
SWAP_ROUTER02: dict[int, str] = {
    1: "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
    8453: "0x2626664c2603336E57B271c5C0b26F421741e481",
}
# 0.05% then 0.3% USDC/WETH pools — try the tight one first.
_POOL_FEES = (500, 3000)
# Need a dust of ETH to pay for approve + swap + unwrap before the top-up
# lands. Below this, only a manual bootstrap works.
_BOOTSTRAP_MIN_WEI = 3 * 10**14  # 0.0003 ETH
_APPROVE_GAS = 60_000
_SWAP_GAS = 250_000
_UNWRAP_GAS = 60_000


def _selector(sig: str) -> str:
    from eth_utils import keccak

    return "0x" + keccak(text=sig)[:4].hex()


def _encode_call(sig: str, types: list[str], args: list[Any]) -> str:
    from eth_abi import encode

    return _selector(sig) + encode(types, args).hex()


def _sign_and_broadcast(
    *,
    to: str,
    data: str,
    chain_id: int,
    gas: int,
    value: int = 0,
    purpose: str,
) -> str:
    """Low-level EIP-1559 send from the test wallet. Returns txid."""
    st = status()
    if not st.get("enabled"):
        raise SignerError(f"signer disabled: {st.get('reason')}")
    acct = _account()
    assert acct is not None
    max_fee, priority = _fees(chain_id)
    gas_cost = gas * max_fee
    native = _wei(chain._rpc("eth_getBalance", [acct.address, "latest"], chain_id=chain_id))
    if native < gas_cost + value:
        raise SignerError(
            f"not enough gas for {purpose}: wallet has {native / 1e18:.6f} ETH on "
            f"{chain.chain_name(chain_id)}, needs about {(gas_cost + value) / 1e18:.6f}"
        )
    nonce = _wei(chain._rpc(
        "eth_getTransactionCount", [acct.address, "pending"], chain_id=chain_id
    ))
    from eth_utils import to_checksum_address

    tx = {
        "type": 2,
        "chainId": chain_id,
        "nonce": nonce,
        "to": to_checksum_address(to),
        "value": int(value),
        "data": data,
        "gas": int(gas),
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": priority,
    }
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    raw_hex = raw if isinstance(raw, str) else "0x" + bytes(raw).hex()
    try:
        txid = chain._rpc("eth_sendRawTransaction", [raw_hex], chain_id=chain_id)
    except chain.ChainError as exc:
        ambiguous = "failed after" in str(exc)
        raise SignerError(
            f"broadcast {'unconfirmed' if ambiguous else 'refused'} ({purpose}): {exc}",
            submitted=ambiguous,
        ) from exc
    if not isinstance(txid, str) or not txid.startswith("0x"):
        raise SignerError(f"node returned no tx hash for {purpose}: {txid!r}", submitted=True)
    logger.info(
        "signer: %s on %s tx %s", purpose, chain.chain_name(chain_id), txid,
    )
    return txid.lower()


def native_balance_wei(chain_id: int) -> int:
    acct = _account()
    if acct is None:
        return 0
    return _wei(chain._rpc("eth_getBalance", [acct.address, "latest"], chain_id=chain_id))


def ensure_gas(
    chain_id: int, *, reserve_usdc: float = 0.0
) -> dict[str, Any]:
    """Top the test wallet up to ``GAS_TOPUP_TARGET_ETH`` via USDC → ETH.

    ``reserve_usdc`` is USDC that must stay available for a pending venue
    send — the top-up never spends into that reserve. Returns a status
    dict; ``ok`` False only when a top-up was needed and could not run
    (bootstrap / no USDC / swap refused).
    """
    chain_id = int(chain_id)
    if not bool(getattr(config, "GAS_TOPUP_ENABLED", True)):
        return {"ok": True, "skipped": "disabled"}
    if chain_id not in WETH or chain_id not in SWAP_ROUTER02:
        return {"ok": True, "skipped": "unsupported_chain"}
    if not enabled():
        return {"ok": False, "reason": "signer_disabled"}

    target = float(getattr(config, "GAS_TOPUP_TARGET_ETH", 0.015) or 0.015)
    target_wei = int(target * 1e18)
    native = native_balance_wei(chain_id)
    if native >= target_wei:
        return {"ok": True, "skipped": "funded", "native_eth": native / 1e18}

    if native < _BOOTSTRAP_MIN_WEI:
        return {
            "ok": False,
            "reason": "bootstrap_needed",
            "detail": (
                f"test wallet has {native / 1e18:.6f} ETH on "
                f"{chain.chain_name(chain_id)} — send a little ETH once so "
                "USDC→ETH gas top-ups can pay for their own approve/swap"
            ),
            "native_eth": native / 1e18,
        }

    need_eth = (target_wei - native) / 1e18
    ceiling = float(getattr(config, "GAS_TOPUP_ETH_PRICE_CEILING_USD", 6000) or 6000)
    max_usd = float(getattr(config, "GAS_TOPUP_MAX_USD", 25) or 25)
    # Buy a bit more than the shortfall so a tippy fee spike still clears.
    usdc_needed = min(max_usd, max(5.0, need_eth * ceiling * 1.1))
    usdc_needed = round(usdc_needed, 2)

    acct = _account()
    assert acct is not None
    usdc_bal = chain._usdc_balance_rpc(acct.address, chain_id=chain_id)
    spendable = max(0.0, usdc_bal - float(reserve_usdc or 0.0))
    if spendable + 1e-9 < usdc_needed:
        return {
            "ok": False,
            "reason": "insufficient_usdc_for_gas",
            "detail": (
                f"need ~${usdc_needed:,.2f} USDC free for gas on "
                f"{chain.chain_name(chain_id)}, have ${spendable:,.2f} after "
                f"reserving ${float(reserve_usdc or 0):,.2f} for the venue send"
            ),
            "native_eth": native / 1e18,
        }

    from eth_utils import to_checksum_address

    router = to_checksum_address(SWAP_ROUTER02[chain_id])
    weth = to_checksum_address(WETH[chain_id])
    usdc = to_checksum_address(chain.usdc_contract(chain_id))
    units = int(round(usdc_needed * (10 ** chain.USDC_DECIMALS)))
    # Catastrophic floor: assume ETH never costs more than the ceiling.
    min_out = int((usdc_needed / ceiling) * 0.90 * 1e18)

    approve_data = _encode_call(
        "approve(address,uint256)",
        ["address", "uint256"],
        [router, units],
    )

    txids: list[str] = []
    try:
        txids.append(_sign_and_broadcast(
            to=usdc, data=approve_data, chain_id=chain_id, gas=_APPROVE_GAS,
            purpose=f"gas-approve-${usdc_needed:.2f}",
        ))
        _await_receipt(txids[-1], chain_id=chain_id)

        swap_ok = False
        last_swap_detail = ""
        for fee in _POOL_FEES:
            # SwapRouter02 ExactInputSingleParams — no deadline field.
            swap_data = _encode_call(
                "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))",
                ["(address,address,uint24,address,uint256,uint256,uint160)"],
                [(usdc, weth, fee, acct.address, units, min_out, 0)],
            )
            swap_txid = _sign_and_broadcast(
                to=router, data=swap_data, chain_id=chain_id, gas=_SWAP_GAS,
                purpose=f"gas-swap-${usdc_needed:.2f}-fee{fee}",
            )
            txids.append(swap_txid)
            swap_receipt = _await_receipt(swap_txid, chain_id=chain_id)
            if int(swap_receipt.get("status") or "0x0", 16) == 1:
                swap_ok = True
                break
            last_swap_detail = f"swap fee {fee} reverted (tx {swap_txid})"
            logger.warning("signer: %s — trying next pool", last_swap_detail)
        if not swap_ok:
            return {
                "ok": False,
                "reason": "gas_topup_failed",
                "detail": last_swap_detail or "USDC→WETH swap reverted",
                "txids": txids,
                "native_eth": native / 1e18,
            }
        # WETH balance → unwrap to native ETH.
        bal_data = "0x70a08231" + chain._pad_address(acct.address)[2:]
        weth_bal = 0
        for _ in range(8):
            weth_bal = _wei(chain._rpc(
                "eth_call", [{"to": weth, "data": bal_data}, "latest"],
                chain_id=chain_id,
            ))
            if weth_bal > 0:
                break
            import time as _time
            _time.sleep(1.5)
        if weth_bal > 0:
            unwrap_data = _encode_call(
                "withdraw(uint256)", ["uint256"], [weth_bal]
            )
            txids.append(_sign_and_broadcast(
                to=weth, data=unwrap_data, chain_id=chain_id, gas=_UNWRAP_GAS,
                purpose=f"gas-unwrap-{weth_bal / 1e18:.6f}",
            ))
            _await_receipt(txids[-1], chain_id=chain_id)
    except SignerError as exc:
        return {
            "ok": False,
            "reason": "gas_topup_failed",
            "detail": str(exc),
            "txids": txids,
            "native_eth": native / 1e18,
        }

    after = native_balance_wei(chain_id)
    if after <= native:
        return {
            "ok": False,
            "reason": "gas_topup_failed",
            "detail": (
                f"gas top-up txs landed but native ETH did not rise "
                f"({native / 1e18:.6f} → {after / 1e18:.6f}) on "
                f"{chain.chain_name(chain_id)}"
            ),
            "txids": txids,
            "native_eth": after / 1e18,
        }
    logger.info(
        "signer: gas top-up on %s spent $%.2f USDC → %.6f ETH (was %.6f)",
        chain.chain_name(chain_id), usdc_needed, after / 1e18, native / 1e18,
    )
    return {
        "ok": True,
        "topped_up": True,
        "usdc_spent": usdc_needed,
        "native_eth_before": native / 1e18,
        "native_eth_after": after / 1e18,
        "chain_id": chain_id,
        "txids": txids,
    }


def _await_receipt(txid: str, *, chain_id: int, timeout_s: float = 90.0) -> dict:
    """Poll until the tx has a receipt, or raise SignerError."""
    import time as _time

    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        try:
            receipt = chain._rpc(
                "eth_getTransactionReceipt", [txid], chain_id=chain_id
            )
        except chain.ChainError:
            receipt = None
        if isinstance(receipt, dict) and receipt.get("blockNumber"):
            return receipt
        _time.sleep(2.0)
    raise SignerError(f"no receipt for {txid} after {timeout_s:.0f}s")
