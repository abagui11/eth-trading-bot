"""Shared data models for trade suggestions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Suggestion:
  action: str  # spot_buy|spot_sell|deriv_buy|deriv_sell|no_trade
  size: float
  entry: float | None
  stop_loss: float | None
  take_profits: list[float] = field(default_factory=list)
  risk_reward: float | None = None
  rationale: str = ""
  order_block: dict[str, Any] | None = None  # low, high, start_ts, end_ts

  @classmethod
  def no_trade(cls, rationale: str = "No setup") -> Suggestion:
    return cls(
      action="no_trade",
      size=0.0,
      entry=None,
      stop_loss=None,
      take_profits=[],
      risk_reward=None,
      rationale=rationale,
      order_block=None,
    )

  @classmethod
  def from_dict(cls, data: dict[str, Any]) -> Suggestion:
    return cls(
      action=str(data.get("action", "no_trade")),
      size=float(data.get("size", 0) or 0),
      entry=float(data["entry"]) if data.get("entry") is not None else None,
      stop_loss=float(data["stop_loss"]) if data.get("stop_loss") is not None else None,
      take_profits=[float(tp) for tp in data.get("take_profits", [])],
      risk_reward=float(data["risk_reward"]) if data.get("risk_reward") is not None else None,
      rationale=str(data.get("rationale", "")),
      order_block=data.get("order_block"),
    )
