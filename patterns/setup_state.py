"""Track multi-hour setup phases (e.g. bearish OB retest) across hourly cycles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from patterns.signal_state import get_state, set_state

SETUP_STATE_KEY = "trade_setup_state"

SetupPhase = Literal[
    "idle",
    "awaiting_bearish_retest",
    "bearish_retest_filled",
    "bearish_retest_rejected",
]


@dataclass
class SetupState:
    phase: SetupPhase
    retest_low: float | None = None
    retest_high: float | None = None
    tagged_high: float | None = None
    tagged_ts: str | None = None
    updated_ts: str | None = None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "retest_low": self.retest_low,
            "retest_high": self.retest_high,
            "tagged_high": self.tagged_high,
            "tagged_ts": self.tagged_ts,
            "updated_ts": self.updated_ts,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> SetupState:
        if not data:
            return cls(phase="idle")
        return cls(
            phase=str(data.get("phase", "idle")),  # type: ignore[arg-type]
            retest_low=float(data["retest_low"]) if data.get("retest_low") is not None else None,
            retest_high=float(data["retest_high"]) if data.get("retest_high") is not None else None,
            tagged_high=float(data["tagged_high"]) if data.get("tagged_high") is not None else None,
            tagged_ts=data.get("tagged_ts"),
            updated_ts=data.get("updated_ts"),
        )


def load_setup_state() -> SetupState:
    return SetupState.from_dict(get_state(SETUP_STATE_KEY))


def save_setup_state(state: SetupState) -> None:
    set_state(SETUP_STATE_KEY, state.to_dict())


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_bearish_retest_state(
    *,
    spot: float,
    range_high_24h: float | None,
    retest_low: float | None,
    retest_high: float | None,
    htf_bearish_bias: bool,
    recent_bearish_h1_sfp: bool,
) -> tuple[SetupState, list[str], list[str]]:
    """
    Advance bearish retest setup state and return (state, alerts, setup_tags).
    """
    alerts: list[str] = []
    tags: list[str] = []
    state = load_setup_state()
    now = _now_iso()

    if retest_low is None or retest_high is None or not htf_bearish_bias:
        if state.phase != "idle":
            state = SetupState(phase="idle", updated_ts=now)
            save_setup_state(state)
        return state, alerts, tags

    high = range_high_24h if range_high_24h is not None else spot
    retest_tagged = high >= retest_low
    newly_filled = False

    if state.phase in ("idle", "awaiting_bearish_retest"):
        if retest_tagged:
            state = SetupState(
                phase="bearish_retest_filled",
                retest_low=retest_low,
                retest_high=retest_high,
                tagged_high=high,
                tagged_ts=now,
                updated_ts=now,
            )
            newly_filled = True
            alerts.append(
                f"BEARISH RETEST FILLED: 24h high {high:,.2f} tagged supply zone "
                f"{retest_low:,.2f}-{retest_high:,.2f}"
            )
            tags.append("bearish_retest_filled")
        elif spot < retest_low:
            state = SetupState(
                phase="awaiting_bearish_retest",
                retest_low=retest_low,
                retest_high=retest_high,
                updated_ts=now,
            )
            alerts.append(
                f"Awaiting bearish HTF retest: rally into supply zone "
                f"{retest_low:,.2f}-{retest_high:,.2f} for short entry"
            )
            tags.append("awaiting_bearish_retest")

    if state.phase == "bearish_retest_filled" and not newly_filled:
        if retest_tagged and state.tagged_high is None:
            state.tagged_high = high
            state.tagged_ts = now
        rejection_threshold = retest_low * 0.998
        if spot < rejection_threshold and (state.tagged_high or high) >= retest_low:
            state.phase = "bearish_retest_rejected"
            state.updated_ts = now
            alerts.append(
                f"RETEST REJECTION: price {spot:,.2f} fell back below supply zone after "
                f"tagging {state.tagged_high or high:,.2f} — favor SHORT if LTF aligns"
            )
            tags.append("bearish_retest_rejected")
            if recent_bearish_h1_sfp:
                alerts.append(
                    "SHORT TRIGGER: bearish H1 SFP at resistance + retest rejection "
                    "(evaluate deriv_sell / spot_sell)"
                )
                tags.append("short_trigger_retest")

    if state.phase == "bearish_retest_rejected" and spot >= retest_low:
        # Price reclaimed supply — reset to filled, not idle.
        state.phase = "bearish_retest_filled"
        state.updated_ts = now

    state.retest_low = retest_low
    state.retest_high = retest_high
    save_setup_state(state)
    return state, alerts, tags
