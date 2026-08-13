"""Deterministic worker task assignment (legacy worker-task-planner, P4-12).

The global worker -> resource allocation layer.  Forced tasks (RP2, ``task.py``)
bypass the matrix and are assigned directly; every remaining worker is allocated
to a resource cell by a deterministic rectangular Hungarian solver over the
route-aware net-value cost model, with sticky routing (previous assignments plus
cross-tick claim leases) so assignments stay stable across ticks.

Behavior mirrors the legacy TypeScript ``WorkerTaskPlanner``
(``packages/arena-agent/src/planning/worker-task-planner.ts`` at the pinned
oracle commit) and is fixture-compared in ``tests/planning/``.  Unlike the
stateful oracle, this layer is a pure function: the cross-tick claim lease is an
explicit deterministic input/output (``claims`` -> ``WorkerAssignmentResult.
claims``) so the same inputs always produce the same outputs.

Cost model (RP2 + route-aware)::

    cost = expected_resource_value - travel_time - return_time - threat_risk
           - congestion - stale_penalty + exploration_gain + beacon_bonus
           + sticky + hysteresis + confidence + refill_bonus

Travel/return use a bounded obstacle-aware BFS distance field; cells the BFS
cannot cover fall back to Manhattan plus ``UNKNOWN_ROUTE_PENALTY`` (a degraded
path is never treated as permanently unreachable).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from arena_hero_agent.domain import Coordinate, Direction, UnitRole, manhattan

from .min_cost_assignment import minimum_cost_assignment
from .mission import (
    DEFAULT_MISSION_CONFIG,
    MissionConfig,
    is_collectable,
    refill_bonus_of,
    surveyor_ids,
    target_confidence,
)
from .plan import _safe_int
from .planning_snapshot import PlanningSnapshot, PlanningUnit
from .task import Task, TaskType, forced_task_for

DEFAULT_STICKY_BONUS: Final = 0.5
DEFAULT_CONGESTION_PENALTY: Final = 1.0

# Cost-model constants (numerically equal to the oracle; travel/return = distance * 1.0).
RESOURCE_VALUE: Final = 1.0
TRAVEL_WEIGHT: Final = 1.0
RETURN_WEIGHT: Final = 1.0
BEACON_BONUS: Final = 2.0
EXPLORATION_GAIN: Final = 0.0
UNKNOWN_ROUTE_PENALTY: Final = 8.0
MEMORY_MAX_DIRECT_DISTANCE: Final = 40
STALE_AGE_WEIGHT: Final = 0.2
STALE_MAX_PENALTY: Final = 8.0
SEEDED_PENALTY: Final = 2.0
CLAIM_BONUS: Final = 20.0
DEFAULT_CLAIM_NO_PROGRESS_TTL_TICKS: Final = 10
ASSIGNMENT_ROUTE_RADIUS: Final = 24
ASSIGNMENT_ROUTE_NODE_BUDGET: Final = 1024

_BEACON_GROUND = "ground"
# BFS expansion order matches the oracle: RIGHT, DOWN, LEFT, UP.
_PATH_DELTAS: Final = ((1, 0), (0, 1), (-1, 0), (0, -1))


def progress_decay(distance: float, norm_distance: float = 20.0) -> float:
    """Distance-proportional sticky decay: 1.0 at 0, 0.5 at norm, toward 0."""

    if isinstance(distance, bool) or not isinstance(distance, (int, float)):
        raise TypeError("distance must be a number")
    if isinstance(norm_distance, bool) or not isinstance(norm_distance, (int, float)):
        raise TypeError("norm_distance must be a number")
    return norm_distance / (norm_distance + distance)


def apply_sticky_bonus(
    unit_id: str,
    target_cell_key: str,
    previous_assignments: Sequence[Assignment],
    amount: float,
    distance: float | None = None,
) -> float:
    """Return the sticky bonus when this worker previously targeted the cell.

    The bonus is amount * progress_decay(distance) when the previous assignment
    targeted the same cell; without a distance it is the plain amount.
    """

    previous = next(
        (assignment for assignment in previous_assignments if assignment.unit_id == unit_id),
        None,
    )
    if previous is None:
        return 0.0
    previous_target = previous.task.target_cell_key
    if previous_target is None and previous.task.target is not None:
        previous_target = previous.task.target.cell_key
    if previous_target != target_cell_key:
        return 0.0
    if distance is None:
        return amount
    return amount * progress_decay(distance)


def shortest_path_distances(
    start: Coordinate,
    targets: Sequence[Coordinate],
    obstacles: frozenset[str],
    *,
    search_radius: int = ASSIGNMENT_ROUTE_RADIUS,
    node_budget: int = ASSIGNMENT_ROUTE_NODE_BUDGET,
) -> dict[str, int]:
    """Return BFS path distances to the target cells (legacy shortestPathDistances).

    Four-way BFS in the oracle's fixed (E, S, W, N) order.  Obstacle cells are
    blocked; unknown cells are traversable; the search is bounded by a Chebyshev
    radius and a node budget.  Only reached targets appear in the result, so the
    caller can degrade to Manhattan plus ``UNKNOWN_ROUTE_PENALTY``.

    This deliberately does not reuse the fail-closed domain ``NavigationGrid``:
    the oracle treats unknown cells as traversable and this exact contract feeds
    the RP2 cost model for bit-for-bit differential comparison.
    """

    if not isinstance(start, Coordinate):
        raise TypeError("start must be a Coordinate")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    search_radius = _safe_int("search_radius", search_radius, minimum=1)
    node_budget = _safe_int("node_budget", node_budget, minimum=1)

    target_keys = {target.cell_key for target in targets}
    result: dict[str, int] = {}
    if not target_keys:
        return result
    start_key = start.cell_key
    if start_key in target_keys:
        result[start_key] = 0
        if len(result) == len(target_keys):
            return result

    queue: deque[tuple[Coordinate, int]] = deque([(start, 0)])
    visited = {start_key}
    head = 0
    while head < len(queue) and head < node_budget:
        current, depth = queue[head]
        head += 1
        for dx, dy in _PATH_DELTAS:
            neighbor = Coordinate(current.x + dx, current.y + dy)
            if max(abs(neighbor.x - start.x), abs(neighbor.y - start.y)) > search_radius:
                continue
            key = neighbor.cell_key
            if key in visited or key in obstacles:
                continue
            visited.add(key)
            next_depth = depth + 1
            if key in target_keys:
                result[key] = next_depth
                if len(result) == len(target_keys):
                    return result
            queue.append((neighbor, next_depth))
    return result


_DIRECTION_FROM_DELTA: Final[dict[tuple[int, int], Direction]] = {
    (1, 0): Direction.EAST,
    (0, 1): Direction.SOUTH,
    (-1, 0): Direction.WEST,
    (0, -1): Direction.NORTH,
}


def next_step_toward(
    start: Coordinate,
    target: Coordinate,
    obstacles: frozenset[str],
    *,
    search_radius: int = ASSIGNMENT_ROUTE_RADIUS,
    node_budget: int = ASSIGNMENT_ROUTE_NODE_BUDGET,
) -> Direction | None:
    """Return the first cardinal step of a BFS route from ``start`` to ``target``.

    Unknown cells are traversable and obstacle cells are blocked, matching
    :func:`shortest_path_distances`. The search is Chebyshev-bounded and
    node-budget-bounded; ``None`` means no route was found within the budget.
    This gives the explore/collect conversions an obstacle-aware first step so a
    scout does not repeatedly push into a wall on a fixed greedy axis.
    """

    if not isinstance(start, Coordinate) or not isinstance(target, Coordinate):
        raise TypeError("start and target must be Coordinate values")
    if not isinstance(obstacles, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys")
    search_radius = _safe_int("search_radius", search_radius, minimum=1)
    node_budget = _safe_int("node_budget", node_budget, minimum=1)

    if start.cell_key == target.cell_key:
        return None
    if target.cell_key in obstacles:
        return None

    prev: dict[str, str] = {start.cell_key: ""}
    queue: deque[str] = deque([start.cell_key])
    head = 0
    found: str | None = None
    while head < len(queue) and head < node_budget:
        key = queue[head]
        head += 1
        x, y = _parse_key_coords(key)
        for dx, dy in _PATH_DELTAS:
            neighbor = Coordinate(x + dx, y + dy)
            if max(abs(neighbor.x - start.x), abs(neighbor.y - start.y)) > search_radius:
                continue
            nkey = neighbor.cell_key
            if nkey in prev or nkey in obstacles:
                continue
            prev[nkey] = key
            if nkey == target.cell_key:
                found = nkey
                queue.clear()
                break
            queue.append(nkey)
        if found is not None:
            break

    if found is None:
        return None
    # walk the parent chain back to the first step after start
    step_key = found
    while prev[step_key] != start.cell_key:
        step_key = prev[step_key]
    sx, sy = _parse_key_coords(step_key)
    return _DIRECTION_FROM_DELTA[(sx - start.x, sy - start.y)]


def _parse_key_coords(key: str) -> tuple[int, int]:
    """Parse a canonical ``x,y`` cell key without importing the parser module."""

    if not isinstance(key, str):
        raise TypeError("cell key must be a string")
    x_str, y_str = key.split(",", 1)
    return int(x_str), int(y_str)


@dataclass(frozen=True, slots=True)
class WorkerClaim:
    """Cross-tick GO_RESOURCE claim lease (legacy WorkerClaim)."""

    __canonical_name__ = "arena-hero.worker-claim.v1"

    unit_id: str
    cell_key: str
    claim_tick: int
    last_progress_tick: int
    progress_distance: int
    last_position: Coordinate

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, str) or not self.unit_id:
            raise ValueError("claim unit_id must be a non-empty string")
        if not isinstance(self.cell_key, str) or not self.cell_key:
            raise ValueError("claim cell_key must be a non-empty string")
        _safe_int("claim claim_tick", self.claim_tick, minimum=1)
        _safe_int("claim last_progress_tick", self.last_progress_tick, minimum=1)
        _safe_int("claim progress_distance", self.progress_distance)
        if not isinstance(self.last_position, Coordinate):
            raise TypeError("claim last_position must be a Coordinate")


@dataclass(frozen=True, slots=True)
class Assignment:
    """One worker -> task assignment (legacy Assignment)."""

    __canonical_name__ = "arena-hero.worker-assignment.v1"

    unit_id: str
    task: Task

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, str) or not self.unit_id:
            raise ValueError("assignment unit_id must be a non-empty string")
        if not isinstance(self.task, Task):
            raise TypeError("assignment task must be a Task")


@dataclass(frozen=True, slots=True)
class WorkerTaskPlan:
    """All worker assignments for one tick (legacy WorkerTaskPlan)."""

    __canonical_name__ = "arena-hero.worker-task-plan.v1"

    assignments: tuple[Assignment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.assignments, tuple) or any(
            not isinstance(assignment, Assignment) for assignment in self.assignments
        ):
            raise TypeError("assignments must be a tuple of Assignment")


@dataclass(frozen=True, slots=True)
class WorkerTaskPlannerConfig:
    """Immutable worker-task-planner thresholds; defaults reproduce the oracle."""

    __canonical_name__ = "arena-hero.worker-task-planner-config.v1"

    sticky_bonus: float = DEFAULT_STICKY_BONUS
    congestion_penalty: float = DEFAULT_CONGESTION_PENALTY
    mission: MissionConfig = DEFAULT_MISSION_CONFIG
    claim_no_progress_ttl_ticks: int = DEFAULT_CLAIM_NO_PROGRESS_TTL_TICKS

    def __post_init__(self) -> None:
        for name, value in (
            ("sticky_bonus", self.sticky_bonus),
            ("congestion_penalty", self.congestion_penalty),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
        if not isinstance(self.mission, MissionConfig):
            raise TypeError("mission must be a MissionConfig")
        _safe_int(
            "claim_no_progress_ttl_ticks",
            self.claim_no_progress_ttl_ticks,
        )


@dataclass(frozen=True, slots=True)
class WorkerAssignmentResult:
    """Deterministic assignment plus the next cross-tick claim lease state."""

    __canonical_name__ = "arena-hero.worker-assignment-result.v1"

    plan: WorkerTaskPlan
    claims: frozenset[WorkerClaim]

    def __post_init__(self) -> None:
        if not isinstance(self.plan, WorkerTaskPlan):
            raise TypeError("plan must be a WorkerTaskPlan")
        if not isinstance(self.claims, frozenset) or any(
            not isinstance(claim, WorkerClaim) for claim in self.claims
        ):
            raise TypeError("claims must be a frozenset of WorkerClaim")


DEFAULT_WORKER_TASK_PLANNER_CONFIG: Final = WorkerTaskPlannerConfig()


def _explore_task(
    unit_id: str,
    exploration_targets: Mapping[str, Coordinate] | None,
) -> Task:
    """Build an EXPLORE task, carrying the supplied target when available."""

    target = None if exploration_targets is None else exploration_targets.get(unit_id)
    if target is not None:
        return Task(type=TaskType.EXPLORE, target=target)
    return Task(type=TaskType.EXPLORE)


def assign_worker_tasks(
    snapshot: PlanningSnapshot,
    previous_assignments: Sequence[Assignment] = (),
    *,
    config: WorkerTaskPlannerConfig = DEFAULT_WORKER_TASK_PLANNER_CONFIG,
    survey_burst_active: bool = False,
    claims: frozenset[WorkerClaim] = frozenset(),
    blocked_cells: frozenset[str] = frozenset(),
    refill_predictions: Mapping[str, int] | None = None,
    exploration_targets: Mapping[str, Coordinate] | None = None,
) -> WorkerAssignmentResult:
    """Deterministically assign tasks to every worker for one observed tick.

    Forced tasks (DEPOSIT / HARVEST_CURRENT / PICKUP_BEACON / RETURN_FOR_HEAL)
    are assigned directly and claim their cells; remaining workers are solved
    against the available resource cells with the rectangular Hungarian cost
    matrix.  Workers that reach no real task are routed to EXPLORE (surveyor
    role arbitration) or WAIT.  Cross-tick claim leases are pruned against the
    current facts and returned as the next state.  ``blocked_cells`` are
    excluded from the matrix (research stuck-guard reassignment hook).
    ``exploration_targets`` optionally supplies ring-quota explore destinations
    for the surveyor workers (research exploration-v2 switch); when ``None`` the
    legacy fixed-direction EXPLORE task is emitted unchanged.
    """

    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot must be a PlanningSnapshot")
    if not isinstance(config, WorkerTaskPlannerConfig):
        raise TypeError("config must be a WorkerTaskPlannerConfig")
    if not isinstance(survey_burst_active, bool):
        raise TypeError("survey_burst_active must be a boolean")
    if not isinstance(claims, frozenset):
        raise TypeError("claims must be a frozenset of WorkerClaim")
    if not isinstance(blocked_cells, frozenset) or any(
        not isinstance(key, str) for key in blocked_cells
    ):
        raise TypeError("blocked_cells must be a frozenset of cell key strings")
    if refill_predictions is not None and not isinstance(refill_predictions, Mapping):
        raise TypeError("refill_predictions must be a Mapping or None")
    if exploration_targets is not None and not isinstance(exploration_targets, Mapping):
        raise TypeError("exploration_targets must be a Mapping or None")

    workers = [unit for unit in snapshot.units if unit.unit_role is UnitRole.WORKER]
    assignments: list[Assignment] = []
    claimed_cells: set[str] = set()
    for worker in workers:
        forced = forced_task_for(worker, snapshot)
        if forced is not None:
            assignments.append(Assignment(unit_id=worker.id.value, task=forced))
            if forced.target_cell_key is not None:
                claimed_cells.add(forced.target_cell_key)

    claims_by_cell = {claim.cell_key: claim for claim in claims}
    pruned = _prune_claims(
        claims_by_cell,
        workers,
        frozenset(assignment.unit_id for assignment in assignments),
        snapshot,
        previous_assignments,
        config,
    )

    unassigned = [
        worker
        for worker in workers
        if worker.id.value not in {assignment.unit_id for assignment in assignments}
    ]
    occupied_by_worker = {unit.position.cell_key for unit in workers}
    available_cells = sorted(
        key
        for key in snapshot.resource_cells
        if key not in claimed_cells
        and key not in blocked_cells
        and not (snapshot.resource_cells[key].visible is False and key in occupied_by_worker)
    )
    pool = sorted(unassigned, key=lambda unit: unit.id.value)

    # Surveyor pre-reserve during a post-migration survey burst: the first cap
    # workers (by id) become EXPLORE before the matrix so the floor guarantee
    # cannot be stolen by abundant mines.
    surveyors = surveyor_ids(tuple(pool), config.mission, survey_burst_active=survey_burst_active)
    if survey_burst_active and surveyors:
        remaining: list[PlanningUnit] = []
        for worker in pool:
            if worker.id.value in surveyors:
                assignments.append(
                    Assignment(unit_id=worker.id.value, task=Task(type=TaskType.EXPLORE))
                )
            else:
                remaining.append(worker)
        pool = remaining

    pool_ids = {unit.id.value for unit in pool}
    reserved_for = {
        key: claim.unit_id for key, claim in pruned.items() if claim.unit_id in pool_ids
    }

    if pool:
        target_positions = [snapshot.resource_cells[key].position for key in available_cells]
        routing_obstacles = snapshot.obstacle_cells | snapshot.enemy_cells
        travel_fields: dict[str, dict[str, int]] = {}
        if routing_obstacles:
            for worker in pool:
                travel_fields[worker.id.value] = shortest_path_distances(
                    worker.position,
                    target_positions,
                    routing_obstacles,
                    search_radius=ASSIGNMENT_ROUTE_RADIUS,
                    node_budget=ASSIGNMENT_ROUTE_NODE_BUDGET,
                )
        return_field: dict[str, int] = {}
        if snapshot.core_position is not None and routing_obstacles:
            return_field = shortest_path_distances(
                snapshot.core_position,
                target_positions,
                routing_obstacles,
                search_radius=ASSIGNMENT_ROUTE_RADIUS,
                node_budget=ASSIGNMENT_ROUTE_NODE_BUDGET,
            )

        real_net_values = [
            [
                _net_value(
                    worker,
                    key,
                    snapshot,
                    previous_assignments,
                    frozenset(claimed_cells),
                    config,
                    travel_fields.get(worker.id.value, {}).get(key),
                    return_field.get(key),
                    bool(routing_obstacles),
                    refill_predictions,
                )
                for key in available_cells
            ]
            for worker in pool
        ]
        finite_costs = [-net for row in real_net_values for net in row if math.isfinite(net)]
        max_real = max(finite_costs) if finite_costs else 0.0
        wait_cost = max_real + 1_000_000.0
        forbidden_cost = wait_cost + 1_000_000.0

        # Matrix: rows = workers, columns = resource cells plus one dummy WAIT
        # column per worker (rows <= columns).  Real tasks always beat WAIT and
        # WAIT always beats explicitly forbidden tasks via the sentinel ordering.
        matrix: list[list[float]] = []
        for row_index, row in enumerate(real_net_values):
            worker = pool[row_index]
            matrix_row: list[float] = []
            for col_index, net in enumerate(row):
                key = available_cells[col_index]
                if not math.isfinite(net):
                    matrix_row.append(forbidden_cost)
                    continue
                reserved_worker_id = reserved_for.get(key)
                if reserved_worker_id is not None and worker.id.value != reserved_worker_id:
                    matrix_row.append(forbidden_cost)
                    continue
                cell = snapshot.resource_cells[key]
                if not is_collectable(
                    net,
                    worker,
                    cell.position.x,
                    cell.position.y,
                    config.mission,
                    refill_predictions,
                    visible=cell.visible,
                ):
                    matrix_row.append(forbidden_cost)
                    continue
                lease_bonus = CLAIM_BONUS if reserved_worker_id == worker.id.value else 0.0
                matrix_row.append(-(net + lease_bonus))
            matrix_row.extend([wait_cost] * len(pool))
            matrix.append(matrix_row)

        columns = minimum_cost_assignment(matrix)
        real_targets: dict[str, str] = {}
        dummy_workers: list[PlanningUnit] = []
        for row_index, worker in enumerate(pool):
            column = columns[row_index]
            if column < len(available_cells):
                real_targets[worker.id.value] = available_cells[column]
            else:
                dummy_workers.append(worker)

        # Role arbitration for workers that reached no real task: the first cap
        # (by id) become EXPLORE surveyors; the rest WAIT at home.
        leftover_surveyors: frozenset[str] = (
            frozenset()
            if survey_burst_active
            else surveyor_ids(tuple(dummy_workers), config.mission, survey_burst_active=False)
        )
        for worker in pool:
            key = real_targets.get(worker.id.value)
            if key is not None:
                cell = snapshot.resource_cells[key]
                assignments.append(
                    Assignment(
                        unit_id=worker.id.value,
                        task=Task(
                            type=TaskType.GO_RESOURCE,
                            target=cell.position,
                            target_cell_key=key,
                        ),
                    )
                )
            else:
                task = (
                    _explore_task(worker.id.value, exploration_targets)
                    if worker.id.value in leftover_surveyors
                    else Task(type=TaskType.WAIT)
                )
                assignments.append(Assignment(unit_id=worker.id.value, task=task))
    elif pool:
        # No candidate cells at all: the whole pool is idle; role arbitration
        # sends the first cap (by id) out exploring and keeps the rest at home.
        leftover_surveyors = (
            frozenset()
            if survey_burst_active
            else surveyor_ids(tuple(pool), config.mission, survey_burst_active=False)
        )
        for worker in pool:
            task = (
                _explore_task(worker.id.value, exploration_targets)
                if worker.id.value in leftover_surveyors
                else Task(type=TaskType.WAIT)
            )
            assignments.append(Assignment(unit_id=worker.id.value, task=task))

    claims_next = _update_claims(
        assignments,
        {unit.id.value: unit for unit in workers},
        snapshot.tick,
        claims_by_cell,
    )
    ordered = tuple(sorted(assignments, key=lambda assignment: assignment.unit_id))
    return WorkerAssignmentResult(
        plan=WorkerTaskPlan(assignments=ordered),
        claims=claims_next,
    )


def _net_value(
    worker: PlanningUnit,
    key: str,
    snapshot: PlanningSnapshot,
    previous_assignments: Sequence[Assignment],
    claimed_cells: frozenset[str],
    config: WorkerTaskPlannerConfig,
    routed_travel_distance: float | None,
    routed_return_distance: float | None,
    has_routing_obstacles: bool,
    refill_predictions: Mapping[str, int] | None,
) -> float:
    """Net value of assigning one worker to one resource cell (higher is better)."""

    cell = snapshot.resource_cells.get(key)
    if cell is None:
        return float("-inf")
    direct_travel = manhattan(worker.position, cell.position)
    # Long-haul memory cells are empty runs: exclude from the matrix entirely.
    if cell.visible is False and direct_travel > MEMORY_MAX_DIRECT_DISTANCE:
        return float("-inf")
    # A cell under an enemy is not harvestable this tick.
    if key in snapshot.enemy_cells:
        return float("-inf")

    travel_distance = (
        routed_travel_distance
        if routed_travel_distance is not None
        else direct_travel + (UNKNOWN_ROUTE_PENALTY if has_routing_obstacles else 0)
    )
    direct_return = (
        0 if snapshot.core_position is None else manhattan(cell.position, snapshot.core_position)
    )
    if snapshot.core_position is None:
        return_distance = 0
    elif routed_return_distance is not None:
        return_distance = routed_return_distance
    else:
        return_distance = direct_return + (UNKNOWN_ROUTE_PENALTY if has_routing_obstacles else 0)

    travel_time = TRAVEL_WEIGHT * travel_distance
    return_time = RETURN_WEIGHT * return_distance
    threat_risk = snapshot.threat_map.get(key, 0.0)
    congestion = config.congestion_penalty if key in claimed_cells else 0.0
    exploration_gain = EXPLORATION_GAIN

    age = (
        max(
            0,
            snapshot.tick
            - (cell.last_seen_tick if cell.last_seen_tick is not None else snapshot.tick),
        )
        if cell.visible is False
        else 0
    )
    stale_penalty = (
        min(STALE_MAX_PENALTY, age * STALE_AGE_WEIGHT) + (SEEDED_PENALTY if cell.seeded else 0)
        if cell.visible is False
        else 0
    )
    beacon_bonus = (
        BEACON_BONUS
        if snapshot.beacon.status == _BEACON_GROUND and snapshot.beacon.position == cell.position
        else 0
    )
    sticky = apply_sticky_bonus(
        worker.id.value,
        key,
        previous_assignments,
        config.sticky_bonus,
        travel_distance,
    )
    hysteresis = apply_sticky_bonus(
        worker.id.value,
        key,
        previous_assignments,
        config.mission.switch_threshold,
        travel_distance,
    )
    confidence = target_confidence(
        {
            "visible": cell.visible,
            "last_seen_tick": cell.last_seen_tick,
            "seeded": cell.seeded,
        },
        snapshot.tick,
        config.mission,
    )
    refill_bonus = refill_bonus_of(key, refill_predictions, config.mission)
    return (
        RESOURCE_VALUE
        + confidence
        + refill_bonus
        - travel_time
        - return_time
        - threat_risk
        - congestion
        - stale_penalty
        + exploration_gain
        + beacon_bonus
        + sticky
        + hysteresis
    )


def _prune_claims(
    claims: Mapping[str, WorkerClaim],
    workers: Sequence[PlanningUnit],
    forced_ids: frozenset[str],
    snapshot: PlanningSnapshot,
    previous_assignments: Sequence[Assignment],
    config: WorkerTaskPlannerConfig,
) -> dict[str, WorkerClaim]:
    """Release cross-tick claims whose facts no longer hold (fail-open)."""

    if not claims:
        return {}
    tick = snapshot.tick
    ttl = config.claim_no_progress_ttl_ticks
    worker_by_id = {unit.id.value: unit for unit in workers}
    previous_target_by_worker: dict[str, str] = {}
    for assignment in previous_assignments:
        if assignment.task.type is not TaskType.GO_RESOURCE:
            continue
        target_key = assignment.task.target_cell_key
        if target_key is None and assignment.task.target is not None:
            target_key = assignment.task.target.cell_key
        if target_key is not None:
            previous_target_by_worker[assignment.unit_id] = target_key

    pruned: dict[str, WorkerClaim] = {}
    for key, claim in claims.items():
        worker = worker_by_id.get(claim.unit_id)
        if worker is None:
            continue
        if claim.unit_id in forced_ids:
            continue
        if previous_target_by_worker.get(claim.unit_id) != key:
            continue
        cell = snapshot.resource_cells.get(key)
        if cell is None:
            continue
        if key in snapshot.enemy_cells:
            continue
        if tick < claim.last_progress_tick:
            continue
        if tick - claim.last_progress_tick >= ttl:
            continue
        if manhattan(worker.position, cell.position) > config.mission.max_collection_distance:
            continue
        # Freeze-fix semantics: an invisible cell under the claimant holds no
        # mine, so the lease cannot keep locking it.
        if cell.visible is False and worker.position == cell.position:
            continue
        pruned[key] = claim
    return pruned


def _update_claims(
    assignments: Sequence[Assignment],
    workers_by_id: Mapping[str, PlanningUnit],
    tick: int,
    previous: Mapping[str, WorkerClaim],
) -> frozenset[WorkerClaim]:
    """Renew GO_RESOURCE claims with strict-progress tracking; release others."""

    next_claims: dict[str, WorkerClaim] = {}
    for assignment in assignments:
        if (
            assignment.task.type is not TaskType.GO_RESOURCE
            or assignment.task.target_cell_key is None
        ):
            continue
        worker = workers_by_id.get(assignment.unit_id)
        if worker is None:
            continue
        target = assignment.task.target
        existing = previous.get(assignment.task.target_cell_key)
        current_distance = None if target is None else manhattan(worker.position, target)
        if existing is not None and existing.unit_id == assignment.unit_id:
            progressed = (
                current_distance is not None and current_distance < existing.progress_distance
            )
            next_claims[assignment.task.target_cell_key] = WorkerClaim(
                unit_id=assignment.unit_id,
                cell_key=assignment.task.target_cell_key,
                claim_tick=existing.claim_tick,
                last_progress_tick=tick if progressed else existing.last_progress_tick,
                progress_distance=current_distance if progressed else existing.progress_distance,
                last_position=worker.position,
            )
        else:
            next_claims[assignment.task.target_cell_key] = WorkerClaim(
                unit_id=assignment.unit_id,
                cell_key=assignment.task.target_cell_key,
                claim_tick=tick,
                last_progress_tick=tick,
                progress_distance=0 if current_distance is None else current_distance,
                last_position=worker.position,
            )
    return frozenset(next_claims.values())
