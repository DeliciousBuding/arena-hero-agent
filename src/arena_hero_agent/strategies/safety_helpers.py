"""Deterministic safety/tactical helpers (legacy safety-planner-helpers).

Pure, stateless helpers for spawn choice, guard placement, retreat direction,
shot targeting, and kite cells. All functions are deterministic and operate on
planning values (``Coordinate``, cell-key sets, occupancy maps) so they can be
fixture-compared against the TypeScript oracle without any runtime state.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from arena_hero_agent.domain import (
    Coordinate,
    Direction,
    UnitRole,
    chebyshev,
    manhattan,
    shot_line_blocked,
)

from ..planning.planning_snapshot import PlanningUnit
from .safety_planner_config import SafetyPlannerConfig

VANGUARD_GUARD_RADIUS = 3
RANGER_GUARD_RADIUS = 2
RANGER_SHOOT_RANGE = 3

_THREAT_AXES = ("N", "E", "S", "W")
_AXIS_DIRECTIONS: dict[str, Coordinate] = {
    "N": Coordinate(0, -1),
    "E": Coordinate(1, 0),
    "S": Coordinate(0, 1),
    "W": Coordinate(-1, 0),
}
_RETREAT_ORDER = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)
_KITE_DELTAS = (
    (1, 0),
    (1, 1),
    (0, 1),
    (-1, 1),
    (-1, 0),
    (-1, -1),
    (0, -1),
    (1, -1),
)
_SHELTER_NEIGHBORS = ((0, -1), (1, 0), (0, 1), (-1, 0))


@dataclass(frozen=True, slots=True)
class VisibleEnemy:
    """One visible hostile entity (unit or core) in tactical helper inputs."""

    __canonical_name__ = "arena-hero.visible-enemy.v1"

    id: str
    position: Coordinate
    kind: str
    unit_role: UnitRole | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("enemy id must be a non-empty string")
        if not isinstance(self.position, Coordinate):
            raise TypeError("enemy position must be a Coordinate")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("enemy kind must be a non-empty string")
        if self.unit_role is not None and not isinstance(self.unit_role, UnitRole):
            raise TypeError("enemy unit_role must be a UnitRole or None")


def next_spawn(
    workers: int,
    vanguards: int,
    rangers: int,
    worker_target: int,
    config: SafetyPlannerConfig,
) -> UnitRole:
    """Choose the next unit to spawn: keep building Workers until the target."""

    if isinstance(workers, bool) or not isinstance(workers, int):
        raise TypeError("workers must be an integer")
    if isinstance(vanguards, bool) or not isinstance(vanguards, int):
        raise TypeError("vanguards must be an integer")
    if isinstance(rangers, bool) or not isinstance(rangers, int):
        raise TypeError("rangers must be an integer")
    if not isinstance(config, SafetyPlannerConfig):
        raise TypeError("config must be a SafetyPlannerConfig")
    if workers < 0 or vanguards < 0 or rangers < 0:
        raise ValueError("unit counts cannot be negative")
    if workers < worker_target:
        return UnitRole.WORKER
    return next_military(vanguards, rangers, config)


def next_military(
    vanguards: int,
    rangers: int,
    config: SafetyPlannerConfig,
) -> UnitRole:
    """Choose VANGUARD or RANGER, honoring an optional vanguard ratio."""

    if isinstance(vanguards, bool) or not isinstance(vanguards, int):
        raise TypeError("vanguards must be an integer")
    if isinstance(rangers, bool) or not isinstance(rangers, int):
        raise TypeError("rangers must be an integer")
    if not isinstance(config, SafetyPlannerConfig):
        raise TypeError("config must be a SafetyPlannerConfig")
    if vanguards < 0 or rangers < 0:
        raise ValueError("unit counts cannot be negative")
    ratio = config.vanguard_ratio
    if ratio is None:
        return UnitRole.VANGUARD if vanguards <= rangers else UnitRole.RANGER
    military = vanguards + rangers
    target_vanguards = math.ceil((military + 1) * ratio)
    return UnitRole.VANGUARD if vanguards < target_vanguards else UnitRole.RANGER


def _axis_of_delta(dx: int, dy: int) -> str:
    if abs(dx) >= abs(dy):
        return "E" if dx >= 0 else "W"
    return "S" if dy >= 0 else "N"


def defense_post(
    core: Coordinate,
    enemies: tuple[VisibleEnemy, ...],
    obstacles: frozenset[str],
    unit_role: UnitRole,
    index: int,
) -> Coordinate | None:
    """Pick the defense post for one guard on the nearest enemy approach axis."""

    if not isinstance(core, Coordinate):
        raise TypeError("core must be a Coordinate")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    if unit_role not in (UnitRole.VANGUARD, UnitRole.RANGER):
        raise ValueError("defense posts are only defined for VANGUARD and RANGER")
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("index must be an integer")
    if index < 0:
        raise ValueError("index cannot be negative")

    axis_min_distance: dict[str, float] = {axis: float("inf") for axis in _THREAT_AXES}
    for enemy in enemies:
        if enemy.kind == "CORE":
            continue
        axis = _axis_of_delta(enemy.position.x - core.x, enemy.position.y - core.y)
        axis_min_distance[axis] = min(
            axis_min_distance[axis], float(manhattan(core, enemy.position))
        )

    axes_with_enemies = [axis for axis in _THREAT_AXES if math.isfinite(axis_min_distance[axis])]
    axes_with_enemies.sort(key=lambda axis: (axis_min_distance[axis], _THREAT_AXES.index(axis)))
    if not axes_with_enemies:
        return None
    axis = axes_with_enemies[index % len(axes_with_enemies)]
    radius = VANGUARD_GUARD_RADIUS if unit_role is UnitRole.VANGUARD else RANGER_GUARD_RADIUS
    for r in range(radius, 0, -1):
        delta = _AXIS_DIRECTIONS[axis]
        candidate = Coordinate(core.x + delta.x * r, core.y + delta.y * r)
        if candidate.cell_key in obstacles:
            continue
        if any(enemy.position == candidate for enemy in enemies):
            continue
        return candidate
    return None


def _nearest_enemy_distance(cell: Coordinate, enemies: tuple[VisibleEnemy, ...]) -> float:
    nearest = float("inf")
    for enemy in enemies:
        nearest = min(nearest, float(manhattan(cell, enemy.position)))
    return nearest


def _direction_of(core: Coordinate, cell: Coordinate, order: tuple[Direction, ...]) -> int:
    for index, direction in enumerate(order):
        delta = direction.delta
        if cell.x == core.x + delta[0] and cell.y == core.y + delta[1]:
            return index
    return 2**31 - 1


def yield_anchor(
    core: Coordinate,
    obstacles: frozenset[str],
    occupancy: Mapping[str, int],
    enemies: tuple[VisibleEnemy, ...] = (),
) -> Coordinate | None:
    """Pick where a unit standing on the Core should yield to unblock it."""

    if not isinstance(core, Coordinate):
        raise TypeError("core must be a Coordinate")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    if not isinstance(occupancy, Mapping):
        raise TypeError("occupancy must be a Mapping")
    order = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)

    candidates: list[Coordinate] = []
    for pass_index in (0, 1):
        for direction in order:
            cell = core.step(direction)
            if cell.cell_key in obstacles:
                continue
            if occupancy.get(cell.cell_key, 0) == pass_index:
                candidates.append(cell)
        if candidates:
            break
    if not candidates:
        return None
    if not enemies:
        return candidates[0]
    candidates.sort(
        key=lambda cell: (
            -_nearest_enemy_distance(cell, enemies),
            _direction_of(core, cell, order),
        )
    )
    return candidates[0]


def occupancy_counts(
    core_position: Coordinate | None,
    units: tuple[PlanningUnit, ...],
) -> dict[str, int]:
    """Count current occupancy per cell: the Core plus every controlled unit."""

    if core_position is not None and not isinstance(core_position, Coordinate):
        raise TypeError("core_position must be a Coordinate or None")
    counts: dict[str, int] = {}
    if core_position is not None:
        counts[core_position.cell_key] = 1
    for unit in units:
        key = unit.position.cell_key
        counts[key] = counts.get(key, 0) + 1
    return counts


def home_cell(
    core: Coordinate,
    obstacles: frozenset[str],
    index: int = 0,
) -> Coordinate | None:
    """Pick the first non-obstacle neighbor of the Core, rotated by index."""

    if not isinstance(core, Coordinate):
        raise TypeError("core must be a Coordinate")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    order = (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST)
    for offset in range(len(order)):
        direction = order[(index + offset) % len(order)]
        cell = core.step(direction)
        if cell.cell_key not in obstacles:
            return cell
    return None


_RING_ORDER = (
    (-2, -2),
    (2, -2),
    (-2, 2),
    (2, 2),
    (-2, 0),
    (2, 0),
    (0, -2),
    (0, 2),
    (-3, -3),
    (3, -3),
    (-3, 3),
    (3, 3),
    (-3, 0),
    (3, 0),
    (0, -3),
    (0, 3),
    (-4, -4),
    (4, -4),
    (-4, 4),
    (4, 4),
    (-4, 0),
    (4, 0),
    (0, -4),
    (0, 4),
    (-4, -2),
    (4, -2),
    (-4, 2),
    (4, 2),
    (-2, -4),
    (2, -4),
    (-2, 4),
    (2, 4),
    (-5, -5),
    (5, -5),
    (-5, 5),
    (5, 5),
    (-5, 0),
    (5, 0),
    (0, -5),
    (0, 5),
    (-5, -2),
    (5, -2),
    (-5, 2),
    (5, 2),
    (-2, -5),
    (2, -5),
    (-2, 5),
    (2, 5),
)
_CORNER_RING_ORDER = (
    (3, 3),
    (-3, 3),
    (3, -3),
    (-3, -3),
    (4, 4),
    (-4, 4),
    (4, -4),
    (-4, -4),
    (5, 5),
    (-5, 5),
    (5, -5),
    (-5, -5),
    (3, 0),
    (0, 3),
    (-3, 0),
    (0, -3),
    (4, 0),
    (0, 4),
    (-4, 0),
    (0, -4),
    (5, 0),
    (0, 5),
    (-5, 0),
    (0, -5),
    (3, 1),
    (3, -1),
    (-3, 1),
    (-3, -1),
    (1, 3),
    (1, -3),
    (-1, 3),
    (-1, -3),
    (4, 2),
    (4, -2),
    (-4, 2),
    (-4, -2),
    (2, 4),
    (2, -4),
    (-2, 4),
    (-2, -4),
    (5, 2),
    (5, -2),
    (-5, 2),
    (-5, -2),
    (2, 5),
    (2, -5),
    (-2, 5),
    (-2, -5),
)


def guard_home_cell(
    core: Coordinate,
    obstacles: frozenset[str],
    index: int = 0,
    avoid: frozenset[str] | None = None,
    *,
    corner_spacing: bool = False,
) -> Coordinate | None:
    """Pick a guard post on the outer ring, falling back to the Core neighbors."""

    if not isinstance(core, Coordinate):
        raise TypeError("core must be a Coordinate")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    if avoid is not None and not isinstance(avoid, frozenset):
        raise TypeError("avoid must be a frozenset of cell keys or None")
    order = _CORNER_RING_ORDER if corner_spacing else _RING_ORDER
    for offset in range(len(order)):
        dx, dy = order[(index + offset) % len(order)]
        cell = Coordinate(core.x + dx, core.y + dy)
        if cell.cell_key in obstacles:
            continue
        if avoid is not None and cell.cell_key in avoid:
            continue
        return cell
    return home_cell(core, obstacles, index)


def _shelter_entrance(position: Coordinate, obstacles: frozenset[str]) -> Coordinate | None:
    neighbors: list[Coordinate] = []
    for dx, dy in _SHELTER_NEIGHBORS:
        cell = Coordinate(position.x + dx, position.y + dy)
        if cell.cell_key not in obstacles:
            neighbors.append(cell)
    return neighbors[0] if len(neighbors) == 1 else None


def core_shelter_target(
    core: Coordinate,
    obstacles: frozenset[str],
    resource_cells: frozenset[str],
    search_radius: int = 8,
) -> tuple[Coordinate, Coordinate] | None:
    """Find the nearest single-entrance shelter for a Core migration target."""

    if not isinstance(core, Coordinate):
        raise TypeError("core must be a Coordinate")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    if not isinstance(resource_cells, frozenset):
        raise TypeError("resource_cells must be a frozenset of cell keys")
    if isinstance(search_radius, bool) or not isinstance(search_radius, int):
        raise TypeError("search_radius must be an integer")
    if search_radius < 1:
        raise ValueError("search_radius must be positive")

    best: tuple[Coordinate, Coordinate, int] | None = None
    for dx in range(-search_radius, search_radius + 1):
        for dy in range(-search_radius, search_radius + 1):
            if dx == 0 and dy == 0:
                continue
            candidate = Coordinate(core.x + dx, core.y + dy)
            if candidate.cell_key in obstacles:
                continue
            if candidate.cell_key in resource_cells:
                continue
            entrance = _shelter_entrance(candidate, obstacles)
            if entrance is None:
                continue
            if entrance.cell_key in obstacles:
                continue
            if entrance.cell_key in resource_cells:
                continue
            distance = abs(dx) + abs(dy)
            if best is None or (
                distance < best[2]
                or (
                    distance == best[2]
                    and (
                        candidate.x < best[0].x
                        or (candidate.x == best[0].x and candidate.y < best[0].y)
                    )
                )
            ):
                best = (candidate, entrance, distance)
    return None if best is None else (best[0], best[1])


def is_core_shelter(
    core: Coordinate,
    obstacles: frozenset[str],
) -> Coordinate | None:
    """Return the shelter entrance when the Core cell itself is a shelter."""

    if not isinstance(core, Coordinate):
        raise TypeError("core must be a Coordinate")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    return _shelter_entrance(core, obstacles)


def nearest_enemy(
    enemies: tuple[VisibleEnemy, ...],
    position: Coordinate,
) -> VisibleEnemy | None:
    """Return the nearest enemy, tie-broken by raw id ordering."""

    if not isinstance(position, Coordinate):
        raise TypeError("position must be a Coordinate")
    if not enemies:
        return None
    return min(
        enemies,
        key=lambda enemy: (manhattan(position, enemy.position), enemy.id),
    )


def _projected_damage_at(
    target: Coordinate,
    enemy: VisibleEnemy,
    blocked_cells: frozenset[Coordinate],
) -> int:
    if enemy.kind == "CORE":
        return 0
    if enemy.unit_role is UnitRole.RANGER:
        distance = chebyshev(target, enemy.position)
        if distance == 0 or distance > RANGER_SHOOT_RANGE:
            return 0
        return 0 if shot_line_blocked(target, enemy.position, blocked_cells) else 1
    return 1 if manhattan(target, enemy.position) == 1 else 0


def _blocked_coordinates(obstacles: frozenset[str]) -> frozenset[Coordinate]:
    return frozenset(Coordinate.parse_cell_key(key) for key in obstacles)


def retreat_direction(
    core: Coordinate,
    enemies: tuple[VisibleEnemy, ...],
    obstacles: frozenset[str],
    beacon: Coordinate,
    scoring: str = "distance",
) -> Direction | None:
    """Choose a retreat direction away from enemies, with optional multi-target scoring."""

    if not isinstance(core, Coordinate):
        raise TypeError("core must be a Coordinate")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    if not isinstance(beacon, Coordinate):
        raise TypeError("beacon must be a Coordinate")
    if scoring not in ("distance", "multi"):
        raise ValueError("scoring must be 'distance' or 'multi'")

    blocked_cells = _blocked_coordinates(obstacles)
    best: Direction | None = None
    best_distance_score = float("-inf")
    best_damage = 0
    best_vector: tuple[int, ...] = ()
    best_beacon = 0
    for direction in _RETREAT_ORDER:
        destination = core.step(direction)
        if destination.cell_key in obstacles:
            continue
        if any(enemy.position == destination for enemy in enemies):
            continue
        if scoring == "distance":
            min_enemy_distance = (
                float("inf")
                if not enemies
                else float(min(manhattan(destination, enemy.position) for enemy in enemies))
            )
            score = min_enemy_distance * 1000 + manhattan(destination, beacon)
            if score > best_distance_score:
                best_distance_score = score
                best = direction
            continue
        projected_damage = sum(
            _projected_damage_at(destination, enemy, blocked_cells) for enemy in enemies
        )
        distance_vector = tuple(sorted(manhattan(destination, enemy.position) for enemy in enemies))
        beacon_distance = manhattan(destination, beacon)
        if (
            best is None
            or _compare_retreat(
                projected_damage,
                distance_vector,
                beacon_distance,
                best_damage,
                best_vector,
                best_beacon,
            )
            > 0
        ):
            best_damage = projected_damage
            best_vector = distance_vector
            best_beacon = beacon_distance
            best = direction
    return best


def _compare_retreat(
    candidate_damage: int,
    candidate_vector: tuple[int, ...],
    candidate_beacon: int,
    incumbent_damage: int,
    incumbent_vector: tuple[int, ...],
    incumbent_beacon: int,
) -> int:
    if candidate_damage != incumbent_damage:
        return incumbent_damage - candidate_damage
    length = max(len(candidate_vector), len(incumbent_vector))
    for index in range(length):
        a = candidate_vector[index] if index < len(candidate_vector) else 0
        b = incumbent_vector[index] if index < len(incumbent_vector) else 0
        if a != b:
            return a - b
    return incumbent_beacon - candidate_beacon


def _shot_target_rank(enemy: VisibleEnemy) -> int:
    if enemy.kind == "CORE":
        return 3
    if enemy.unit_role is UnitRole.WORKER:
        return 0
    if enemy.unit_role is UnitRole.RANGER:
        return 1
    return 2


def _defensive_shot_target_rank(enemy: VisibleEnemy) -> int:
    if enemy.kind == "CORE":
        return 3
    if enemy.unit_role is UnitRole.RANGER:
        return 0
    if enemy.unit_role is UnitRole.VANGUARD:
        return 1
    return 2


def aggressive_shot_priority(a: VisibleEnemy, b: VisibleEnemy) -> int:
    """Comparator: break enemy economy first, then raw id order."""

    return _shot_target_rank(a) - _shot_target_rank(b) or (
        -1 if a.id < b.id else 1 if a.id > b.id else 0
    )


def defensive_shot_priority(from_position: Coordinate, a: VisibleEnemy, b: VisibleEnemy) -> int:
    """Comparator: nearest threat first, then defensive value, then raw id order."""

    distance_a = manhattan(from_position, a.position)
    distance_b = manhattan(from_position, b.position)
    if distance_a != distance_b:
        return distance_a - distance_b
    rank_delta = _defensive_shot_target_rank(a) - _defensive_shot_target_rank(b)
    if rank_delta != 0:
        return rank_delta
    return -1 if a.id < b.id else 1 if a.id > b.id else 0


def can_shoot(
    from_position: Coordinate,
    target: Coordinate,
    obstacles: frozenset[str],
) -> bool:
    """Return whether a Ranger can fire at the target cell this tick."""

    if not isinstance(from_position, Coordinate):
        raise TypeError("from_position must be a Coordinate")
    if not isinstance(target, Coordinate):
        raise TypeError("target must be a Coordinate")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    dx = target.x - from_position.x
    dy = target.y - from_position.y
    distance = max(abs(dx), abs(dy))
    aligned = dx == 0 or dy == 0 or abs(dx) == abs(dy)
    return (
        distance >= 1
        and distance <= RANGER_SHOOT_RANGE
        and aligned
        and not shot_line_blocked(from_position, target, _blocked_coordinates(obstacles))
    )


def predicted_enemy_cell(
    actor: Coordinate,
    enemy: Coordinate,
) -> Coordinate | None:
    """Predict the enemy's next cell: one step along the dominant axis."""

    if not isinstance(actor, Coordinate):
        raise TypeError("actor must be a Coordinate")
    if not isinstance(enemy, Coordinate):
        raise TypeError("enemy must be a Coordinate")
    dx = enemy.x - actor.x
    dy = enemy.y - actor.y
    if dx == 0 and dy == 0:
        return None
    if abs(dx) >= abs(dy):
        return Coordinate(enemy.x - (1 if dx > 0 else -1), enemy.y)
    return Coordinate(enemy.x, enemy.y - (1 if dy > 0 else -1))


def kite_cell(
    from_position: Coordinate,
    threat: Coordinate,
    obstacles: frozenset[str],
    occupancy: Mapping[str, int],
    enemies: tuple[VisibleEnemy, ...],
) -> Coordinate | None:
    """Pick the best kite cell: Chebyshev 2-3 from the threat, still shootable."""

    if not isinstance(from_position, Coordinate):
        raise TypeError("from_position must be a Coordinate")
    if not isinstance(threat, Coordinate):
        raise TypeError("threat must be a Coordinate")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    if not isinstance(occupancy, Mapping):
        raise TypeError("occupancy must be a Mapping")

    best: Coordinate | None = None
    best_min_dist = float("-inf")
    for dx, dy in _KITE_DELTAS:
        candidate = Coordinate(from_position.x + dx, from_position.y + dy)
        if candidate.cell_key in obstacles:
            continue
        if any(enemy.position == candidate for enemy in enemies):
            continue
        if occupancy.get(candidate.cell_key, 0) >= 2:
            continue
        dist_to_threat = chebyshev(candidate, threat)
        if dist_to_threat < 2 or dist_to_threat > 3:
            continue
        if not can_shoot(candidate, threat, obstacles):
            continue
        min_enemy_dist = min(
            dist_to_threat,
            *(manhattan(candidate, enemy.position) for enemy in enemies),
        )
        better = min_enemy_dist > best_min_dist or (
            min_enemy_dist == best_min_dist
            and (
                best is None
                or candidate.x < best.x
                or (candidate.x == best.x and candidate.y < best.y)
            )
        )
        if better:
            best_min_dist = min_enemy_dist
            best = candidate
    return best
