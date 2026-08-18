"""Persistent terrain map that accumulates obstacle cells across ticks.

Production lesson (r010-r019 deadlock defense): the BFS pathfinder
(``next_step_toward``) receives ``snapshot.obstacle_cells`` which only
contains **currently visible** obstacles. Unknown cells are treated as
traversable, so the BFS routes workers through invisible obstacles →
``MOVE_BLOCKED_TERRAIN`` → next tick the same route is found → death loop.

All five reference implementations (legacy TS oracle, waaiging, wuwd,
tactic, massarmy) maintain a persistent obstacle set that accumulates
``obstacle_cells`` every tick. This module provides the same capability.

The map also records ``MOVE_BLOCKED_TERRAIN`` destinations as permanent
obstacles — when the BFS routes through an unknown cell and the game
rejects the move, the terrain map learns about the new obstacle. Next
tick, the pathfinder avoids it.
"""

from __future__ import annotations

from typing import Final

from arena_hero_agent.domain import Coordinate, cell_key

_TERRAIN_BLOCKED_SENTINEL: Final = -1


class TerrainMap:
    """Persistent obstacle map accumulated across ticks.

    The map is a simple ``set[str]`` of cell keys (``"x,y"`` format).
    Obstacles are permanent in Arena Hero (deterministic HMAC terrain
    generation), so the set only grows — never shrinks — within a single
    game session. On world reset (tick regression), call :meth:`reset`.

    Usage in :class:`ComposedDecider`::

        accumulated = self._terrain_map.observe(snapshot.obstacle_cells)
        # Pass 'accumulated' (superset of snapshot.obstacle_cells) to
        # pathfinding, migration direction, and movement guard.
    """

    __slots__ = ("_blocked",)

    def __init__(self) -> None:
        self._blocked: set[str] = set()

    def observe(self, obstacle_cells: frozenset[str]) -> frozenset[str]:
        """Union current visible obstacles into the map; return the full set.

        The returned ``frozenset`` is a snapshot of all known obstacles —
        a superset of ``obstacle_cells``. It is safe to pass directly to
        :func:`next_step_toward` or :func:`astar_next_step` as the
        ``obstacles`` parameter.
        """

        if not isinstance(obstacle_cells, frozenset):
            raise TypeError("obstacle_cells must be a frozenset of cell keys")
        self._blocked |= obstacle_cells
        return frozenset(self._blocked)

    def record_blocked_move(self, position: Coordinate) -> None:
        """Record a ``MOVE_BLOCKED_TERRAIN`` destination as a permanent obstacle.

        When the pathfinder routes a worker through an unknown cell and the
        game returns ``MOVE_BLOCKED_TERRAIN``, the destination cell is an
        obstacle that was not in the current vision. Recording it here
        ensures the next-tick pathfinder avoids it.
        """

        if not isinstance(position, Coordinate):
            raise TypeError("position must be a Coordinate")
        self._blocked.add(cell_key(position))

    @property
    def known_obstacle_count(self) -> int:
        """Return the number of accumulated obstacle cells (for diagnostics)."""

        return len(self._blocked)

    def reset(self) -> None:
        """Clear all accumulated obstacles.

        Call on world reset (tick regression — ``snapshot.tick`` decreases).
        The game world may have changed (new seed), so all accumulated
        terrain knowledge is invalid.
        """

        self._blocked.clear()

    def __contains__(self, key: str) -> bool:
        return key in self._blocked

    def __len__(self) -> int:
        return len(self._blocked)
