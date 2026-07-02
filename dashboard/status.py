"""Human-readable agent status from audit snapshots."""

from __future__ import annotations

from typing import Any

TRADE_ACTIONS = frozenset({"spot_buy", "spot_sell", "deriv_buy", "deriv_sell"})

_PHASE_HEADLINES = {
    "idle": "Monitoring H12 structure — no active setup phase",
    "awaiting_bearish_retest": "Waiting for rally into bearish HTF supply zone",
    "bearish_retest_filled": "Bearish retest zone tagged — watching for rejection",
    "bearish_retest_rejected": "Retest rejection — favor short if LTF aligns",
}


def _fmt_zone(low: float, high: float) -> str:
    return f"{low:,.2f}–{high:,.2f}"


def format_agent_status(
    snapshot_row: dict[str, Any] | None,
    *,
    ledger_row: dict[str, Any] | None = None,
    open_positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build status card fields for the dashboard."""
    if snapshot_row is None and ledger_row is None and not open_positions:
        return {
            "headline": "No analysis cycles recorded yet",
            "phase": "unknown",
            "alerts": [],
            "watching": [],
            "cycle_id": None,
            "action": None,
            "ts": None,
        }

    snap = (snapshot_row or {}).get("snapshot") or {}
    suggestion = (snapshot_row or {}).get("suggestion") or {}
    action = str(
        (ledger_row or {}).get("action") or suggestion.get("action") or "no_trade"
    )
    cycle_id = str((snapshot_row or {}).get("cycle_id") or (ledger_row or {}).get("cycle_id") or "")
    ts = str((snapshot_row or {}).get("ts") or (ledger_row or {}).get("ts") or "")

    setup_state = snap.get("setup_state") or {}
    phase = str(setup_state.get("phase") or "idle")
    alerts = list(snap.get("alerts") or [])

    watching: list[str] = []
    zone_snap = snap.get("zone_snapshot") or {}
    retest_low = zone_snap.get("bearish_retest_low") or setup_state.get("retest_low")
    retest_high = zone_snap.get("bearish_retest_high") or setup_state.get("retest_high")
    if retest_low is not None and retest_high is not None:
        watching.append(f"Bearish retest zone: {_fmt_zone(float(retest_low), float(retest_high))}")

    for ob in (snap.get("order_blocks") or [])[-2:]:
        direction = str(ob.get("direction", ""))
        low, high = float(ob["low"]), float(ob["high"])
        watching.append(f"H1 {direction} order block: {_fmt_zone(low, high)}")

    for zone in (zone_snap.get("zones_containing_price") or [])[:2]:
        ztype = str(zone.get("zone_type", "zone")).upper()
        direction = str(zone.get("direction", ""))
        low, high = float(zone["low"]), float(zone["high"])
        watching.append(f"H12 {ztype} {direction}: {_fmt_zone(low, high)}")

    # Open position overrides headline
    if open_positions:
        pos = open_positions[0]
        side = str(pos.get("side", ""))
        entry = float(pos.get("avg_entry") or 0)
        sl = float(pos.get("stop_loss") or 0)
        tps = pos.get("take_profits") or []
        tp_str = f"{float(tps[0]):,.2f}" if tps else "n/a"
        headline = (
            f"In {side} from ${entry:,.2f} — SL ${sl:,.2f}, TP ${tp_str}"
        )
        if len(open_positions) > 1:
            headline = f"{len(open_positions)} open positions — {headline}"
    elif alerts:
        headline = alerts[0]
    elif phase in _PHASE_HEADLINES:
        headline = _PHASE_HEADLINES[phase]
        if retest_low is not None and retest_high is not None and phase != "idle":
            headline += f" ({_fmt_zone(float(retest_low), float(retest_high))})"
    elif action == "no_trade":
        headline = "No trade — waiting for H1 fib retest and H12/LTF alignment"
    else:
        headline = f"Latest action: {action.replace('_', ' ')}"

    return {
        "headline": headline,
        "phase": phase,
        "alerts": alerts[:6],
        "watching": watching[:6],
        "cycle_id": cycle_id or None,
        "action": action,
        "ts": ts or None,
        "spot_at_cycle": snap.get("spot"),
    }
