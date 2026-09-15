"""One definition of the scale-out ladder, shared by the live and paper books.

Each book used to derive its own rungs. Live split the clip into whole CDE nano
contracts and let the remainder ride the furthest target; paper divided the size
still open by the targets still ahead, which is an exact even split. The same
plan therefore exited differently depending on which book held it: a
four-contract live clip put half its size on the last target where paper put a
third, and paper laddered a BTC idea across three targets that a one-contract
live clip closes entirely at the first. Paper is the published journal, so that
gap is a reporting error, not just an inconsistency.

``contract_rungs`` is the whole-unit plan the exchange receives. ``weights`` is
the same plan as fractions of the position, which is what a book sized in
fractional units follows so both land on the same ladder shape.
"""

from __future__ import annotations

# A position is never split more finely than this fraction of a unit.
_EPS = 1e-9


def ordered_targets(
    side: str, take_profits: list[float] | None, entry: float
) -> list[float]:
    """Targets in the order price would reach them, wrong-side levels dropped."""
    levels = [float(tp) for tp in (take_profits or []) if tp]
    if side == "long":
        return sorted(tp for tp in levels if tp > entry)
    return sorted((tp for tp in levels if tp < entry), reverse=True)


def contract_rungs(contracts: int, levels: list[float]) -> list[tuple[float, int]]:
    """Whole-unit scale-out plan as ``(price, contracts)`` rungs.

    Contracts are indivisible, so a clip with fewer contracts than targets
    can't use the whole ladder. It banks at the *nearest* targets rather than
    skipping early profit — a one-contract clip closes fully at TP1.
    """
    if contracts <= 0 or not levels:
        return []
    used = levels[: min(contracts, len(levels))]
    rungs = len(used)
    base, extra = divmod(contracts, rungs)
    # The remainder rides the furthest targets, so the runner is the last rung.
    return [
        (price, base + (1 if i >= rungs - extra else 0))
        for i, price in enumerate(used)
    ]


def unit_count(qty: float, unit: float | None) -> int:
    """How many indivisible lots a position holds. ``0`` when it holds none."""
    if qty <= 0 or not unit or unit <= 0:
        return 0
    return int(qty / unit + _EPS)


def weights(
    levels: list[float], *, units: int | None = None
) -> list[tuple[float, float]]:
    """Each rung's share of the position as ``(price, fraction)``.

    ``units`` is how many indivisible lots the position holds; ``None`` means
    the book can split freely, so every target gets an even share. A position
    holding less than one whole lot still cannot be split, so it gets a single
    rung rather than none. Fractions sum to 1, which lets a caller apply them
    to whatever size it is actually holding.
    """
    if not levels:
        return []
    if units is None:
        share = 1.0 / len(levels)
        return [(price, share) for price in levels]
    plan = contract_rungs(max(int(units), 1), levels)
    total = sum(n for _, n in plan)
    if total <= 0:
        return []
    return [(price, n / total) for price, n in plan]
