"""Why is nothing filling? Walks every live-execution gate in order.

Read-only: resolves instruments and reads balances, but never places, cancels,
or closes an order. Safe to run against a live sleeve at any time.

    cd /opt/eth-trading-agent
    sudo -u ethagent .venv/bin/python deploy/diagnose_live.py

The gates are checked in the same order execute._execute applies them, so the
FIRST failure reported is the one actually blocking fills. Note that the
EXECUTION_MODE=off gate returns before any logging, which is why an "off"
sleeve looks completely silent in journalctl rather than logging a skip.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot_config  # noqa: E402
import config  # noqa: E402
import live_ledger  # noqa: E402

MILL_ENV = Path(os.getenv("MILL_ENV", "/opt/trade-ideas/.env"))

BLOCKERS: list[str] = []
WARNINGS: list[str] = []


def block(msg: str) -> None:
    BLOCKERS.append(msg)
    print(f"   BLOCKED  {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"   WARN     {msg}")


def ok(msg: str) -> None:
    print(f"   ok       {msg}")


def head(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def env_value(path: Path, key: str) -> str | None:
    """Read one key from a .env without importing it (no secrets printed)."""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def check_switches() -> None:
    head("1. Master switches")
    mode = config.EXECUTION_MODE
    if mode == "off":
        block(
            "EXECUTION_MODE=off — execute._execute returns before it logs "
            "anything, so BOTH the HQ and mill sleeves are silently disabled. "
            "This is the only gate that produces no log line at all."
        )
    elif mode == "shadow":
        warn(
            "EXECUTION_MODE=shadow — orders are logged as SHADOW ORDER and "
            "nothing reaches Coinbase. No ledger rows are written either."
        )
    else:
        ok(f"EXECUTION_MODE={mode}")

    if MILL_ENV.exists():
        raw = (env_value(MILL_ENV, "MILL_LIVE_ENABLED") or "").lower()
        if raw in ("1", "true", "yes", "on"):
            ok(f"MILL_LIVE_ENABLED={raw} ({MILL_ENV})")
        else:
            block(
                f"MILL_LIVE_ENABLED={raw or 'unset'} in {MILL_ENV} — the mill "
                "never POSTs to /api/v1/execute/mill, so mill clips cannot "
                "fill. (Does not affect the HQ/Eva lane.)"
            )
        if not env_value(MILL_ENV, "SERVICE_TOKEN"):
            block(f"SERVICE_TOKEN unset in {MILL_ENV} — mill live bridge skips.")
    else:
        warn(f"{MILL_ENV} not found — cannot verify the mill's flags from here.")

    ok(f"WATCHDOG_LIVE_ENABLED={bot_config.watchdog_live_enabled()} (separate gate)")


def check_halt() -> None:
    head("2. Halt state (shared by BOTH sleeves)")
    try:
        reason = live_ledger.get_meta("live_halt")
        halt_date = live_ledger.get_meta("live_halt_date")
    except Exception as exc:
        warn(f"could not read live_meta: {exc}")
        return
    if not reason:
        ok("no halt set")
        return
    expired = (
        halt_date and halt_date < _today() and str(reason).startswith("daily_loss")
    )
    if expired:
        ok(f"stale daily halt from {halt_date} — auto-clears on next entry")
    else:
        block(
            f"live_halt={reason!r} (set {halt_date}). halt_live() writes ONE "
            "global key, so this blocks the HQ and mill sleeves together. "
            "Manual halts (stop_reject:*) never expire — clear them by hand."
        )


def check_daily_loss() -> None:
    head("3. Daily loss budget")
    for source, limit in (
        ("hq", bot_config.LIVE_DAILY_LOSS_LIMIT_USD),
        ("mill", bot_config.LIVE_MILL_DAILY_LOSS_LIMIT_USD),
    ):
        try:
            pnl = sum(
                float(t.get("pnl_usd") or 0.0)
                for t in live_ledger.get_closed_trades(limit=200, source=source)
                if (t.get("closed_at") or "").startswith(_today())
            )
        except Exception as exc:
            warn(f"{source}: could not read closed trades ({exc})")
            continue
        if pnl <= -limit:
            block(f"{source}: today's realized {pnl:+.2f} breached -{limit:.0f} → halts")
        else:
            ok(f"{source}: today's realized {pnl:+.2f} of -{limit:.0f} budget")


def check_schema() -> None:
    head("4. live_trades schema (migration landed?)")
    try:
        with sqlite3.connect(config.LEDGER_DB) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)")}
    except Exception as exc:
        block(f"cannot open {config.LEDGER_DB}: {exc}")
        return
    missing = {"fill_type", "filled_by"} - cols
    if missing:
        block(
            f"live_trades is missing {sorted(missing)} — record_open would raise "
            "AFTER the exchange order is placed, leaving a real position with no "
            "ledger row. Run: .venv/bin/python -c 'import live_ledger; "
            "live_ledger.init_db()'"
        )
    else:
        ok("fill_type + filled_by present")


def check_mill_capacity() -> None:
    head("5. Mill sleeve occupancy")
    try:
        import execute

        cap = execute.mill_capacity()
    except Exception as exc:
        warn(f"mill_capacity failed: {exc}")
        return
    print(
        f"   {cap['open']}/{cap['max_open']} open · exposure "
        f"${cap['open_notional_usd']:,.0f} of ${cap['sleeve_usd']:,.0f} "
        f"(free ${cap['sleeve_free_usd']:,.0f})"
    )
    for t in cap["open_trades"]:
        age = ""
        try:
            opened = datetime.strptime(
                str(t["opened_at"]), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            age = f"  age {(datetime.now(timezone.utc) - opened).days}d"
        except ValueError:
            pass
        print(
            f"     #{t['id']} {t['product_id']} {t['side']} @ {t['entry']} "
            f"· {t['fill_type']}{age}"
        )
    if cap["open"] >= cap["max_open"]:
        block(
            "mill sleeve FULL — auto skips as sleeve_full and your Accepts get "
            "the 'too many trades' reply. Close a clip to free a slot."
        )
    elif cap["open"] > 0:
        warn(
            "mill book is not empty — the auto/FIFO path deliberately only "
            "fills an EMPTY sleeve, so auto fills are paused by design. Your "
            "Accepts can still take the remaining "
            f"{cap['slots_free']} slot(s)."
        )
    else:
        ok("sleeve empty — the next mint above the conviction floor should fill")


def check_gateway() -> None:
    head("6. Coinbase gateway (auth, instruments, balance)")
    if config.EXECUTION_MODE == "off":
        warn("EXECUTION_MODE=off — checking anyway to prove credentials work")
    try:
        from coinbase_deriv import INSTRUMENT_MAP, get_gateway

        gw = get_gateway()
    except Exception as exc:
        block(f"gateway construction failed: {exc}")
        return
    try:
        gw.auth_check()
        ok("CDP auth OK")
    except Exception as exc:
        block(
            f"CDP auth FAILED: {exc} — every fill skips at the instrument "
            "lookup. Check COINBASE_CDP_API_KEY_NAME and that "
            "COINBASE_CDP_PRIVATE_KEY is double-quoted in .env."
        )
        return
    for product in ("ETH-USD", "BTC-USD"):
        try:
            resolved = INSTRUMENT_MAP.get(product)
        except Exception as exc:
            resolved = None
            warn(f"{product} resolution raised {exc}")
        if resolved:
            ok(f"{product} -> {resolved}")
        else:
            block(
                f"{product} has NO instrument mapping — execute logs 'Live skip: "
                "no instrument mapping' and returns. Contract may have rolled or "
                "expired."
            )
    try:
        summary = gw.get_account_summary()
        raw = summary.get("raw") or {}

        def val(key: str) -> float:
            return float((raw.get(key) or {}).get("value") or 0)

        # These are margined futures, so the sleeve constants are NOTIONAL
        # caps, not cash requirements. Spot USDC counts as collateral, which
        # is why futures_buying_power >> cfm_usd_balance. Compare against
        # buying power; the futures wallet balance alone is not the budget.
        power = float(summary.get("available_funds") or 0)
        print(
            f"   futures wallet ${val('cfm_usd_balance'):,.2f} · spot/pending "
            f"${val('cbi_usd_balance'):,.2f} · buying power ${power:,.2f}"
        )
        print(
            f"   margin used ${val('initial_margin'):,.2f} · available "
            f"${val('available_margin'):,.2f} · unrealized "
            f"{val('unrealized_pnl'):+,.2f} · realized today "
            f"{val('daily_realized_pnl'):+,.2f}"
        )
        notional_cap = bot_config.LIVE_MILL_SLEEVE_USD + bot_config.LIVE_HQ_EQUITY_USD
        if power < notional_cap:
            warn(
                f"buying power ${power:,.2f} < the ${notional_cap:,.0f} of "
                f"notional both sleeves may open (mill "
                f"{bot_config.LIVE_MILL_SLEEVE_USD:,.0f} + HQ "
                f"{bot_config.LIVE_HQ_EQUITY_USD:,.0f}). Sleeve sizes are "
                "hardcoded and never reconciled against the broker, so a fully "
                "loaded book could be refused margin mid-session."
            )
        else:
            ok(f"buying power covers the ${notional_cap:,.0f} notional cap")
    except Exception as exc:
        warn(f"account summary failed: {exc}")


def check_hq_vault() -> None:
    head("7. HQ / Eva sleeve (vault admission)")
    try:
        import vault

        p = vault.policy_public()
        print(
            f"   nav ${p['nav_usd']:,.0f} · per-name "
            f"${p['notional_per_name_usd']:,.0f} · max_open {p['max_open']} "
            f"· products {', '.join(p['allowed_products'])}"
        )
    except Exception as exc:
        warn(f"vault policy unavailable: {exc}")
    try:
        n = len(live_ledger.get_open_trades(source="hq"))
        if n >= bot_config.LIVE_MAX_OPEN_HQ:
            block(
                f"HQ has {n}/{bot_config.LIVE_MAX_OPEN_HQ} open — new Eva ideas "
                "are skipped until one closes."
            )
        else:
            ok(f"HQ {n}/{bot_config.LIVE_MAX_OPEN_HQ} open")
    except Exception as exc:
        warn(f"could not read HQ open trades: {exc}")
    print(
        "   note: Eva also needs a non-no_trade hourly suggestion AND vault\n"
        "         admission. Grep the log for 'Vault skip:' to see refusals."
    )


def check_recent_activity() -> None:
    head("8. Recent live trades")
    try:
        rows = live_ledger.get_open_trades() + live_ledger.get_closed_trades(limit=10)
    except Exception as exc:
        warn(f"could not read live_trades: {exc}")
        return
    if not rows:
        warn(
            "live_trades is EMPTY — nothing has ever filled. Consistent with a "
            "master switch being off rather than a per-idea gate."
        )
        return
    for t in rows[:10]:
        print(
            f"   #{t['id']} {t['opened_at']} {t['source']:<4} "
            f"{t.get('fill_type', '?'):<6} {t['product_id']} {t['side']} "
            f"qty={t['qty']} {t['status']}"
        )


def main() -> int:
    print("=" * 72)
    print("LIVE EXECUTION DIAGNOSTIC — read-only, places no orders")
    print(f"ledger: {config.LEDGER_DB}")
    print("=" * 72)

    live_ledger.init_db()
    for fn in (
        check_switches,
        check_halt,
        check_daily_loss,
        check_schema,
        check_mill_capacity,
        check_gateway,
        check_hq_vault,
        check_recent_activity,
    ):
        try:
            fn()
        except Exception as exc:
            warn(f"{fn.__name__} crashed: {exc}")

    head("VERDICT")
    if BLOCKERS:
        print(f"   {len(BLOCKERS)} blocker(s) — fix in this order:\n")
        for i, b in enumerate(BLOCKERS, 1):
            print(f"   {i}. {b}\n")
    else:
        print("   No hard blocker found.")
        print("   If nothing is filling anyway, the cause is per-idea rather")
        print("   than structural. Check the logs for these lines:")
        print("     journalctl -u trade-ideas  | grep 'Mill live bridge'")
        print("     journalctl -u eth-dashboard| grep 'Mill idea'")
        print("     journalctl -u eth-agent    | grep -E 'Live skip|Vault skip|LIVE FILL'")
    if WARNINGS:
        print(f"\n   {len(WARNINGS)} warning(s):")
        for w in WARNINGS:
            print(f"     - {w}")
    return 1 if BLOCKERS else 0


if __name__ == "__main__":
    raise SystemExit(main())
