"""Third-party Core economy-budget strategies as self-contained pure functions.

Absorbs four economy-budget mechanisms from the referenced third-party
implementations:

1. same-tick deposit projection minus heal reserve before Core SPAWN/REPAIR;
2. full-capacity dispersal to home staging points plus CELL_UNIT_LIMIT
   spawn-suspension and Core-cell evacuation;
3. stepped worker-expansion reserve that prices the next stage of workers;
4. full-cargo return-home routing that ignores unit occupancy.

Everything is deterministic with no randomness and no I/O. This layer is not
wired into the composed decider yet; the main session may import the public
functions below when integrating into ``ComposedDecider``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    TS_COMPATIBLE_SEARCH_LIMITS,
    Coordinate,
    Direction,
    NavigationGrid,
    RulesVersion,
    SearchLimitExceeded,
    UnitRole,
    UnknownTraversalPolicy,
    UnreachableError,
    manhattan,
    shortest_path,
    unit_price,
)

# --- Mechanism 1: same-tick deposit + heal reserve ----------------------------

HEAL_RESOURCE_RESERVE: Final = 10


def unit_max_health(role: UnitRole) -> int:
    """Return the reference maximum health for one controlled unit role."""

    return 4 if role is UnitRole.VANGUARD else 2


def heal_reserve(healing_roles: Iterable[UnitRole]) -> int:
    """Return the worst-case resource cost to fully heal the given units."""

    return sum(unit_max_health(role) - 1 for role in healing_roles)


def projected_core_resources(
    *,
    resources: int,
    resource_space: int,
    deposit_cargo: int,
    healing_reserve: int = 0,
) -> int:
    """Return same-tick projected Core resources after deposits minus heal reserve.

    Deposits can never exceed free capacity, and the projected balance never goes
    below zero. Compose with :func:`heal_reserve` to subtract the worst-case cost
    of every unit scheduled to HEAL this tick.
    """

    deposited = min(resource_space, deposit_cargo)
    return max(0, resources + deposited - healing_reserve)


def can_afford_heal(
    *,
    resources: int,
    missing_health: int,
    reserve: int = HEAL_RESOURCE_RESERVE,
) -> bool:
    """Return whether Core can pay for a heal above the fixed resource reserve."""

    return resources >= reserve + missing_health


# --- Mechanism 2: full-capacity dispersal + spawn suspension -------------------

SPAWN_CLEAR_TICKS: Final = 3
UNIT_CELL_CAPACITY: Final = 2

HOME_PATROL_OFFSETS: Final = (
    Coordinate(3, 0),
    Coordinate(3, 3),
    Coordinate(0, 3),
    Coordinate(-3, 3),
    Coordinate(-3, 0),
    Coordinate(-3, -3),
    Coordinate(0, -3),
    Coordinate(3, -3),
)

_CARDINAL_ORDER: Final = (
    Direction.NORTH,
    Direction.EAST,
    Direction.SOUTH,
    Direction.WEST,
)

_ADJACENT_ORDER: Final = (
    Direction.EAST,
    Direction.WEST,
    Direction.SOUTH,
    Direction.NORTH,
)


@dataclass(frozen=True, slots=True)
class FullCapacityRoute:
    """Immediate step and home staging goal for one full-cargo worker."""

    next_step: Coordinate | None
    staging_goal: Coordinate


def spawn_clear_until(
    *,
    tick: int,
    previous_clear_until: int,
    cell_unit_limit: bool,
) -> int:
    """Advance the spawn-suspension deadline when ``CELL_UNIT_LIMIT`` is observed."""

    if cell_unit_limit:
        return max(previous_clear_until, tick + SPAWN_CLEAR_TICKS)
    return previous_clear_until


def spawn_suspended(*, tick: int, clear_until: int) -> bool:
    """Return whether Core spawn is still suspended at ``tick``."""

    return tick < clear_until


def full_capacity_worker_route(
    *,
    core: Coordinate,
    position: Coordinate,
    worker_index: int,
    obstacles: frozenset[Coordinate] = frozenset(),
    occupancy: Mapping[Coordinate, int] | None = None,
    cell_capacity: int = UNIT_CELL_CAPACITY,
) -> FullCapacityRoute:
    """Return the dispersal route for one full-cargo worker.

    ``obstacles`` are navigation/terrain obstacles; the Core cell is excluded and
    is always treated as impassable for traversal. ``occupancy`` maps each cell to
    its current friendly-unit count, and cells at or above ``cell_capacity`` are
    treated as impassable. A worker standing on the Core cell steps to the least
    occupied free neighbor to vacate the production cell; otherwise it steps
    toward the first non-obstacle home staging point.
    """

    counts = {} if occupancy is None else occupancy
    staging_goal = _add(core, HOME_PATROL_OFFSETS[worker_index % len(HOME_PATROL_OFFSETS)])

    if position == core:
        return FullCapacityRoute(
            next_step=_vacate_core_step(core, worker_index, obstacles, counts, cell_capacity),
            staging_goal=staging_goal,
        )

    full_cells = frozenset(cell for cell, count in counts.items() if count >= cell_capacity)
    for offset_index in range(len(HOME_PATROL_OFFSETS)):
        offset = HOME_PATROL_OFFSETS[(worker_index + offset_index) % len(HOME_PATROL_OFFSETS)]
        goal = _add(core, offset)
        if goal in obstacles or goal == core:
            continue
        staging_goal = goal
        if position == goal:
            return FullCapacityRoute(next_step=None, staging_goal=goal)
        step = _first_step_toward(position, goal, obstacles | {core} | full_cells)
        if step is not None:
            return FullCapacityRoute(next_step=step, staging_goal=goal)
    return FullCapacityRoute(next_step=None, staging_goal=staging_goal)


def _add(origin: Coordinate, offset: Coordinate) -> Coordinate:
    return Coordinate(origin.x + offset.x, origin.y + offset.y)


def _vacate_core_step(
    core: Coordinate,
    worker_index: int,
    obstacles: frozenset[Coordinate],
    occupancy: Mapping[Coordinate, int],
    cell_capacity: int,
) -> Coordinate | None:
    start = worker_index % len(_CARDINAL_ORDER)
    directions = _CARDINAL_ORDER[start:] + _CARDINAL_ORDER[:start]
    candidates = [core.step(direction) for direction in directions]
    for cell in sorted(candidates, key=lambda candidate: occupancy.get(candidate, 0)):
        if cell in obstacles:
            continue
        if occupancy.get(cell, 0) >= cell_capacity:
            continue
        return cell
    return None


def _first_step_toward(
    start: Coordinate,
    target: Coordinate,
    blocked: frozenset[Coordinate],
) -> Coordinate | None:
    grid = NavigationGrid(open_cells=frozenset(), blocked_cells=blocked)
    try:
        path = shortest_path(
            grid,
            start,
            target,
            unknown_policy=UnknownTraversalPolicy.ALLOW,
            limits=TS_COMPATIBLE_SEARCH_LIMITS,
        )
    except (UnreachableError, SearchLimitExceeded):
        return None
    if len(path) < 2:
        return None
    return path[1]


# --- Mechanism 3: stepped worker-expansion reserve ----------------------------

BASE_WORKER_TARGET: Final = 6
CORE_RESOURCE_RESERVE: Final = 10
LATE_EXPANSION_RESERVE: Final = 15


def worker_expansion_threshold(
    *,
    worker_count: int,
    worker_target: int,
    resource_capacity: int,
    population: int,
    rules_version: RulesVersion = CURRENT_RULES_VERSION,
    base_worker_target: int = BASE_WORKER_TARGET,
    core_resource_reserve: int = CORE_RESOURCE_RESERVE,
    late_expansion_reserve: int = LATE_EXPANSION_RESERVE,
) -> int:
    """Return the resource threshold for the next staged worker expansion.

    Before ``base_worker_target`` workers the threshold is the fixed Core reserve
    plus the next worker's current price, clamped to ``resource_capacity``.
    Afterward workers expand in two-worker stages: the threshold prices the
    remaining workers in the current stage at the populations they would occupy,
    then adds the late-expansion reserve. Prices use the deterministic domain
    :func:`arena_hero_agent.domain.unit_price`.
    """

    next_worker_cost = unit_price(UnitRole.WORKER, population, rules_version)
    if worker_count < base_worker_target:
        return min(core_resource_reserve + next_worker_cost, resource_capacity)

    completed_late_stages = (worker_count - base_worker_target) // 2
    stage_target = min(
        worker_target,
        base_worker_target + 2 * (completed_late_stages + 1),
    )
    remaining_stage_workers = max(1, stage_target - worker_count)
    stage_cost = sum(
        unit_price(UnitRole.WORKER, population + offset, rules_version)
        for offset in range(remaining_stage_workers)
    )
    return late_expansion_reserve + stage_cost


# --- Mechanism 4: full-cargo home return ignoring unit occupancy ---------------


@dataclass(frozen=True, slots=True)
class HomeRoute:
    """Immediate step and adjacent drop-off target for one full-cargo worker."""

    next_step: Coordinate | None
    target: Coordinate


def ignore_occupancy_home_step(
    *,
    position: Coordinate,
    core: Coordinate,
    terrain_blocked: frozenset[Coordinate] = frozenset(),
) -> HomeRoute:
    """Return the first step toward an adjacent drop-off cell ignoring occupancy.

    Unit occupancy is deliberately not an obstacle: a full-cargo worker may enter
    cells occupied by friendly units (the engine resolves the temporary overlap),
    so only terrain obstacles and the Core cell block traversal. The target is the
    nearest open cardinal neighbor of the Core.
    """

    targets: list[Coordinate] = []
    for direction in _ADJACENT_ORDER:
        cell = core.step(direction)
        if cell not in terrain_blocked:
            targets.append(cell)
    if not targets:
        return HomeRoute(next_step=None, target=core)

    target = min(targets, key=lambda candidate: manhattan(position, candidate))
    if position == target:
        return HomeRoute(next_step=None, target=target)

    step = _first_step_toward(position, target, terrain_blocked | {core})
    return HomeRoute(next_step=step, target=target)


__all__ = [
    "BASE_WORKER_TARGET",
    "CORE_RESOURCE_RESERVE",
    "HEAL_RESOURCE_RESERVE",
    "HOME_PATROL_OFFSETS",
    "LATE_EXPANSION_RESERVE",
    "SPAWN_CLEAR_TICKS",
    "UNIT_CELL_CAPACITY",
    "FullCapacityRoute",
    "HomeRoute",
    "can_afford_heal",
    "full_capacity_worker_route",
    "heal_reserve",
    "ignore_occupancy_home_step",
    "projected_core_resources",
    "spawn_clear_until",
    "spawn_suspended",
    "unit_max_health",
    "worker_expansion_threshold",
]
