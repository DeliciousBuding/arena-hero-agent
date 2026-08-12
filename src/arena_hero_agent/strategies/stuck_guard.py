"""Deterministic stuck-worker guard (research variant; disabled by default).

Detects workers whose recent movement is frozen (no displacement for
``n_ticks``) or whose last ``n_ticks`` positions stay inside a ``k_cells``
box. The composed decider uses the result to block a stuck worker's current
resource target and force reassignment.

This is a research/experimental layer: it is gated off by default through
``ComposedDeciderConfig.stuck_guard_enabled`` and is not registered as a
production variant in ``variant_registry``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from arena_hero_agent.domain import Coordinate

DEFAULT_STUCK_GUARD_TICKS: Final = 16
DEFAULT_STUCK_GUARD_RADIUS: Final = 6


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def positions_stuck(positions: Sequence[Coordinate], k_cells: int) -> bool:
    """Return whether positions are all identical or confined to a k_cells box."""

    if not positions:
        return False
    first = positions[0]
    if all(position == first for position in positions):
        return True
    xs = [position.x for position in positions]
    ys = [position.y for position in positions]
    return (max(xs) - min(xs)) <= k_cells and (max(ys) - min(ys)) <= k_cells


def detect_stuck_unit_ids(
    positions_by_unit: Mapping[str, Sequence[Coordinate]],
    *,
    n_ticks: int = DEFAULT_STUCK_GUARD_TICKS,
    k_cells: int = DEFAULT_STUCK_GUARD_RADIUS,
) -> frozenset[str]:
    """Return unit ids whose last ``n_ticks`` positions look stuck.

    A unit is stuck when its most recent ``n_ticks`` positions are all identical
    or all fall inside a box of ``k_cells`` per axis. Units with fewer recorded
    positions are skipped (not enough evidence yet).
    """

    n_ticks = _positive_int("n_ticks", n_ticks)
    k_cells = _positive_int("k_cells", k_cells)
    stuck: set[str] = set()
    for unit_id, positions in positions_by_unit.items():
        recent = tuple(positions)[-n_ticks:]
        if len(recent) < n_ticks:
            continue
        if positions_stuck(recent, k_cells):
            stuck.add(unit_id)
    return frozenset(stuck)


__all__ = [
    "DEFAULT_STUCK_GUARD_RADIUS",
    "DEFAULT_STUCK_GUARD_TICKS",
    "detect_stuck_unit_ids",
    "positions_stuck",
]
