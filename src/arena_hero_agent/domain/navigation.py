"""Deterministic four-way navigation over explicit terrain knowledge."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from .value_objects import Coordinate, Direction, _require_int

_PATH_DIRECTION_ORDER = (
    Direction.EAST,
    Direction.SOUTH,
    Direction.WEST,
    Direction.NORTH,
)
EXPLORE_RING_COUNT = 5


class UnknownTraversalPolicy(StrEnum):
    """Caller-selected treatment of cells absent from terrain observations."""

    __canonical_name__ = "arena-hero.unknown-traversal-policy.v1"

    BLOCK = "block"
    ALLOW = "allow"


class CellState(StrEnum):
    """Semantic terrain knowledge for a coordinate."""

    __canonical_name__ = "arena-hero.cell-state.v1"

    UNKNOWN = "unknown"
    OPEN = "open"
    BLOCKED = "blocked"


class UnreachableError(ValueError):
    """Raised only when the explored graph proves that no path exists."""


class SearchLimitExceeded(RuntimeError):
    """Raised when explicit search limits prevent a reachability conclusion."""


@dataclass(frozen=True, slots=True)
class SearchLimits:
    """Finite BFS envelope for searches that may traverse unknown unbounded terrain."""

    __canonical_name__ = "arena-hero.search-limits.v1"

    node_budget: int
    search_radius: int

    def __post_init__(self) -> None:
        budget = _require_int("node_budget", self.node_budget)
        radius = _require_int("search_radius", self.search_radius)
        if budget < 1:
            raise ValueError("node_budget must be positive")
        if radius < 1:
            raise ValueError("search_radius must be positive")


TS_COMPATIBLE_SEARCH_LIMITS = SearchLimits(node_budget=4096, search_radius=24)


@dataclass(frozen=True, slots=True)
class Bounds:
    """Inclusive signed-coordinate navigation bounds."""

    __canonical_name__ = "arena-hero.navigation-bounds.v1"

    min_x: int
    max_x: int
    min_y: int
    max_y: int

    def __post_init__(self) -> None:
        minimum_x = Coordinate(self.min_x, 0).x
        maximum_x = Coordinate(self.max_x, 0).x
        minimum_y = Coordinate(0, self.min_y).y
        maximum_y = Coordinate(0, self.max_y).y
        if minimum_x > maximum_x:
            raise ValueError("bounds min_x cannot exceed max_x")
        if minimum_y > maximum_y:
            raise ValueError("bounds min_y cannot exceed max_y")

    def contains(self, coordinate: Coordinate) -> bool:
        return self.min_x <= coordinate.x <= self.max_x and self.min_y <= coordinate.y <= self.max_y


@dataclass(frozen=True, slots=True)
class NavigationGrid:
    """Immutable three-state terrain projection used by pure navigation.

    Cells in neither collection are unknown. Unknown cells are never silently treated as
    empty: every traversal operation requires an :class:`UnknownTraversalPolicy`.
    """

    __canonical_name__ = "arena-hero.navigation-grid.v1"

    open_cells: frozenset[Coordinate] = field(default_factory=frozenset)
    blocked_cells: frozenset[Coordinate] = field(default_factory=frozenset)
    bounds: Bounds | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.open_cells, frozenset):
            raise TypeError("open_cells must be a frozenset")
        if not isinstance(self.blocked_cells, frozenset):
            raise TypeError("blocked_cells must be a frozenset")
        if any(not isinstance(cell, Coordinate) for cell in self.open_cells):
            raise TypeError("open_cells must contain only Coordinate values")
        if any(not isinstance(cell, Coordinate) for cell in self.blocked_cells):
            raise TypeError("blocked_cells must contain only Coordinate values")
        overlap = self.open_cells & self.blocked_cells
        if overlap:
            cells = ", ".join(cell.cell_key for cell in sorted(overlap))
            raise ValueError(f"terrain cells cannot be both open and blocked: {cells}")
        if self.bounds is not None:
            outside = sorted(
                cell
                for cell in self.open_cells | self.blocked_cells
                if not self.bounds.contains(cell)
            )
            if outside:
                cells = ", ".join(cell.cell_key for cell in outside)
                raise ValueError(f"terrain cells fall outside bounds: {cells}")

    def state_at(self, coordinate: Coordinate) -> CellState:
        if self.bounds is not None and not self.bounds.contains(coordinate):
            raise ValueError(f"coordinate is outside navigation bounds: {coordinate.cell_key}")
        if coordinate in self.blocked_cells:
            return CellState.BLOCKED
        if coordinate in self.open_cells:
            return CellState.OPEN
        return CellState.UNKNOWN

    def can_enter(
        self,
        coordinate: Coordinate,
        unknown_policy: UnknownTraversalPolicy,
    ) -> bool:
        if not isinstance(unknown_policy, UnknownTraversalPolicy):
            raise TypeError("unknown_policy must be an UnknownTraversalPolicy")
        if self.bounds is not None and not self.bounds.contains(coordinate):
            return False
        state = self.state_at(coordinate)
        if state is CellState.BLOCKED:
            return False
        if state is CellState.OPEN:
            return True
        return unknown_policy is UnknownTraversalPolicy.ALLOW


def manhattan(first: Coordinate, second: Coordinate) -> int:
    """Return four-way grid distance."""

    return abs(first.x - second.x) + abs(first.y - second.y)


def chebyshev(first: Coordinate, second: Coordinate) -> int:
    """Return king-move grid distance."""

    return max(abs(first.x - second.x), abs(first.y - second.y))


def direction_to_adjacent(origin: Coordinate, target: Coordinate) -> Direction:
    """Return the cardinal direction to an adjacent target."""

    dx = target.x - origin.x
    dy = target.y - origin.y
    if abs(dx) + abs(dy) != 1:
        raise ValueError("target must be cardinally adjacent")
    if dx == 1:
        return Direction.EAST
    if dx == -1:
        return Direction.WEST
    if dy == 1:
        return Direction.SOUTH
    return Direction.NORTH


def _ordered_directions(origin: Coordinate, target: Coordinate) -> tuple[Direction, ...]:
    dx = target.x - origin.x
    dy = target.y - origin.y
    x_direction = None if dx == 0 else Direction.EAST if dx > 0 else Direction.WEST
    y_direction = None if dy == 0 else Direction.SOUTH if dy > 0 else Direction.NORTH
    preferred: list[Direction] = []
    axes = (x_direction, y_direction) if abs(dx) >= abs(dy) else (y_direction, x_direction)
    preferred.extend(direction for direction in axes if direction is not None)
    preferred.extend(direction for direction in _PATH_DIRECTION_ORDER if direction not in preferred)
    return tuple(preferred)


def _candidate_neighbors(origin: Coordinate, target: Coordinate) -> tuple[Coordinate, ...]:
    neighbors: list[Coordinate] = []
    for direction in _ordered_directions(origin, target):
        try:
            neighbors.append(origin.step(direction))
        except ValueError:
            # Signed-int32 overflow is the edge of the domain grid.
            continue
    return tuple(neighbors)


def shortest_path(
    grid: NavigationGrid,
    start: Coordinate,
    target: Coordinate,
    *,
    unknown_policy: UnknownTraversalPolicy,
    limits: SearchLimits | None = None,
) -> tuple[Coordinate, ...]:
    """Return the deterministic shortest path, including start and target.

    Breadth-first expansion is target-biased and otherwise uses the pinned east, south,
    west, north tie order from the TypeScript oracle. Duplicate discovery is suppressed
    before enqueueing, so input insertion order cannot affect the result. Traversing unknown
    cells without finite bounds requires explicit limits; exhausting those limits raises
    :class:`SearchLimitExceeded`, never :class:`UnreachableError`.
    """

    if unknown_policy is UnknownTraversalPolicy.ALLOW and grid.bounds is None and limits is None:
        raise ValueError("unbounded unknown traversal requires explicit SearchLimits")
    if not grid.can_enter(start, unknown_policy):
        raise UnreachableError(f"start is not traversable: {start.cell_key}")
    if not grid.can_enter(target, unknown_policy):
        raise UnreachableError(f"target is not traversable: {target.cell_key}")
    if limits is not None and chebyshev(start, target) > limits.search_radius:
        raise SearchLimitExceeded(
            f"target {target.cell_key} lies outside search radius {limits.search_radius}"
        )
    if start == target:
        return (start,)

    queue: deque[Coordinate] = deque([start])
    parents: dict[Coordinate, Coordinate | None] = {start: None}
    expanded = 0
    radius_pruned = False
    while queue:
        if limits is not None and expanded >= limits.node_budget:
            raise SearchLimitExceeded(
                f"node budget {limits.node_budget} exhausted before reachability was known"
            )
        current = queue.popleft()
        expanded += 1
        for neighbor in _candidate_neighbors(current, target):
            if limits is not None and chebyshev(start, neighbor) > limits.search_radius:
                if (
                    grid.bounds is None or grid.bounds.contains(neighbor)
                ) and neighbor not in grid.blocked_cells:
                    radius_pruned = True
                continue
            if neighbor in parents or not grid.can_enter(neighbor, unknown_policy):
                continue
            parents[neighbor] = current
            if neighbor == target:
                path = [target]
                cursor = current
                while cursor is not None:
                    path.append(cursor)
                    cursor = parents[cursor]
                path.reverse()
                return tuple(path)
            queue.append(neighbor)
    if radius_pruned:
        raise SearchLimitExceeded(
            f"search radius {limits.search_radius if limits is not None else 0} "
            "exhausted before reachability was known"
        )
    raise UnreachableError(f"no path from {start.cell_key} to {target.cell_key}")


def first_step(
    grid: NavigationGrid,
    start: Coordinate,
    target: Coordinate,
    *,
    unknown_policy: UnknownTraversalPolicy,
    limits: SearchLimits | None = None,
) -> Direction:
    """Return the first direction on the deterministic shortest path."""

    path = shortest_path(
        grid,
        start,
        target,
        unknown_policy=unknown_policy,
        limits=limits,
    )
    if len(path) < 2:
        raise ValueError("start and target are identical; no first step exists")
    return direction_to_adjacent(path[0], path[1])


def is_reachable(
    grid: NavigationGrid,
    start: Coordinate,
    target: Coordinate,
    *,
    unknown_policy: UnknownTraversalPolicy,
    limits: SearchLimits | None = None,
) -> bool:
    """Return proven reachability; search-limit exhaustion propagates to the caller."""

    try:
        shortest_path(
            grid,
            start,
            target,
            unknown_policy=unknown_policy,
            limits=limits,
        )
    except UnreachableError:
        return False
    return True


def reachable_cells(
    grid: NavigationGrid,
    start: Coordinate,
    *,
    unknown_policy: UnknownTraversalPolicy,
    max_distance: int | None = None,
) -> frozenset[Coordinate]:
    """Return all cells reachable from start within an optional path distance."""

    if max_distance is not None:
        distance_limit = _require_int("max_distance", max_distance)
        if distance_limit < 0:
            raise ValueError("max_distance cannot be negative")
    elif unknown_policy is UnknownTraversalPolicy.ALLOW and grid.bounds is None:
        raise ValueError("unbounded unknown traversal requires max_distance or bounds")
    else:
        distance_limit = None

    if not grid.can_enter(start, unknown_policy):
        raise UnreachableError(f"start is not traversable: {start.cell_key}")
    queue: deque[tuple[Coordinate, int]] = deque([(start, 0)])
    visited = {start}
    while queue:
        current, distance = queue.popleft()
        if distance_limit is not None and distance >= distance_limit:
            continue
        for direction in _PATH_DIRECTION_ORDER:
            try:
                neighbor = current.step(direction)
            except ValueError:
                continue
            if neighbor in visited or not grid.can_enter(neighbor, unknown_policy):
                continue
            visited.add(neighbor)
            queue.append((neighbor, distance + 1))
    return frozenset(visited)


def vision_line_blocked(
    origin: Coordinate,
    target: Coordinate,
    obstacles: frozenset[Coordinate],
) -> bool:
    """Return whether integer-supercover intermediates block line of sight.

    Diagonal corner crossings include both side cells. The target cell itself never
    blocks visibility, matching the pinned v0.14 oracle behavior.
    """

    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset")
    if any(not isinstance(cell, Coordinate) for cell in obstacles):
        raise TypeError("obstacles must contain only Coordinate values")
    dx = target.x - origin.x
    dy = target.y - origin.y
    nx = abs(dx)
    ny = abs(dy)
    sx = (dx > 0) - (dx < 0)
    sy = (dy > 0) - (dy < 0)
    x = origin.x
    y = origin.y
    ix = 0
    iy = 0
    while ix < nx or iy < ny:
        next_x = (2 * ix + 1) * ny
        next_y = (2 * iy + 1) * nx
        if next_x < next_y:
            x += sx
            ix += 1
        elif next_x > next_y:
            y += sy
            iy += 1
        else:
            side_x = Coordinate(x + sx, y)
            side_y = Coordinate(x, y + sy)
            if side_x != target and side_x in obstacles:
                return True
            if side_y != target and side_y in obstacles:
                return True
            x += sx
            y += sy
            ix += 1
            iy += 1
        current = Coordinate(x, y)
        if current == target:
            break
        if current in obstacles:
            return True
    return False


def explore_radius_for_ring(base_radius: int, ring_index: int) -> int:
    """Return the five-ring deterministic exploration radius."""

    base = _require_int("base_radius", base_radius)
    ring = _require_int("ring_index", ring_index)
    if base < 1:
        raise ValueError("base_radius must be positive")
    return base * ((ring % EXPLORE_RING_COUNT) + 1)
