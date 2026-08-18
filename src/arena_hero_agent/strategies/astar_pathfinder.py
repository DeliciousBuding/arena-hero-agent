"""A* shortest-path pathfinder with Manhattan heuristic.

Designed based on cross-project analysis of 5 reference implementations:

- **waaiging**: A* with Manhattan + threat + visited penalties (30k cap)
- **wuwd**: A* with Manhattan + discouraged cells (4k cap)
- **tactic**: A* with soft-avoid + wall-following fallback (480-5k cap)
- **massarmy**: A* with target_radius + goal-set fallback (4k cap)
- **Legacy TS oracle**: 3-tier BFS (radius→expanding-box→greedy)

A* is preferred over BFS for long-distance routing (36+ tiles to resources):
the Manhattan heuristic guides the search toward the target, reducing
expansions by an order of magnitude vs. BFS on a 64-radius grid.

Key design decisions:
- **4-connected** (cardinal only, matching game movement rules)
- **Manhattan heuristic** (admissible for 4-connected uniform-cost grid)
- **Discouraged cells** (+4 cost penalty, not hard block) — for recent trail
  / threat avoidance
- **Goal-set fallback** — if target is on an obstacle, accept any passable
  neighbor at Manhattan distance 1
- **Deterministic tie-break** — insertion sequence in the priority queue
- **Bounded** — Chebyshev search radius + hard node budget cap
"""

from __future__ import annotations

import heapq
from typing import Final

from arena_hero_agent.domain import Coordinate, Direction

_DISCOURAGED_PENALTY: Final = 4
_DEFAULT_SEARCH_RADIUS: Final = 128
_DEFAULT_NODE_BUDGET: Final = 32768

_ASTAR_DELTAS: Final[tuple[tuple[int, int, Direction], ...]] = (
    (1, 0, Direction.EAST),
    (0, 1, Direction.SOUTH),
    (-1, 0, Direction.WEST),
    (0, -1, Direction.NORTH),
)


def _decode_obstacles(obstacles: frozenset[str]) -> frozenset[tuple[int, int]]:
    """Decode cell-key strings to integer (x, y) tuples."""

    decoded: set[tuple[int, int]] = set()
    for key in obstacles:
        if not isinstance(key, str):
            continue
        comma = key.find(",")
        if comma < 0:
            continue
        try:
            decoded.add((int(key[:comma]), int(key[comma + 1:])))
        except ValueError:
            continue
    return frozenset(decoded)


def _chebyshev_distance(
    origin: tuple[int, int],
    target: tuple[int, int],
) -> int:
    return max(abs(target[0] - origin[0]), abs(target[1] - origin[1]))


def _manhattan_distance(
    origin: tuple[int, int],
    target: tuple[int, int],
) -> int:
    return abs(target[0] - origin[0]) + abs(target[1] - origin[1])


def astar_next_step(
    start: Coordinate,
    target: Coordinate,
    obstacles: frozenset[str],
    *,
    discouraged: frozenset[str] | None = None,
    search_radius: int = _DEFAULT_SEARCH_RADIUS,
    node_budget: int = _DEFAULT_NODE_BUDGET,
) -> Direction | None:
    """Return the first cardinal step of an A* route from ``start`` to ``target``.

    Unknown cells are traversable; obstacle cells are blocked. The search is
    Chebyshev-bounded and node-budget-bounded. ``None`` means no route was
    found within the budget.

    When ``discouraged`` is provided, those cells receive a +4 cost penalty
    (soft avoidance, not a hard block). This is useful for:

    - Recent worker trail (prevent retracing the same path)
    - Threat cells (avoid enemy-adjacent positions)

    If ``target`` is on an obstacle, the function falls back to accepting any
    passable neighbor at Manhattan distance 1 from the target (goal-set
    fallback, matching the wuwd/massarmy pattern).
    """

    if not isinstance(start, Coordinate) or not isinstance(target, Coordinate):
        raise TypeError("start and target must be Coordinate values")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    if discouraged is not None and not isinstance(discouraged, frozenset):
        raise TypeError("discouraged must be a frozenset or None")
    if not isinstance(search_radius, int) or isinstance(search_radius, bool):
        raise TypeError("search_radius must be an integer")
    if search_radius < 1:
        raise ValueError("search_radius must be at least 1")
    if not isinstance(node_budget, int) or isinstance(node_budget, bool):
        raise TypeError("node_budget must be an integer")
    if node_budget < 1:
        raise ValueError("node_budget must be at least 1")

    start_position = (start.x, start.y)
    target_position = (target.x, target.y)

    if start_position == target_position:
        return None

    blocked = _decode_obstacles(obstacles)
    discouraged_set = _decode_obstacles(discouraged) if discouraged is not None else frozenset()

    if target_position in blocked:
        goals = frozenset(
            (target.x + dx, target.y + dy)
            for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1))
            if (target.x + dx, target.y + dy) not in blocked
        )
    else:
        goals = frozenset({target_position})

    if not goals or start_position in goals:
        return None

    def heuristic(position: tuple[int, int]) -> int:
        return min(_manhattan_distance(position, goal) for goal in goals)

    open_heap: list[tuple[int, int, int, tuple[int, int]]] = []
    start_h = heuristic(start_position)
    heapq.heappush(open_heap, (start_h, start_h, 0, start_position))

    g_score: dict[tuple[int, int], int] = {start_position: 0}
    came_from: dict[tuple[int, int], tuple[tuple[int, int], Direction]] = {}

    sequence = 0
    expansions = 0

    while open_heap and expansions < node_budget:
        _, _, current_cost, current = heapq.heappop(open_heap)

        if current_cost != g_score.get(current, -1):
            continue

        if current in goals:
            first_step = current
            while came_from.get(first_step, (None, None))[0] != start_position:
                first_step = came_from[first_step][0]
            return came_from[first_step][1]

        expansions += 1

        for delta_x, delta_y, direction in _ASTAR_DELTAS:
            neighbor = (current[0] + delta_x, current[1] + delta_y)

            if neighbor in blocked:
                continue

            if _chebyshev_distance(start_position, neighbor) > search_radius:
                continue

            step_cost = 1
            if neighbor in discouraged_set:
                step_cost += _DISCOURAGED_PENALTY

            new_cost = current_cost + step_cost
            if new_cost >= g_score.get(neighbor, 0x7FFFFFFF):
                continue

            g_score[neighbor] = new_cost
            came_from[neighbor] = (current, direction)
            sequence += 1
            neighbor_h = heuristic(neighbor)
            heapq.heappush(
                open_heap,
                (new_cost + neighbor_h, neighbor_h, sequence, neighbor),
            )

    return None


__all__ = ["astar_next_step"]
