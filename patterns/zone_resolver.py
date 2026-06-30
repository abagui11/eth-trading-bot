"""Resolve canonical HTF zones at the current price."""

from __future__ import annotations

from dataclasses import dataclass

from patterns.htf_structure import HTFZone


@dataclass
class ZoneSnapshot:
    spot: float
    zones_containing_price: list[HTFZone]
    primary_bullish: HTFZone | None
    primary_bearish: HTFZone | None
    nearest_bearish_above: HTFZone | None
    nearest_bullish_below: HTFZone | None
    bearish_retest_low: float | None
    bearish_retest_high: float | None


def _active(zones: list[HTFZone]) -> list[HTFZone]:
    return [z for z in zones if not z.mitigated]


def _contains(spot: float, zone: HTFZone) -> bool:
    return zone.low <= spot <= zone.high


def _zone_span(zone: HTFZone) -> float:
    return max(zone.high - zone.low, 0.0)


def bearish_supply_bounds(zone: HTFZone, premium_start: float = 0.5) -> tuple[float, float]:
    """Upper portion of a bearish zone — where short retests are expected."""
    span = _zone_span(zone)
    low = zone.low + span * premium_start
    return round(low, 2), round(zone.high, 2)


def bullish_demand_bounds(zone: HTFZone, discount_end: float = 0.5) -> tuple[float, float]:
    """Lower portion of a bullish zone — where long entries are expected."""
    span = _zone_span(zone)
    high = zone.low + span * discount_end
    return round(zone.low, 2), round(high, 2)


def _nearest_above(spot: float, zones: list[HTFZone], direction: str) -> HTFZone | None:
    candidates = [
        z
        for z in zones
        if z.direction == direction and z.low > spot
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda z: z.low - spot)


def _nearest_below(spot: float, zones: list[HTFZone], direction: str) -> HTFZone | None:
    candidates = [
        z
        for z in zones
        if z.direction == direction and z.high < spot
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda z: spot - z.high)


def _pick_primary(containing: list[HTFZone], direction: str) -> HTFZone | None:
    matches = [z for z in containing if z.direction == direction]
    if not matches:
        return None
    # Prefer the largest active zone (most structural weight).
    return max(matches, key=lambda z: _zone_span(z))


def resolve_zones(spot: float, htf_zones: list[HTFZone]) -> ZoneSnapshot:
    """Pick the zones that matter at `spot` instead of dumping the full history."""
    active = _active(htf_zones)
    containing = [z for z in active if _contains(spot, z)]
    primary_bull = _pick_primary(containing, "bullish")
    primary_bear = _pick_primary(containing, "bearish")

    retest_low: float | None = None
    retest_high: float | None = None
    if primary_bear is not None:
        retest_low, retest_high = bearish_supply_bounds(primary_bear)
    else:
        above = _nearest_above(spot, active, "bearish")
        if above is not None:
            retest_low, retest_high = bearish_supply_bounds(above)

    return ZoneSnapshot(
        spot=spot,
        zones_containing_price=containing,
        primary_bullish=primary_bull,
        primary_bearish=primary_bear,
        nearest_bearish_above=_nearest_above(spot, active, "bearish"),
        nearest_bullish_below=_nearest_below(spot, active, "bullish"),
        bearish_retest_low=retest_low,
        bearish_retest_high=retest_high,
    )


def format_zone(zone: HTFZone) -> str:
    mitigated = "mitigated" if zone.mitigated else "active"
    return (
        f"{zone.zone_type} {zone.direction} {zone.low:,.2f}-{zone.high:,.2f} "
        f"@ {zone.start_ts[:16]} ({mitigated})"
    )
