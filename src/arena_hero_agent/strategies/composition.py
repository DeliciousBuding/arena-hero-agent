"""Composed live decider: world projection + safety planner + worker assignment.

P4-21 wires the already-tested deterministic components into one
:class:`Decider` usable by the live CLI entrypoint:

- :func:`snapshot_from_turn` projects one application ``TurnObservation`` into
  a deterministic ``PlanningSnapshot`` (P4-11 extraction with a derived
  economy state; the turn carries no seed, so a fixed zero seed is used and
  nothing downstream reads it);
- :class:`ComposedDecider` runs the P4-11 ``SafetyPlanner`` baseline first,
  then the P4-12 ``assign_worker_tasks`` layer and lets worker assignments
  override the baseline worker actions, mirroring the oracle's
  ``DeterministicPlanner`` order (fallback plan, then WorkerTaskPlanner as the
  resource-task SSOT);
- :func:`plan_to_decision` converts the planning ``Plan`` into the application
  ``Decision`` DTO that the SDK plan builder consumes.

Cross-tick claim leases and previous assignments are explicit deterministic
state on the decider (the same pure-function contract as the P4-12 layer), so
identical tick sequences produce identical decisions.

Composition behavior (worker forced-task conversion, GO_RESOURCE/EXPLORE
conversion, plan-to-decision mapping) is registered as
``planner_composition`` ALLOWED_DIFFERENCE in ``docs/planning-differences.md``:
every helper it calls is fixture-compared against the legacy TypeScript oracle
at 8cf5cbb.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Final

from arena_hero_agent.application import (
    CoreAction as ApplicationCoreAction,
)
from arena_hero_agent.application import (
    CoreIntent,
    Decision,
    TurnObservation,
    UnitIntent,
)
from arena_hero_agent.application import (
    UnitAction as ApplicationUnitAction,
)
from arena_hero_agent.application.tick_loop import DeadlineBudget, Decider
from arena_hero_agent.domain import (
    Coordinate,
    Direction,
    EconomyState,
    EconomyTurnInput,
    UnitRole,
    cell_key,
    manhattan,
    parse_cell_key,
    unit_price,
)
from arena_hero_agent.planning import (
    EXPLORATION_SURVEY_CAP,
    Assignment,
    CoreActionType,
    ExplorationState,
    MoveFailureEvent,
    Plan,
    PlanningSnapshot,
    PlanningUnit,
    TaskType,
    UnitActionType,
    WorkerClaim,
    WorkerTaskPlannerConfig,
    assign_worker_tasks,
    build_exploration_targets,
    extract_planning_snapshot,
    is_hungry,
    mark_reached,
    observe_exploration,
    with_memory_resource_cells,
)
from arena_hero_agent.planning import (
    CoreAction as PlanningCoreAction,
)
from arena_hero_agent.planning import (
    UnitAction as PlanningUnitAction,
)

from .astar_pathfinder import astar_next_step
from .economy_budget import (
    heal_reserve,
    projected_core_resources,
    unit_max_health,
    worker_expansion_threshold,
)
from .movement_guard import (
    DEFAULT_CARGO_CORE_DISTANCE,
    DEFAULT_CARGO_SPIN_BUDGET,
    DEFAULT_CARGO_SPIN_TICKS,
    DEFAULT_DEPOSIT_REPATH_STREAK,
    DEFAULT_DEPOSIT_STALL_TICKS,
    DEFAULT_LOOP_MIN_UNIQUE,
    DEFAULT_LOOP_WINDOW,
    DepositProgress,
    LoopTrail,
    MoveBackoffState,
    cargo_spin_self_heal,
    deposit_escape_needed,
    detect_spatial_loop,
    forced_escape_step,
    mark_loop_repath,
    observe_loop_position,
    record_deposit_repath,
    refresh_deposit_progress,
    should_pause_move,
    soft_obstacles_from_trail,
    update_move_backoff,
)
from .raid_quota import (
    RAID_MAX_DISTANCE,
    RAID_MIN_FIGHTERS,
    RAID_MIN_OBSERVATIONS,
    RaidState,
    ReplacementQueue,
    StationaryCore,
    StrikeGroup,
    acquire_raid_target,
    clear_raid_target,
    core_assault_quota,
    pick_raid_target,
    raid_active,
    raid_fighters_ready,
    raid_guard_ids,
    recall_raid,
    reconcile_replacement_queue,
    select_strike_group,
)
from .respawn_recovery import (
    DEFAULT_BARREN_MIGRATION_FAIL_LIMIT,
    DEFAULT_BARREN_MIGRATION_TICKS,
    DEFAULT_DETECTION_DISTANCE,
    DEFAULT_RECOVERY_WORKERS,
    DEFAULT_STUCK_RESOURCES_TICKS,
    BarrenMigrationState,
    RespawnRecoveryState,
    StuckWithResourcesState,
    detect_respawn,
    has_local_yield,
    migration_direction_toward_origin,
)
from .safety_helpers import can_shoot
from .safety_planner import SafetyPlanner, step_toward, worker_dense_direction
from .safety_planner_config import DEFAULT_SAFETY_CONFIG, SafetyPlannerConfig
from .stuck_guard import (
    DEFAULT_STUCK_GUARD_RADIUS,
    DEFAULT_STUCK_GUARD_TICKS,
    detect_stuck_unit_ids,
)
from .tactical_squads import reconcile_tactical_squads
from .terrain_map import TerrainMap
from .variant_registry import apply_variant_overrides

# Legacy TS DENSE_DELTAS table (safety-planner.ts): 16 dense scan slots. The
# live worker patrol maps a slot to one cardinal step on the dominant axis.
_DENSE_DELTAS: Final[tuple[tuple[int, int], ...]] = (
    (1, 0),
    (2, 1),
    (1, 1),
    (1, 2),
    (0, 1),
    (-1, 2),
    (-1, 1),
    (-2, 1),
    (-1, 0),
    (-2, -1),
    (-1, -1),
    (-1, -2),
    (0, -1),
    (1, -2),
    (1, -1),
    (2, -1),
)

_UNIT_ACTION_TYPES: Final[dict[UnitActionType, ApplicationUnitAction]] = {
    UnitActionType.WAIT: ApplicationUnitAction.WAIT,
    UnitActionType.MOVE: ApplicationUnitAction.MOVE,
    UnitActionType.HARVEST: ApplicationUnitAction.HARVEST,
    UnitActionType.DEPOSIT: ApplicationUnitAction.DEPOSIT,
    UnitActionType.SWEEP: ApplicationUnitAction.SWEEP,
    UnitActionType.SHOOT: ApplicationUnitAction.SHOOT,
    UnitActionType.PICKUP_BEACON: ApplicationUnitAction.PICKUP_BEACON,
    UnitActionType.DROP_BEACON: ApplicationUnitAction.DROP_BEACON,
    UnitActionType.SELF_DESTRUCT: ApplicationUnitAction.SELF_DESTRUCT,
    UnitActionType.HEAL: ApplicationUnitAction.HEAL,
}

_CORE_ACTION_TYPES: Final[dict[CoreActionType, ApplicationCoreAction]] = {
    CoreActionType.WAIT: ApplicationCoreAction.WAIT,
    CoreActionType.SPAWN: ApplicationCoreAction.SPAWN,
    CoreActionType.REPAIR_SHIELD: ApplicationCoreAction.REPAIR_SHIELD,
    CoreActionType.HEAL: ApplicationCoreAction.HEAL,
    CoreActionType.START_MOVE: ApplicationCoreAction.START_MOVE,
    CoreActionType.CANCEL_MOVE: ApplicationCoreAction.CANCEL_MOVE,
    CoreActionType.PICKUP_BEACON: ApplicationCoreAction.PICKUP_BEACON,
    CoreActionType.DROP_BEACON: ApplicationCoreAction.DROP_BEACON,
    CoreActionType.SELF_DESTRUCT: ApplicationCoreAction.SELF_DESTRUCT,
}


EXPANSION_SURVEY_CAP: Final = 3
EXPANSION_EARLY_RESERVE: Final = 0
ESCAPE_STICKY_TICKS: Final = 5
DEFAULT_BARREN_RESOURCE_DISTANCE: Final = 40
DEFAULT_NO_WORKER_DEADLOCK_TICKS: Final = 12
# A cargo-carrying worker this close (manhattan, worker -> Core) is about to
# deposit; holding the next START_MOVE lets the deposit land before migration
# resumes (the engine rejects deposits while the Core is migrating).
DEPOSIT_HOLD_RADIUS: Final = 1
# When recovering from a respawn and an enemy is this close, prefer stepping
# away from it over stepping toward origin (respawn-war-zone survival).
ENEMY_FLEE_RADIUS: Final = 25
# Hard cap on worker -> cell Manhattan distance for the collection matrix,
# aligned with the barren-migration threshold (Core -> resource). Visible
# cells beyond this fall out of the matrix and the worker explores instead of
# trekking 40+ tiles (production: workers pushed 96-156 tiles toward a single
# remembered cell while the Core had already migrated).
COLLECTION_MAX_DISTANCE: Final = 40.0
# Idle workers farther than this from the Core are recalled home (stranded
# survivors of old migrations; production t3 workers idled 45-89 tiles away
# harvesting nothing). Aligned with COLLECTION_MAX_DISTANCE.
STRANDED_RECALL_DISTANCE: Final = 40
# Recall routing budget: the plain assignment BFS (64 radius / 16k nodes)
# tops out at ~64 Chebyshev tiles, which silently froze workers stranded
# farther away (production t3: 111 tiles, hundreds of ticks, replay-verified).
# A* with a 192-tile radius covers any production-world recall distance.
RECALL_ROUTE_RADIUS: Final = 192
RECALL_ROUTE_NODE_BUDGET: Final = 131072
# Extra cost of switching a worker to a different target cell (production
# hysteresis; the pure assignment layer defaults to 0.0).
HYSTERESIS_SWITCH_THRESHOLD: Final = 0.5
# Terrain-trap self-destruct confirmation: a worker must occupy the Core cell
# for this many consecutive ticks before the trap hook destroys it.
TERRAIN_TRAP_CONFIRM_TICKS: Final = 3
# Claim softening: a non-claimant pays this to preempt a reserved cell
# (injected into the production assignment matrix; the pure layer defaults to
# 0.0 which reproduces the oracle's hard exclusion).
CLAIM_PREEMPT_PENALTY: Final = 6.0
# Beacon S4 contest gate (production composition): below these economy
# thresholds the military keeps guarding instead of walking to a ground
# Beacon. The pure safety layer defaults to 0 (no gate).
BEACON_CONTEST_MIN_POPULATION: Final = 6
BEACON_CONTEST_MIN_RESOURCES: Final = 10


@dataclass(frozen=True, slots=True)
class ComposedDeciderConfig:
    """Immutable inputs for the composed live decider.

    ``variants`` are registered safety variant ids merged over
    ``safety_config`` (P4-13); unknown or unmigrated ids fail fast at
    construction.
    """

    safety_config: SafetyPlannerConfig = DEFAULT_SAFETY_CONFIG
    variants: tuple[str, ...] = ()
    worker_config: WorkerTaskPlannerConfig = WorkerTaskPlannerConfig()
    survey_burst_active: bool = True
    stuck_guard_enabled: bool = True
    stuck_guard_ticks: int = DEFAULT_STUCK_GUARD_TICKS
    stuck_guard_radius: int = DEFAULT_STUCK_GUARD_RADIUS
    movement_guard_enabled: bool = True
    movement_loop_window: int = DEFAULT_LOOP_WINDOW
    movement_loop_min_unique: int = DEFAULT_LOOP_MIN_UNIQUE
    movement_deposit_stall_ticks: int = DEFAULT_DEPOSIT_STALL_TICKS
    movement_deposit_repath_streak: int = DEFAULT_DEPOSIT_REPATH_STREAK
    movement_cargo_spin_ticks: int = DEFAULT_CARGO_SPIN_TICKS
    movement_cargo_spin_budget: int = DEFAULT_CARGO_SPIN_BUDGET
    movement_cargo_core_distance: int = DEFAULT_CARGO_CORE_DISTANCE
    economy_budget_enabled: bool = True
    economy_expansion_enabled: bool = True
    raid_quota_enabled: bool = True
    exploration_v2_enabled: bool = True
    respawn_recovery_enabled: bool = True
    respawn_worker_target: int = DEFAULT_RECOVERY_WORKERS
    respawn_detection_distance: int = DEFAULT_DETECTION_DISTANCE
    raid_min_observations: int = RAID_MIN_OBSERVATIONS
    raid_max_distance: int = RAID_MAX_DISTANCE
    raid_min_fighters: int = RAID_MIN_FIGHTERS
    barren_migration_enabled: bool = True
    barren_migration_ticks: int = DEFAULT_BARREN_MIGRATION_TICKS
    barren_resource_distance: int = DEFAULT_BARREN_RESOURCE_DISTANCE
    stuck_resources_enabled: bool = True
    stuck_resources_ticks: int = DEFAULT_STUCK_RESOURCES_TICKS

    def __post_init__(self) -> None:
        if not isinstance(self.safety_config, SafetyPlannerConfig):
            raise TypeError("safety_config must be a SafetyPlannerConfig")
        if isinstance(self.variants, str) or not isinstance(self.variants, tuple):
            raise TypeError("variants must be a tuple of strings")
        if any(not isinstance(variant, str) for variant in self.variants):
            raise TypeError("variants must contain only strings")
        if not isinstance(self.worker_config, WorkerTaskPlannerConfig):
            raise TypeError("worker_config must be a WorkerTaskPlannerConfig")
        if not isinstance(self.survey_burst_active, bool):
            raise TypeError("survey_burst_active must be a boolean")
        if not isinstance(self.stuck_guard_enabled, bool):
            raise TypeError("stuck_guard_enabled must be a boolean")
        if isinstance(self.stuck_guard_ticks, bool) or not isinstance(self.stuck_guard_ticks, int):
            raise TypeError("stuck_guard_ticks must be an integer")
        if self.stuck_guard_ticks < 1:
            raise ValueError("stuck_guard_ticks must be at least 1")
        if isinstance(self.stuck_guard_radius, bool) or not isinstance(
            self.stuck_guard_radius, int
        ):
            raise TypeError("stuck_guard_radius must be an integer")
        if self.stuck_guard_radius < 1:
            raise ValueError("stuck_guard_radius must be at least 1")
        for name in (
            "movement_guard_enabled",
            "economy_budget_enabled",
            "economy_expansion_enabled",
            "raid_quota_enabled",
            "exploration_v2_enabled",
            "respawn_recovery_enabled",
            "barren_migration_enabled",
            "stuck_resources_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")
        for name in (
            "movement_loop_window",
            "movement_loop_min_unique",
            "movement_deposit_stall_ticks",
            "movement_deposit_repath_streak",
            "movement_cargo_spin_ticks",
            "movement_cargo_spin_budget",
            "movement_cargo_core_distance",
            "raid_min_observations",
            "raid_min_fighters",
            "respawn_worker_target",
            "respawn_detection_distance",
            "barren_migration_ticks",
            "barren_resource_distance",
            "stuck_resources_ticks",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be at least 1")
        if isinstance(self.raid_max_distance, bool) or not isinstance(self.raid_max_distance, int):
            raise TypeError("raid_max_distance must be an integer")
        if self.raid_max_distance < 0:
            raise ValueError("raid_max_distance cannot be negative")


def snapshot_from_turn(observation: TurnObservation) -> PlanningSnapshot:
    """Project one application turn into a deterministic planning snapshot."""

    if not isinstance(observation, TurnObservation):
        raise TypeError("observation must be a TurnObservation")
    economy = EconomyState.initial(
        EconomyTurnInput.observed(
            seed=0,
            tick=observation.tick,
            rules_version=observation.projection.rules_version,
            resources=observation.resources,
            population=observation.population,
        )
    )
    snapshot = extract_planning_snapshot(observation.projection, economy)
    # Previous-tick resolution events carry the engine's move-failure reasons;
    # the movement guard pairs them with the previously planned direction to
    # infer obstacles that were outside vision (see _infer_blocked_cells).
    move_failures = tuple(
        MoveFailureEvent(
            unit_id=event.actor_id.value,
            reason=event.reason if event.reason is not None else "",
        )
        for event in observation.events
        if event.actor_id is not None and event.kind == "UNIT_MOVE_FAILED"
    )
    if not move_failures:
        return snapshot
    return replace(snapshot, move_failures=move_failures)


def _dense_to_cardinal(dense_index: int) -> Direction:
    """Map one 16-way dense scan slot to the cardinal step on its dominant axis."""

    dx, dy = _DENSE_DELTAS[dense_index]
    if abs(dx) >= abs(dy):
        return Direction.EAST if dx > 0 else Direction.WEST
    return Direction.SOUTH if dy > 0 else Direction.NORTH


def _cargo_heal_direction(
    core: Coordinate,
    target: Coordinate,
    obstacles: frozenset[str],
) -> Direction:
    """Step Core toward the spinning worker, skipping blocked cells."""

    primary = step_toward(core, target)
    if core.step(primary).cell_key not in obstacles:
        return primary
    best_direction: Direction | None = None
    best_distance: int | None = None
    for direction in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
        next_cell = core.step(direction)
        if next_cell.cell_key in obstacles:
            continue
        distance = manhattan(next_cell, target)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_direction = direction
    return primary if best_direction is None else best_direction


def _worker_ordinal(snapshot: PlanningSnapshot, unit: PlanningUnit) -> int:
    """Return the unit's ordinal among controlled workers in snapshot order."""

    index = 0
    for candidate in snapshot.units:
        if candidate.id == unit.id:
            return index
        if candidate.unit_role is UnitRole.WORKER:
            index += 1
    return index


def _directions_toward_target(
    position: Coordinate,
    target: Coordinate,
) -> tuple[Direction, ...]:
    """Return cardinal directions ordered toward ``target`` first, then lateral.

    Mirrors the legacy TS oracle's ``orderedDirections``: axis-aligned steps
    that reduce Manhattan distance come first, then the remaining directions.
    Used by the obstacle-aware greedy fallback so a worker never steps into a
    visible obstacle when the primary pathfinder returns None.
    """

    dx = target.x - position.x
    dy = target.y - position.y
    ordered: list[Direction] = []
    if dx != 0:
        ordered.append(Direction.EAST if dx > 0 else Direction.WEST)
    if dy != 0:
        ordered.append(Direction.SOUTH if dy > 0 else Direction.NORTH)
    for candidate in (Direction.EAST, Direction.SOUTH, Direction.WEST, Direction.NORTH):
        if candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered)


def _route_direction(
    unit: PlanningUnit,
    target: Coordinate,
    obstacles: frozenset[str],
    discouraged: frozenset[str] | None = None,
) -> Direction:
    """Obstacle-aware first step toward ``target``, with layered fallbacks.

    Layer 1: A* (Manhattan heuristic, 32k node budget, 128 radius) for
    efficient long-distance routing around visible obstacles. When
    ``discouraged`` is provided, those cells receive a +4 cost penalty
    (soft avoidance) — used for the worker's recent trail to prevent
    retracing the same path.

    Layer 2 (fallback): obstacle-aware greedy step matching the legacy TS
    oracle's tier-3 fail-safe — tries directions toward the target but never
    steps into a cell present in ``obstacles``. This guarantees a visible
    obstacle is never walked into when the primary pathfinder gives up.

    Layer 3 (last resort): terrain-blind ``step_toward``. Only reached when
    every adjacent cell is an obstacle (worker fully surrounded), at which
    point the movement guard's sticky escape and barren-migration hooks take
    over.
    """

    direction = astar_next_step(unit.position, target, obstacles, discouraged=discouraged)
    if direction is not None:
        return direction
    for candidate in _directions_toward_target(unit.position, target):
        delta_x, delta_y = candidate.delta
        neighbor_key = f"{unit.position.x + delta_x},{unit.position.y + delta_y}"
        if neighbor_key not in obstacles:
            return candidate
    return step_toward(unit.position, target)


_MIGRATION_WAYPOINT_RADIUS: Final = 60


def _migration_step_toward_origin(
    core: Coordinate,
    obstacles: frozenset[str],
) -> Direction | None:
    """A*-routed first migration step toward the origin.

    The legacy ``migration_direction_toward_origin`` probe only tests the two
    toward-origin neighbors, so a migrating Core that ran into any terrain
    obstacle received ``None`` and — after three strikes — self-destructed
    instead of going around it (observed live: two Cores destroyed themselves
    mid-trek). Routing through ``astar_next_step`` against a waypoint clamped
    inside the pathfinder's search radius slides the Core around obstacles.
    ``None`` now means the Core is genuinely enclosed, which is the only case
    where the self-destruct fail-safe is appropriate.
    """

    if core.x == 0 and core.y == 0:
        return None
    waypoint_x = max(
        core.x - _MIGRATION_WAYPOINT_RADIUS,
        min(core.x + _MIGRATION_WAYPOINT_RADIUS, 0),
    )
    waypoint_y = max(
        core.y - _MIGRATION_WAYPOINT_RADIUS,
        min(core.y + _MIGRATION_WAYPOINT_RADIUS, 0),
    )
    waypoint = Coordinate(waypoint_x, waypoint_y)
    direction = astar_next_step(core, waypoint, obstacles)
    if direction is not None:
        return direction
    label = migration_direction_toward_origin(core, obstacles)
    if label is None:
        return None
    return {
        "E": Direction.EAST,
        "W": Direction.WEST,
        "S": Direction.SOUTH,
        "N": Direction.NORTH,
    }[label]


def _migration_step_away_from(
    core: Coordinate,
    enemy_position: Coordinate,
    obstacles: frozenset[str],
) -> Direction | None:
    """Return the first cardinal migration step that maximizes enemy distance.

    Used during respawn recovery in a war zone: respawn placement is 20-30
    tiles from the nearest living Core, so the naive toward-origin step can
    walk straight back into the attacker's kill range. Preferring the
    unblocked cardinal neighbor farthest from the visible enemy breaks that
    re-entry loop. ``None`` means every cardinal neighbor is terrain-blocked.
    """

    candidates: list[tuple[int, Direction]] = []
    for direction in (Direction.NORTH, Direction.EAST, Direction.SOUTH, Direction.WEST):
        neighbor = core.step(direction)
        if cell_key(neighbor) in obstacles:
            continue
        candidates.append((manhattan(neighbor, enemy_position), direction))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _routing_obstacles(snapshot: PlanningSnapshot) -> frozenset[str]:
    """Terrain obstacles plus the Core cell for non-deposit worker routing.

    The Core cell is transit-forbidden: a worker walking to a mine would
    otherwise path straight through it every tick (the cell legally holds the
    Core plus one unit), oscillating with the vacate override and blocking
    deposits from cargo workers (reproduced by the state-seed replay harness
    as a 500-tick deposit stall). Deposits remain the only way onto the Core
    cell.
    """

    obstacles = snapshot.obstacle_cells
    core = snapshot.core_position
    if core is None:
        return obstacles
    return obstacles | {core.cell_key}


# Candidate D: when an enemy is this close to the Core, cargo-less workers
# stop trekking outward and hold near home instead of walking into the
# attacker's path one by one (production t2 wiped 2026-08-21: workers picked
# off over ~160 ticks while the economy kept sending them out).
WORKER_SANCTUARY_RADIUS: Final = 5


def _worker_threat_sanctuary(snapshot: PlanningSnapshot, plan: Plan) -> Plan:
    """Redirect cargo-less worker MOVEs home while an enemy is at the door."""

    core = snapshot.core_position
    if core is None or not snapshot.enemy_units:
        return plan
    nearest_enemy_distance = min(
        manhattan(core, enemy.position) for enemy in snapshot.enemy_units
    )
    if nearest_enemy_distance > WORKER_SANCTUARY_RADIUS:
        return plan
    units_by_id = {unit.id: unit for unit in snapshot.units}
    parking_cells = [core.step(direction) for direction in Direction]
    open_parking = [
        cell for cell in parking_cells if cell_key(cell) not in snapshot.obstacle_cells
    ]
    if not open_parking:
        return plan
    new_actions = list(plan.unit_actions)
    for index, action in enumerate(new_actions):
        if action.type is not UnitActionType.MOVE:
            continue
        unit = units_by_id.get(action.unit_id)
        if unit is None or unit.unit_role is not UnitRole.WORKER or unit.cargo != 0:
            continue
        if manhattan(unit.position, core) <= 1:
            continue
        parking = min(open_parking, key=lambda cell: manhattan(unit.position, cell))
        direction = astar_next_step(
            unit.position,
            parking,
            _routing_obstacles(snapshot),
            search_radius=RECALL_ROUTE_RADIUS,
            node_budget=RECALL_ROUTE_NODE_BUDGET,
        )
        if direction is None:
            direction = step_toward(unit.position, parking)
        new_actions[index] = PlanningUnitAction(
            unit_id=action.unit_id,
            type=UnitActionType.MOVE,
            direction=direction,
        )
    return Plan(
        tick=plan.tick,
        unit_actions=tuple(new_actions),
        core_action=plan.core_action,
    )


def _vacate_core_cell_actions(
    snapshot: PlanningSnapshot,
    unit_actions: tuple[PlanningUnitAction, ...],
) -> tuple[PlanningUnitAction, ...]:
    """Override idle workers on the Core cell to vacate it.

    A cell holds two occupying entities and the Core always takes one slot, so
    an empty-cargo worker standing on the Core cell blocks deposits from other
    workers (CORE_MOVING-free but CELL_UNIT_LIMIT) and any SPAWN. Movement
    resolves before the Core action, so issuing a one-cell vacate MOVE frees
    the slot. Cells at capacity two are avoided, but a single friendly or
    enemy occupant does not block the vacate — the target cell can legally
    hold one more. Only WAIT-actions are overridden: a worker with a real
    task (deposit, harvest, an existing move) already has somewhere to go.
    """

    core = snapshot.core_position
    if core is None:
        return unit_actions
    occupancy: dict[str, int] = {}
    for unit in snapshot.units:
        if unit.position == core:
            continue
        key = unit.position.cell_key
        occupancy[key] = occupancy.get(key, 0) + 1
    for enemy in snapshot.enemy_units:
        key = enemy.position.cell_key
        occupancy[key] = occupancy.get(key, 0) + 1
    vacate_candidates = [
        unit
        for unit in snapshot.units
        if unit.unit_role is UnitRole.WORKER
        and unit.cargo == 0
        and unit.position == core
    ]
    if not vacate_candidates:
        return unit_actions
    unit_actions_by_id = {action.unit_id: action for action in unit_actions}
    for worker in vacate_candidates:
        current = unit_actions_by_id.get(worker.id)
        if current is not None and current.type is not UnitActionType.WAIT:
            continue
        direction = _vacate_step(core, occupancy, snapshot.obstacle_cells)
        if direction is None:
            continue
        unit_actions = tuple(
            PlanningUnitAction(
                unit_id=action.unit_id,
                type=UnitActionType.MOVE,
                direction=direction,
            )
            if action.unit_id == worker.id
            else action
            for action in unit_actions
        )
    return unit_actions


def _vacate_step(
    core: Coordinate,
    occupancy: Mapping[str, int],
    obstacles: frozenset[str],
) -> Direction | None:
    """Return the first cardinal step off the Core cell with a free slot."""

    for direction in (Direction.EAST, Direction.SOUTH, Direction.NORTH, Direction.WEST):
        neighbor = core.step(direction)
        key = cell_key(neighbor)
        if key in obstacles or occupancy.get(key, 0) >= 2:
            continue
        return direction
    return None


def _recall_stranded_workers(snapshot: PlanningSnapshot, plan: Plan) -> Plan:
    """Override idle WAIT workers far from the Core to walk home.

    Workers that survived an old migration (or a respawn sweep) can idle tens
    of tiles from the Core with no collectable cells nearby; the collection
    cap keeps them out of the matrix but nothing brought them back, so they
    sat WAIT forever (production: t3 workers idled 45-89 tiles away). Idle
    workers beyond ``STRANDED_RECALL_DISTANCE`` step toward the nearest
    Core-adjacent parking cell instead of waiting in place.
    """

    core = snapshot.core_position
    if core is None:
        return plan
    units_by_id = {unit.id: unit for unit in snapshot.units}
    parking_cells = [core.step(direction) for direction in Direction]
    open_parking = [
        cell for cell in parking_cells if cell_key(cell) not in snapshot.obstacle_cells
    ]
    if not open_parking:
        return plan
    new_actions = list(plan.unit_actions)
    for index, action in enumerate(new_actions):
        if action.type is not UnitActionType.WAIT:
            continue
        unit = units_by_id.get(action.unit_id)
        if unit is None or unit.unit_role is not UnitRole.WORKER or unit.cargo != 0:
            continue
        if manhattan(unit.position, core) <= STRANDED_RECALL_DISTANCE:
            continue
        parking = min(open_parking, key=lambda cell: manhattan(unit.position, cell))
        # Wide search: the plain BFS (64-radius / 16k-node budget) tops out at
        # ~64 Chebyshev tiles, so workers stranded 65+ tiles away were
        # silently skipped and froze forever (production t3: a worker sat 111
        # tiles from the Core for hundreds of ticks; replay-verified).
        # A* covers 192 tiles; if even that fails, fall back to the greedy
        # axis step so the recall never silently no-ops.
        direction = astar_next_step(
            unit.position,
            parking,
            _routing_obstacles(snapshot),
            search_radius=RECALL_ROUTE_RADIUS,
            node_budget=RECALL_ROUTE_NODE_BUDGET,
        )
        if direction is None:
            direction = step_toward(unit.position, parking)
        new_actions[index] = PlanningUnitAction(
            unit_id=action.unit_id,
            type=UnitActionType.MOVE,
            direction=direction,
        )
    return Plan(
        tick=plan.tick,
        unit_actions=tuple(new_actions),
        core_action=plan.core_action,
    )


def _core_return_wait(
    snapshot: PlanningSnapshot,
    unit: PlanningUnit,
) -> PlanningUnitAction | None:
    """Return a WAIT when the Core cell is already occupied by another unit.

    The FFA cell capacity is two entities and the Core itself always occupies
    its own cell, leaving exactly one free slot. A worker adjacent to the Core
    that steps onto the cell while another controlled unit already stands there
    deterministically fails with ``CELL_UNIT_LIMIT``. Waiting one tick lets the
    resident deposit/heal and vacate the cell instead of re-issuing a blocked
    move in a tight loop.
    """

    core = snapshot.core_position
    if core is None:
        return None
    if manhattan(unit.position, core) != 1:
        return None
    if any(candidate.id != unit.id and candidate.position == core for candidate in snapshot.units):
        return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.WAIT)
    return None


def _task_action(
    assignment: Assignment,
    snapshot: PlanningSnapshot,
    *,
    route_aware: bool = False,
    trails: Mapping[str, LoopTrail] | None = None,
) -> PlanningUnitAction:
    """Convert one deterministic worker task into a planning unit action.

    Forced-task conversion matches the P4-11 task-to-action contract; the
    GO_RESOURCE and EXPLORE conversions mirror the oracle's
    ``DeterministicPlanner`` (GO_RESOURCE steps toward the cell, EXPLORE uses a
    deterministic patrol direction).

    When ``trails`` is provided, the worker's recent footprint is fed to the
    A* pathfinder as discouraged cells (+4 cost penalty), preventing the
    worker from retracing the same path and helping it break out of loops.
    """

    unit = next(
        (candidate for candidate in snapshot.units if candidate.id.value == assignment.unit_id),
        None,
    )
    if unit is None:
        raise ValueError(f"worker assignment references unknown unit {assignment.unit_id!r}")
    discouraged: frozenset[str] | None = None
    if trails is not None:
        trail = trails.get(assignment.unit_id)
        if trail is not None:
            soft_positions = soft_obstacles_from_trail(trail, unit.position)
            if soft_positions:
                discouraged = frozenset(cell_key(position) for position in soft_positions)
    task = assignment.task
    if task.type is TaskType.DEPOSIT:
        assert task.target is not None
        if unit.position == snapshot.core_position:
            return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.DEPOSIT)
        if route_aware:
            wait = _core_return_wait(snapshot, unit)
            if wait is not None:
                return wait
        direction = (
            _route_direction(unit, task.target, snapshot.obstacle_cells, discouraged)
            if route_aware
            else step_toward(unit.position, task.target)
        )
        return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.MOVE, direction=direction)
    if task.type is TaskType.HARVEST_CURRENT:
        return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.HARVEST)
    if task.type is TaskType.PICKUP_BEACON:
        return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.PICKUP_BEACON)
    if task.type is TaskType.RETURN_FOR_HEAL:
        assert task.target is not None
        if route_aware:
            wait = _core_return_wait(snapshot, unit)
            if wait is not None:
                return wait
        direction = (
            _route_direction(unit, task.target, _routing_obstacles(snapshot), discouraged)
            if route_aware
            else step_toward(unit.position, task.target)
        )
        return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.MOVE, direction=direction)
    if task.type is TaskType.GO_RESOURCE:
        assert task.target is not None
        if unit.position == task.target:
            return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.HARVEST)
        direction = (
            _route_direction(unit, task.target, _routing_obstacles(snapshot), discouraged)
            if route_aware
            else step_toward(unit.position, task.target)
        )
        return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.MOVE, direction=direction)
    if task.type is TaskType.EXPLORE:
        if task.target is not None:
            if unit.position == task.target:
                return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.WAIT)
            direction = (
                _route_direction(unit, task.target, snapshot.obstacle_cells, discouraged)
                if route_aware
                else step_toward(unit.position, task.target)
            )
            return PlanningUnitAction(
                unit_id=unit.id, type=UnitActionType.MOVE, direction=direction
            )
        dense = worker_dense_direction(_worker_ordinal(snapshot, unit))
        return PlanningUnitAction(
            unit_id=unit.id,
            type=UnitActionType.MOVE,
            direction=_dense_to_cardinal(dense),
        )
    return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.WAIT)


def merge_worker_tasks(
    plan: Plan,
    assignments: tuple[Assignment, ...],
    snapshot: PlanningSnapshot,
    *,
    route_aware: bool = False,
    trails: Mapping[str, LoopTrail] | None = None,
) -> Plan:
    """Override the baseline plan's worker actions with the assignment layer.

    WorkerTaskPlanner is the resource-task SSOT in the oracle; forced tasks and
    matrix/explore assignments replace the baseline safety actions for workers.

    When ``trails`` is provided, each worker's recent footprint is fed to the
    A* pathfinder as discouraged cells to prevent path retracing.
    """

    actions = {action.unit_id.value: action for action in plan.unit_actions}
    for assignment in assignments:
        actions[assignment.unit_id] = _task_action(
            assignment, snapshot, route_aware=route_aware, trails=trails
        )
    return Plan(
        tick=plan.tick,
        unit_actions=tuple(actions.values()),
        core_action=plan.core_action,
    )


_MOVEMENT_TASK_TYPES: Final = frozenset(
    {
        TaskType.GO_RESOURCE,
        TaskType.DEPOSIT,
        TaskType.RETURN_FOR_HEAL,
        TaskType.PICKUP_BEACON,
    }
)


def _apply_movement_overrides(
    plan: Plan,
    escape_steps: dict[str, Direction],
    pause_ids: frozenset[str],
) -> Plan:
    """Override worker MOVE actions with forced escapes or short-stop WAITs."""

    if not escape_steps and not pause_ids:
        return plan
    actions = []
    for action in plan.unit_actions:
        unit_id = action.unit_id.value
        if action.type is UnitActionType.MOVE and unit_id in pause_ids:
            actions.append(PlanningUnitAction(unit_id=action.unit_id, type=UnitActionType.WAIT))
        elif action.type is UnitActionType.MOVE and unit_id in escape_steps:
            actions.append(
                PlanningUnitAction(
                    unit_id=action.unit_id,
                    type=UnitActionType.MOVE,
                    direction=escape_steps[unit_id],
                )
            )
        else:
            actions.append(action)
    return Plan(
        tick=plan.tick,
        unit_actions=tuple(actions),
        core_action=plan.core_action,
    )


def _apply_raid_strike(
    plan: Plan,
    snapshot: PlanningSnapshot,
    target: Coordinate,
    strike: StrikeGroup,
) -> Plan:
    """Point strike-group rangers and vanguards at a confirmed enemy core."""

    unit_by_id = {unit.id.value: unit for unit in snapshot.units}
    actions = []
    for action in plan.unit_actions:
        unit_id = action.unit_id.value
        unit = unit_by_id.get(unit_id)
        if unit is None:
            actions.append(action)
        elif unit_id in strike.ranger_ids:
            if can_shoot(unit.position, target, snapshot.obstacle_cells):
                actions.append(
                    PlanningUnitAction(
                        unit_id=action.unit_id,
                        type=UnitActionType.SHOOT,
                        expected_cell=target,
                    )
                )
            else:
                # A raid target can be confirmed up to RAID_MAX_DISTANCE (40)
                # away but a Ranger shot only reaches 3 cells on a firing line,
                # so an out-of-range Ranger must close the distance first
                # instead of wasting every shot.
                actions.append(
                    PlanningUnitAction(
                        unit_id=action.unit_id,
                        type=UnitActionType.MOVE,
                        direction=step_toward(unit.position, target),
                    )
                )
        elif unit_id in strike.vanguard_ids and unit.position != target:
            if manhattan(unit.position, target) == 1:
                # Adjacent to the enemy Core: SWEEP damages it. A MOVE here
                # would only bump into the occupied Core cell and never land a
                # hit, so melee raiders must sweep once in reach.
                actions.append(
                    PlanningUnitAction(
                        unit_id=action.unit_id,
                        type=UnitActionType.SWEEP,
                        direction=step_toward(unit.position, target),
                    )
                )
            else:
                actions.append(
                    PlanningUnitAction(
                        unit_id=action.unit_id,
                        type=UnitActionType.MOVE,
                        direction=step_toward(unit.position, target),
                    )
                )
        else:
            actions.append(action)
    return Plan(
        tick=plan.tick,
        unit_actions=tuple(actions),
        core_action=plan.core_action,
    )


def _unit_intent(action: object) -> UnitIntent:
    if type(action) is not PlanningUnitAction:
        raise TypeError("expected a planning UnitAction")
    mapped = _UNIT_ACTION_TYPES.get(action.type)
    if mapped is None:
        raise ValueError(f"unsupported plan unit action {action.type.value!r}")
    return UnitIntent(
        unit_id=action.unit_id,
        action=mapped,
        direction=action.direction,
        target_id=action.target_id,
        expected_cell=action.expected_cell,
    )


def _core_intent(action: object) -> CoreIntent:
    if type(action) is not PlanningCoreAction:
        raise TypeError("expected a planning CoreAction")
    mapped = _CORE_ACTION_TYPES.get(action.type)
    if mapped is None:
        raise ValueError(f"unsupported plan core action {action.type.value!r}")
    return CoreIntent(
        action=mapped,
        direction=action.direction,
        unit_role=action.unit_role,
    )


def plan_to_decision(plan: Plan) -> Decision:
    """Convert one deterministic planning plan into an application decision."""

    if not isinstance(plan, Plan):
        raise TypeError("plan must be a Plan")
    return Decision(
        tick=plan.tick,
        unit_intents=tuple(_unit_intent(action) for action in plan.unit_actions),
        core_intent=None if plan.core_action is None else _core_intent(plan.core_action),
    )


class ComposedDecider:
    """Stateful composition over the safety and worker assignment layers.

    The decider holds the P4-12 cross-tick claim lease and previous assignment
    state explicitly, so repeated calls with identical observations produce
    identical decisions. One instance serves one live process.
    """

    def __init__(self, config: ComposedDeciderConfig | None = None) -> None:
        self._config = config if config is not None else ComposedDeciderConfig()
        if not isinstance(self._config, ComposedDeciderConfig):
            raise TypeError("config must be a ComposedDeciderConfig")
        effective = apply_variant_overrides(self._config.safety_config, self._config.variants)
        if self._config.exploration_v2_enabled:
            # Beacon S4 gate (production composition): below these economy
            # thresholds military units keep guarding instead of walking to a
            # ground Beacon. Pure-layer default 0.0 reproduces the oracle.
            effective = replace(
                effective,
                beacon_contest_min_population=max(
                    effective.beacon_contest_min_population,
                    BEACON_CONTEST_MIN_POPULATION,
                ),
                beacon_contest_min_resources=max(
                    effective.beacon_contest_min_resources,
                    BEACON_CONTEST_MIN_RESOURCES,
                ),
                # Military S4 (candidate B combat tier): Ranger predictive
                # fire leads moving enemies when no direct shot is available
                # and the imminent-threat Vanguard (candidate C, inside the
                # safety baseline) fields a defender against converging
                # enemies at low pop. Both are combat-triggered and measured
                # economy-neutral. massarmy_stages stays OFF: the 4-seed
                # preset measured -2.5 dep (-6.3%) from the pop>=8 Vanguard
                # + Ranger pair with zero combat gain in every available
                # instrument; enable it only after a discriminating combat
                # bench (more hunter seeds / longer ticks) shows a gain.
                ranger_predictive_fire=True,
            )
        self._safety = SafetyPlanner(effective)
        self._previous_assignments: tuple[Assignment, ...] = ()
        self._claims: frozenset[WorkerClaim] = frozenset()
        self._position_history: dict[str, tuple[Coordinate, ...]] = {}
        self._loop_trails: dict[str, LoopTrail] = {}
        self._deposit_progress: dict[str, DepositProgress] = {}
        self._move_backoff: dict[str, MoveBackoffState] = {}
        self._previous_move_actions: dict[str, bool] = {}
        self._previous_planned_directions: dict[str, Direction] = {}
        self._squad_by_unit: dict[str, str] = {}
        self._stationary_cores: dict[str, StationaryCore] = {}
        self._cargo_spin_history: dict[str, tuple[Coordinate, ...]] = {}
        self._raid_state = RaidState()
        self._replacement_queue = ReplacementQueue()
        self._previous_unit_roles: dict[str, str] = {}
        self._exploration_state = ExplorationState()
        self._respawn_state = RespawnRecoveryState()
        self._barren_migration = BarrenMigrationState()
        self._stuck_resources = StuckWithResourcesState()
        self._no_worker_deadlock_ticks = 0
        self._escape_sticky: dict[str, tuple[Direction, int]] = {}
        self._terrain_map = TerrainMap()
        self._previous_tick: int | None = None
        self._previous_core_position: Coordinate | None = None
        self._previous_resources: int | None = None
        self._previous_population: int | None = None
        # Terrain-trap self-destruct confirmation: unit id -> tick when it
        # first stood on the Core cell with no cargo. Only workers that keep
        # occupying the Core cell for TERRAIN_TRAP_CONFIRM_TICKS consecutive
        # ticks are destroyed (a worker merely passing through must survive).
        self._trap_suspects: dict[str, int] = {}

    @property
    def config(self) -> ComposedDeciderConfig:
        return self._config

    @property
    def config_hash(self) -> str:
        """Return the stable hash of the effective tunable inputs."""

        from .configuration import config_hash

        return config_hash(self._config)

    @property
    def strategy_hash(self) -> str:
        """Return the stable hash of implementation plus tunable inputs."""

        from .configuration import strategy_hash

        return strategy_hash(self._config)

    def safety_fallback(self, observation: TurnObservation) -> Decision:
        """Produce the inexpensive safety plan used after a strategy overrun."""

        snapshot = snapshot_from_turn(observation)
        return plan_to_decision(self._safety.decide(snapshot).plan)

    def state_summary(self) -> dict[str, object]:
        """Export a compact decider-state digest for offline stall diagnosis.

        Read-only: persisting this never affects decisions. It answers the
        "why did the Core wait / why is migration not firing" questions
        directly from the hooks' own state machines instead of reverse-
        engineering them from positions and events.
        """
        barren = self._barren_migration
        stuck = self._stuck_resources
        raid = self._raid_state
        return {
            "barrenMigration": {
                "active": barren.migration_active,
                "barrenSinceTick": barren.barren_since_tick,
                "migrationStartedTick": barren.migration_started_tick,
                "resetCount": barren.reset_count,
                "failCount": barren.migration_fail_count,
            },
            "noWorkerDeadlockTicks": self._no_worker_deadlock_ticks,
            "stuckResources": {
                "lastPopulation": stuck.last_population,
                "stuckSinceTick": stuck.stuck_since_tick,
            },
            "respawnRecovery": {
                "active": self._respawn_state.active,
                "detectedTick": self._respawn_state.detected_tick,
            },
            "raid": {
                "enabled": raid.enabled,
                "recall": raid.recall,
                "corePosition": (
                    None
                    if raid.core_position is None
                    else [raid.core_position.x, raid.core_position.y]
                ),
                "vanguards": len(raid.vanguard_ids),
                "rangers": len(raid.ranger_ids),
            },
            "loopTrails": len(self._loop_trails),
            "moveBackoff": len(self._move_backoff),
        }

    @property
    def raid_state(self) -> RaidState:
        return self._raid_state

    @property
    def replacement_queue(self) -> ReplacementQueue:
        return self._replacement_queue

    def __call__(
        self,
        observation: TurnObservation,
        budget: DeadlineBudget,
    ) -> Decision:
        if not isinstance(budget, DeadlineBudget):
            raise TypeError("budget must be a DeadlineBudget")
        if budget.exhausted:
            return Decision(
                tick=observation.tick,
                core_intent=CoreIntent(action=ApplicationCoreAction.WAIT),
            )
        snapshot = snapshot_from_turn(observation)
        return plan_to_decision(self.decide_snapshot(snapshot))

    def _stuck_blocked_cells(self, snapshot: PlanningSnapshot) -> frozenset[str]:
        """Return resource targets to block for workers that look stuck this tick."""

        n_ticks = self._config.stuck_guard_ticks
        k_cells = self._config.stuck_guard_radius
        current_positions = {
            unit.id.value: unit.position
            for unit in snapshot.units
            if unit.unit_role is UnitRole.WORKER
        }
        positions_by_unit: dict[str, tuple[Coordinate, ...]] = {}
        for unit_id, position in current_positions.items():
            recent = (self._position_history.get(unit_id, ()) + (position,))[-n_ticks:]
            positions_by_unit[unit_id] = recent

        stuck_ids = detect_stuck_unit_ids(
            positions_by_unit,
            n_ticks=n_ticks,
            k_cells=k_cells,
        )
        blocked: set[str] = set()
        for assignment in self._previous_assignments:
            if assignment.unit_id not in stuck_ids:
                continue
            if assignment.task.type is not TaskType.GO_RESOURCE:
                continue
            key = assignment.task.target_cell_key
            if key is None and assignment.task.target is not None:
                key = assignment.task.target.cell_key
            if key is not None:
                blocked.add(key)

        self._position_history = positions_by_unit
        return frozenset(blocked)

    def _previous_assignment_for(self, unit_id: str) -> Assignment | None:
        """Return the worker's assignment from the previous tick, if any."""

        return next(
            (
                assignment
                for assignment in self._previous_assignments
                if assignment.unit_id == unit_id
            ),
            None,
        )

    @staticmethod
    def _movement_target_for(assignment: Assignment | None) -> Coordinate | None:
        """Return the previous movement target, or None for non-movement tasks."""

        if assignment is None or assignment.task.type not in _MOVEMENT_TASK_TYPES:
            return None
        return assignment.task.target

    @staticmethod
    def _previous_go_resource_key(assignment: Assignment | None) -> str | None:
        """Return the previous GO_RESOURCE cell key to block, if any."""

        if assignment is None or assignment.task.type is not TaskType.GO_RESOURCE:
            return None
        key = assignment.task.target_cell_key
        if key is None and assignment.task.target is not None:
            key = assignment.task.target.cell_key
        return key

    @staticmethod
    def _is_core_return(assignment: Assignment | None) -> bool:
        """Return whether the previous assignment walks the worker back to Core."""

        return assignment is not None and assignment.task.type in (
            TaskType.DEPOSIT,
            TaskType.RETURN_FOR_HEAL,
        )

    def _movement_guard_hook(
        self,
        snapshot: PlanningSnapshot,
        blocked_cells: frozenset[str],
    ) -> tuple[frozenset[str], dict[str, Direction], frozenset[str], PlanningCoreAction | None]:
        """Fold one tick of movement observations into escape/pause overrides."""

        escape_steps: dict[str, Direction] = {}
        pause_ids: set[str] = set()
        core = snapshot.core_position
        # The escape planner speaks Coordinate obstacles; the snapshot carries
        # cell-key strings, so convert once per tick.
        hard_obstacles = frozenset(
            parse_cell_key(key) for key in snapshot.obstacle_cells
        )
        for unit in snapshot.units:
            if unit.unit_role is not UnitRole.WORKER:
                continue
            unit_id = unit.id.value
            previous_trail = self._loop_trails.get(unit_id, LoopTrail())

            previous_move = self._previous_move_actions.get(unit_id, False)
            previous_position = previous_trail.last_pos
            moved = (
                previous_move
                and previous_position is not None
                and previous_position != unit.position
            )
            blocked = (
                previous_move
                and previous_position is not None
                and previous_position == unit.position
            )
            backoff = update_move_backoff(
                self._move_backoff.get(unit_id),
                tick=snapshot.tick,
                moved=moved,
                blocked=blocked,
            )
            self._move_backoff[unit_id] = backoff

            trail = observe_loop_position(
                previous_trail,
                unit.position,
                window=self._config.movement_loop_window,
            )
            assignment = self._previous_assignment_for(unit_id)
            target = self._movement_target_for(assignment)
            if blocked:
                self._escape_sticky.pop(unit_id, None)
            sticky_entry = self._escape_sticky.get(unit_id)
            if sticky_entry is not None and snapshot.tick < sticky_entry[1]:
                escape_steps[unit_id] = sticky_entry[0]
            elif should_pause_move(backoff, tick=snapshot.tick):
                pause_ids.add(unit_id)
            elif backoff.fail_streak >= 2:
                escape_target = target if target is not None else core
                if escape_target is not None:
                    step = forced_escape_step(
                        unit.position,
                        escape_target,
                        soft_obstacles_from_trail(trail, unit.position) | hard_obstacles,
                        repath_side=trail.repath_side,
                    )
                    if step is not None:
                        escape_steps[unit_id] = step
                        self._escape_sticky[unit_id] = (
                            step,
                            snapshot.tick + ESCAPE_STICKY_TICKS,
                        )
            loop = detect_spatial_loop(
                trail,
                target=target,
                window=self._config.movement_loop_window,
                min_unique=self._config.movement_loop_min_unique,
            )
            if loop and target is not None:
                trail = mark_loop_repath(trail, snapshot.tick)
                key = self._previous_go_resource_key(assignment)
                if key is not None:
                    blocked_cells = blocked_cells | {key}
                step = forced_escape_step(
                    unit.position,
                    target,
                    soft_obstacles_from_trail(trail, unit.position) | hard_obstacles,
                    repath_side=trail.repath_side,
                )
                if step is not None:
                    escape_steps[unit_id] = step
            elif core is not None and target is not None and self._is_core_return(assignment):
                progress = refresh_deposit_progress(
                    self._deposit_progress.get(unit_id),
                    manhattan(unit.position, core),
                    snapshot.tick,
                )
                needs_escape = deposit_escape_needed(
                    progress,
                    snapshot.tick,
                    stall_ticks=self._config.movement_deposit_stall_ticks,
                    repath_streak_limit=self._config.movement_deposit_repath_streak,
                )
                if needs_escape:
                    step = forced_escape_step(
                        unit.position,
                        core,
                        soft_obstacles_from_trail(trail, unit.position) | hard_obstacles,
                        repath_side=trail.repath_side,
                    )
                    if step is not None:
                        escape_steps[unit_id] = step
                    progress = record_deposit_repath(progress, repathed=True)
                else:
                    progress = record_deposit_repath(progress, repathed=False)
                self._deposit_progress[unit_id] = progress
            self._loop_trails[unit_id] = trail

        spin_ticks = self._config.movement_cargo_spin_ticks
        self._cargo_spin_history = {
            unit.id.value: (self._cargo_spin_history.get(unit.id.value, ()) + (unit.position,))[
                -spin_ticks:
            ]
            for unit in snapshot.units
            if unit.unit_role is UnitRole.WORKER
        }

        core_action = self._cargo_spin_core_action(snapshot)
        return blocked_cells, escape_steps, frozenset(pause_ids), core_action

    def _cargo_spin_core_action(self, snapshot: PlanningSnapshot) -> PlanningCoreAction | None:
        """Return a Core START_MOVE toward a spinning loaded worker, or None."""

        core = snapshot.core_position
        if core is None or snapshot.core_state != "normal":
            return None
        for unit in snapshot.units:
            if unit.unit_role is not UnitRole.WORKER or unit.cargo <= 0:
                continue
            history = self._cargo_spin_history.get(unit.id.value, ())
            if cargo_spin_self_heal(
                history,
                unit.cargo,
                core,
                spin_ticks=self._config.movement_cargo_spin_ticks,
                spin_budget=self._config.movement_cargo_spin_budget,
                core_distance_threshold=self._config.movement_cargo_core_distance,
            ):
                return PlanningCoreAction(
                    type=CoreActionType.START_MOVE,
                    direction=_cargo_heal_direction(core, unit.position, snapshot.obstacle_cells),
                )
        return None

    def _respawn_recovery_hook(self, snapshot: PlanningSnapshot, plan: Plan) -> Plan:
        """Force Worker production after a detected Core respawn (economy-first).

        Tracks the Core position across ticks; a single-tick teleport beyond the
        configured detection distance is a destroy-then-respawn signal. While the
        recovery latch is active, SPAWN is forced to WORKER until the recovery
        worker target is reached, using projected same-tick resources at a zero
        reserve so the fresh Core can start rolling its economy immediately.
        """

        current_core = snapshot.core_position
        if current_core is not None:
            if detect_respawn(
                self._previous_core_position,
                current_core,
                detection_distance=self._config.respawn_detection_distance,
            ):
                self._respawn_state.note_respawn(snapshot.tick)
                # Clear stale exploration memory from the pre-respawn location.
                # Old resource-cell memory (cell_positions) would make the
                # barren-migration hook think resources still exist, preventing
                # Core migration toward origin. Old visited-point and chunk
                # data are also stale — the new Core has fresh Workers at a
                # new position. Obstacles (permanent terrain) are kept.
                self._exploration_state.reset_location_state()
                self._barren_migration.reset()
                self._stuck_resources.reset()
            self._previous_core_position = current_core

        if not self._config.respawn_recovery_enabled:
            return plan
        if not self._respawn_state.active:
            return plan
        if snapshot.core_state != "normal":
            # A moving (migrating) Core is handled by migration; do not fight it.
            return plan
        # Survival actions from the safety baseline always win over forced
        # worker production. A threat-triggered military spawn also wins: a
        # Vanguard at the door defends the fresh Core while recovery rebuilds
        # the economy; a normal (non-threat) military spawn is overridden so
        # recovery stays economy-first.
        baseline_core = plan.core_action
        if baseline_core is not None:
            if baseline_core.type in (CoreActionType.HEAL, CoreActionType.REPAIR_SHIELD):
                return plan
            if (
                baseline_core.type is CoreActionType.SPAWN
                and baseline_core.unit_role is not UnitRole.WORKER
            ):
                core_position = snapshot.core_position
                if core_position is not None and snapshot.enemy_units:
                    nearest_threat = min(
                        manhattan(core_position, enemy.position)
                        for enemy in snapshot.enemy_units
                    )
                    if nearest_threat <= self._safety.config.threat_enemy_distance:
                        return plan

        workers = sum(1 for unit in snapshot.units if unit.unit_role is UnitRole.WORKER)
        if workers >= self._config.respawn_worker_target:
            self._respawn_state.note_recovered()
            return plan

        deposit_cargo = 0
        core = snapshot.core_position
        if core is not None:
            for unit in snapshot.units:
                if unit.unit_role is UnitRole.WORKER and unit.position == core and unit.cargo > 0:
                    deposit_cargo += unit.cargo
        projected = projected_core_resources(
            resources=snapshot.resources,
            resource_space=snapshot.resource_space,
            deposit_cargo=deposit_cargo,
            healing_reserve=0,
        )
        cost = unit_price(UnitRole.WORKER, snapshot.population, snapshot.rules_version)
        if projected >= cost:
            return Plan(
                tick=plan.tick,
                unit_actions=plan.unit_actions,
                core_action=PlanningCoreAction(
                    type=CoreActionType.SPAWN,
                    unit_role=UnitRole.WORKER,
                ),
            )
        return Plan(
            tick=plan.tick,
            unit_actions=plan.unit_actions,
            core_action=PlanningCoreAction(type=CoreActionType.WAIT),
        )

    def _barren_migration_hook(self, snapshot: PlanningSnapshot, plan: Plan) -> Plan:
        """Start Core migration toward origin when stuck in a resource-barren area.

        After ``barren_migration_ticks`` consecutive ticks with zero visible
        resource cells, the Core should START_MOVE toward [0, 0] where
        resource density is higher. This only fires when the Core is not already
        migrating and has no resources to spawn. Once migration starts, the
        official four-tick migration cycle runs automatically — workers continue
        exploring during migration.
        """

        if not self._config.barren_migration_enabled:
            return plan
        if snapshot.core_state != "normal":
            return plan
        core = snapshot.core_position
        if core is None:
            return plan
        core_migrating = snapshot.core_state == "moving"

        # Cross-tick economic activity tracking: a deposit (resources grew) or
        # a successful spawn (population grew) proves the current region still
        # yields. Sparse rings hold only ~2 resources per 32x32 chunk, so the
        # visible-cell-only trigger misclassified productive regions as barren
        # and migrated the Core away from them (production: t4 lost 111
        # deposits in 400 ticks to CORE_MOVING while the Core constantly
        # migrated). Economic activity therefore resets the state fully — the
        # region is productive, stay put.
        #
        # 2026-08-21 refinement: activity only counts as LOCAL yield. The
        # deposit must come from a harvest recorded within the barren radius
        # of the Core; cargo trekked in from far away must NOT cancel the
        # migration latch. Production t1/t3/t4 crawled one 4-tick migration
        # cycle at a time for hours because every distant-harvest deposit
        # reset the latch the moment it landed.
        previous_resources = self._previous_resources
        previous_population = self._previous_population
        self._previous_resources = snapshot.resources
        self._previous_population = snapshot.population
        economic_activity = (
            previous_resources is not None
            and (
                snapshot.resources > previous_resources
                or snapshot.population > previous_population
            )
            and core is not None
            and has_local_yield(
                self._exploration_state.harvested_cells,
                core,
                self._config.barren_resource_distance,
            )
        )

        # A cargo-carrying worker near the Core is about to deposit; the engine
        # rejects deposits while the Core is migrating (CORE_MOVING), and the
        # continuous-stepping migration latch leaves almost no stationary
        # window. Hold the next START_MOVE so the deposit lands — the
        # resulting economic activity then cancels the migration latch.
        for unit in snapshot.units:
            if (
                unit.unit_role is UnitRole.WORKER
                and unit.cargo > 0
                and manhattan(unit.position, core) <= DEPOSIT_HOLD_RADIUS
            ):
                return plan

        # During respawn recovery in a war zone, prefer stepping away from a
        # nearby enemy over stepping toward origin: respawn placement is 20-30
        # tiles from the nearest living Core, and walking straight back toward
        # the attacker re-enters its kill range (production: t3 destroyed 14
        # times, respawning in place each time). This fires independently of
        # the barren latch — a visible threat outweighs "no resources seen".
        flee_blocked = snapshot.obstacle_cells | frozenset(
            cell_key(unit.position) for unit in snapshot.units
        ) | frozenset(cell_key(enemy.position) for enemy in snapshot.enemy_units)
        if self._respawn_state.active:
            nearest_enemy = min(
                (manhattan(core, enemy.position) for enemy in snapshot.enemy_units),
                default=None,
            )
            if nearest_enemy is not None and nearest_enemy <= ENEMY_FLEE_RADIUS:
                nearest = min(
                    snapshot.enemy_units,
                    key=lambda enemy: manhattan(core, enemy.position),
                )
                flee_step = _migration_step_away_from(core, nearest.position, flee_blocked)
                if flee_step is not None:
                    return Plan(
                        tick=plan.tick,
                        unit_actions=plan.unit_actions,
                        core_action=PlanningCoreAction(
                            type=CoreActionType.START_MOVE,
                            direction=flee_step,
                        ),
                    )

        # A visible/remembered resource sitting beyond ``barren_resource_distance``
        # tiles is treated as effectively absent: workers cannot bootstrap an
        # economy against a target that far, so the Core should migrate toward
        # origin (higher resource density) rather than send workers on a 90+
        # tile trek. This closes the gap where the original barren trigger
        # (``resource_cells`` empty) never fired because a distant resource kept
        # the set non-empty — observed live with the nearest resource 96 tiles
        # away and workers never harvesting.
        nearest_resource_distance: int | None = None
        if snapshot.resource_cells and core is not None:
            nearest_resource_distance = min(
                manhattan(core, cell_info.position)
                for cell_info in snapshot.resource_cells.values()
            )
        has_reachable_resources = bool(snapshot.resource_cells) and (
            nearest_resource_distance is not None
            and nearest_resource_distance <= self._config.barren_resource_distance
        )
        should_migrate = self._barren_migration.observe(
            has_resource_cells=has_reachable_resources,
            tick=snapshot.tick,
            core_migrating=core_migrating,
            barren_threshold=self._config.barren_migration_ticks,
            economic_activity=economic_activity,
        )
        if not should_migrate:
            return plan

        terrain_step = _migration_step_toward_origin(core, snapshot.obstacle_cells)
        if terrain_step is None:
            # No terrain-routed path at all: the Core is genuinely enclosed by
            # obstacles, so keep counting toward the self-destruct fail-safe.
            self._barren_migration.migration_fail_count += 1
            if self._barren_migration.migration_fail_count >= DEFAULT_BARREN_MIGRATION_FAIL_LIMIT:
                return Plan(
                    tick=plan.tick,
                    unit_actions=plan.unit_actions,
                    core_action=PlanningCoreAction(
                        type=CoreActionType.SELF_DESTRUCT,
                    ),
                )
            return plan
        # A terrain path exists, so the Core is not terrain-enclosed; reset the
        # fail-safe counter before checking unit occupancy.
        self._barren_migration.migration_fail_count = 0
        # The engine rejects a Core START_MOVE into any occupied cell
        # (CORE_DESTINATION_OCCUPIED). Route the first step around friendly
        # units and visible enemies too, not just terrain: previously the step
        # ignored units, so a worker standing on the migration path left the
        # Core retrying the same blocked cell every cycle (observed live, 21
        # consecutive CORE_DESTINATION_OCCUPIED). If every routed step is
        # currently occupied, wait for the unit to move rather than striking.
        blocked_cells = snapshot.obstacle_cells | frozenset(
            cell_key(unit.position) for unit in snapshot.units
        ) | frozenset(cell_key(enemy.position) for enemy in snapshot.enemy_units)
        direction = _migration_step_toward_origin(core, blocked_cells)
        if direction is None:
            return plan
        return Plan(
            tick=plan.tick,
            unit_actions=plan.unit_actions,
            core_action=PlanningCoreAction(
                type=CoreActionType.START_MOVE,
                direction=direction,
            ),
        )

    def _terrain_trap_hook(self, snapshot: PlanningSnapshot, plan: Plan) -> Plan:
        """Break terrain-trap deadlock by self-destructing the trapped worker.

        When the Core has resources (> 0) but the population hasn't grown for
        ``stuck_resources_ticks`` consecutive ticks, the most likely cause is
        a terrain trap: the worker is stuck on the Core's cell
        (MOVE_BLOCKED_TERRAIN) and the Core can't spawn (CELL_UNIT_LIMIT).
        Self-destructing the trapped worker frees the Core's cell so the Core
        can spawn a replacement — much less disruptive than self-destructing
        the Core itself (which respawns at a random location that may be worse).
        """

        if not self._config.stuck_resources_enabled:
            return plan
        if snapshot.core_state != "normal":
            return plan
        core_pos = snapshot.core_position
        # Track on-Core occupancy every tick (not only when the timer fires)
        # so the confirmation counts real consecutive occupancy. A worker
        # merely passing through the Core cell must not be destroyed when
        # the stuck-resources timer happens to fire (production t2 burned 5
        # resources per cycle across six SPAWN_FAILED -> self-destruct ->
        # spawn rounds; the killed worker was often not the actual blocker).
        if core_pos is not None:
            trapped_now = [
                unit
                for unit in snapshot.units
                if unit.position == core_pos
                and unit.unit_role is UnitRole.WORKER
                and unit.cargo == 0
            ]
            for unit_id, _since_tick in tuple(self._trap_suspects.items()):
                if any(unit.id.value == unit_id for unit in trapped_now):
                    continue
                del self._trap_suspects[unit_id]
            for unit in trapped_now:
                self._trap_suspects.setdefault(unit.id.value, snapshot.tick)
        should_fire = self._stuck_resources.observe(
            resources=snapshot.resources,
            population=snapshot.population,
            tick=snapshot.tick,
            threshold=self._config.stuck_resources_ticks,
        )
        if not should_fire:
            return plan
        if core_pos is None:
            return plan
        # A cargo-carrying worker standing on the Core is mid-deposit, not
        # trapped: killing it would drop the cargo and delay the economy.
        # Only an empty-cargo worker that keeps occupying the Core's cell
        # (blocking SPAWN) is a trap candidate.
        trapped_workers = [
            unit
            for unit in snapshot.units
            if unit.position == core_pos
            and unit.unit_role is UnitRole.WORKER
            and unit.cargo == 0
            and snapshot.tick - self._trap_suspects.get(unit.id.value, snapshot.tick)
            >= TERRAIN_TRAP_CONFIRM_TICKS
        ]
        if not trapped_workers:
            return plan
        # Self-destructing only helps when the Core can immediately afford a
        # replacement; otherwise it just shrinks the fleet for nothing.
        replacement_cost = unit_price(
            UnitRole.WORKER, snapshot.population, snapshot.rules_version
        )
        if snapshot.resources < replacement_cost:
            return plan
        trapped_id = trapped_workers[0].id
        new_unit_actions = tuple(
            PlanningUnitAction(
                unit_id=trapped_id,
                type=UnitActionType.SELF_DESTRUCT,
            )
            if action.unit_id == trapped_id
            else action
            for action in plan.unit_actions
        )
        return replace(plan, unit_actions=new_unit_actions)

    def _economy_budget_hook(self, snapshot: PlanningSnapshot, plan: Plan) -> Plan:
        """Skip Core SPAWN when same-tick deposits minus heal reserve cannot pay."""

        core_action = plan.core_action
        if core_action is None or core_action.type is not CoreActionType.SPAWN:
            return plan
        core = snapshot.core_position
        deposit_cargo = 0
        healing_roles: list[UnitRole] = []
        if core is not None:
            for unit in snapshot.units:
                if unit.position != core:
                    continue
                if unit.unit_role is UnitRole.WORKER and unit.cargo > 0:
                    deposit_cargo += unit.cargo
                if unit.health < unit_max_health(unit.unit_role):
                    healing_roles.append(unit.unit_role)
        projected = projected_core_resources(
            resources=snapshot.resources,
            resource_space=snapshot.resource_space,
            deposit_cargo=deposit_cargo,
            healing_reserve=heal_reserve(healing_roles),
        )
        role = core_action.unit_role
        if role is None:
            return plan
        cost = unit_price(role, snapshot.population, snapshot.rules_version)
        if projected >= cost:
            return plan
        return Plan(
            tick=plan.tick,
            unit_actions=plan.unit_actions,
            core_action=PlanningCoreAction(type=CoreActionType.WAIT),
        )

    def _economy_expansion_hook(self, snapshot: PlanningSnapshot, plan: Plan) -> Plan:
        """Aggressively grow workers: spawn on projected resources, stepped reserve.

        When the economy-expansion research switch is enabled this hook replaces
        the worker-production gate below ``worker_target`` with the
        ``worker_expansion_threshold`` schedule at a lowered early reserve and
        counts same-tick deposits into the affordability check, so early workers
        spawn as soon as the Core can pay for them.
        """

        if snapshot.core_state != "normal":
            return plan
        # Survival actions from the safety baseline (critical HEAL, shield
        # repair) and threat-response military spawns always win over
        # aggressive worker expansion.
        baseline_core = plan.core_action
        if baseline_core is not None and (
            baseline_core.type in (CoreActionType.HEAL, CoreActionType.REPAIR_SHIELD)
            or (
                baseline_core.type is CoreActionType.SPAWN
                and baseline_core.unit_role is not UnitRole.WORKER
            )
        ):
            return plan
        workers = sum(1 for unit in snapshot.units if unit.unit_role is UnitRole.WORKER)
        if workers > 0:
            self._no_worker_deadlock_ticks = 0
        if workers >= self._safety.config.worker_target:
            return plan
        deposit_cargo = 0
        core = snapshot.core_position
        if core is not None:
            for unit in snapshot.units:
                if unit.unit_role is UnitRole.WORKER and unit.position == core and unit.cargo > 0:
                    deposit_cargo += unit.cargo
        projected = projected_core_resources(
            resources=snapshot.resources,
            resource_space=snapshot.resource_space,
            deposit_cargo=deposit_cargo,
            healing_reserve=0,
        )
        threshold = worker_expansion_threshold(
            worker_count=workers,
            worker_target=self._safety.config.worker_target,
            resource_capacity=snapshot.resource_capacity,
            population=snapshot.population,
            core_resource_reserve=EXPANSION_EARLY_RESERVE,
            base_worker_target=self._safety.config.worker_target,
            late_expansion_reserve=0,
        )
        if projected >= threshold:
            self._no_worker_deadlock_ticks = 0
            # A cell holds two entities and the Core occupies one slot, so an
            # idle worker standing on the Core cell makes SPAWN fail with
            # CELL_UNIT_LIMIT (production: t3 hammered 20 consecutive
            # CORE_SPAWN_FAILED). Movement resolves before the Core action, so
            # vacating the cell in the same tick lets the spawn land.
            unit_actions = _vacate_core_cell_actions(snapshot, plan.unit_actions)
            return Plan(
                tick=plan.tick,
                unit_actions=unit_actions,
                core_action=PlanningCoreAction(
                    type=CoreActionType.SPAWN,
                    unit_role=UnitRole.WORKER,
                ),
            )
        if workers == 0:
            # Deadlock: no worker is alive and the Core cannot afford another,
            # so income can never resume. Count the stuck ticks and, after the
            # grace window, self-destruct to force a respawn (fresh Core with
            # starting resources and a worker) instead of waiting forever.
            self._no_worker_deadlock_ticks += 1
            if self._no_worker_deadlock_ticks >= DEFAULT_NO_WORKER_DEADLOCK_TICKS:
                self._no_worker_deadlock_ticks = 0
                return Plan(
                    tick=plan.tick,
                    unit_actions=plan.unit_actions,
                    core_action=PlanningCoreAction(type=CoreActionType.SELF_DESTRUCT),
                )
        return Plan(
            tick=plan.tick,
            unit_actions=plan.unit_actions,
            core_action=PlanningCoreAction(type=CoreActionType.WAIT),
        )

    @staticmethod
    def _raid_tenant_id(snapshot: PlanningSnapshot) -> str:
        """Return a stable tenant id for squad formation."""

        return snapshot.core_id if snapshot.core_id else "arena"

    def _advance_stationary_cores(self, snapshot: PlanningSnapshot) -> dict[str, StationaryCore]:
        """Track non-unit enemy cells as candidate stationary enemy cores."""

        enemy_unit_cells = {unit.position.cell_key for unit in snapshot.enemy_units}
        stationary: dict[str, StationaryCore] = {}
        for key in snapshot.enemy_cells - enemy_unit_cells:
            previous = self._stationary_cores.get(key)
            observations = previous.observations + 1 if previous is not None else 1
            position = previous.position if previous is not None else parse_cell_key(key)
            stationary[key] = StationaryCore(key=key, position=position, observations=observations)
        self._stationary_cores = stationary
        return stationary

    def _reconcile_replacement_queue(self, snapshot: PlanningSnapshot) -> None:
        """Enqueue lost-unit roles and drain produced roles from the backlog."""

        current_by_unit = {unit.id.value: unit.unit_role.value for unit in snapshot.units}
        self._replacement_queue = reconcile_replacement_queue(
            self._previous_unit_roles,
            current_by_unit,
            self._replacement_queue,
        )
        self._previous_unit_roles = current_by_unit

    def _raid_target_confirmed(
        self,
        position: Coordinate,
        stationary: dict[str, StationaryCore],
    ) -> bool:
        """Return whether an active raid target still meets the confirmation bar."""

        candidate = stationary.get(position.cell_key)
        return (
            candidate is not None and candidate.observations >= self._config.raid_min_observations
        )

    def _raid_quota_hook(self, snapshot: PlanningSnapshot, plan: Plan) -> Plan:
        """Form squads and run the confirmed-raid lifecycle state machine."""

        self._reconcile_replacement_queue(snapshot)

        membership = reconcile_tactical_squads(
            snapshot.units,
            self._squad_by_unit or None,
            self._raid_tenant_id(snapshot),
        )
        self._squad_by_unit = dict(membership.squad_by_unit)
        home_squad = next(
            (squad for squad in membership.squads if squad.role == "HOME_DEFENSE"),
            None,
        )
        guard_ids = raid_guard_ids(home_squad)

        core = snapshot.core_position
        stationary = self._advance_stationary_cores(snapshot)
        confirmed: Coordinate | None = None
        if core is not None:
            confirmed = pick_raid_target(
                stationary,
                core,
                min_observations=self._config.raid_min_observations,
                max_distance=self._config.raid_max_distance,
            )

        fighter_count = sum(
            1 for unit in snapshot.units if unit.unit_role in (UnitRole.VANGUARD, UnitRole.RANGER)
        )
        if not raid_fighters_ready(fighter_count, min_fighters=self._config.raid_min_fighters):
            if raid_active(self._raid_state):
                self._raid_state = recall_raid(self._raid_state)
            return plan

        vanguard_ids = [
            unit.id.value
            for unit in snapshot.units
            if unit.unit_role is UnitRole.VANGUARD and unit.id.value not in guard_ids
        ]
        ranger_ids = [
            unit.id.value
            for unit in snapshot.units
            if unit.unit_role is UnitRole.RANGER and unit.id.value not in guard_ids
        ]
        quota = core_assault_quota(
            len(vanguard_ids),
            len(ranger_ids),
            home_vanguards=0,
            home_rangers=0,
        )

        state = self._raid_state

        # A confirmed raid target stays stable across ticks until it vanishes.
        if raid_active(state) and state.core_position is not None:
            if self._raid_target_confirmed(state.core_position, stationary):
                strike = select_strike_group(vanguard_ids, ranger_ids, quota)
                if strike.member_ids:
                    self._raid_state = replace(
                        state,
                        vanguard_ids=frozenset(strike.vanguard_ids),
                        ranger_ids=frozenset(strike.ranger_ids),
                    )
                    return _apply_raid_strike(plan, snapshot, state.core_position, strike)
            self._raid_state = clear_raid_target(
                replace(
                    state,
                    enabled=False,
                    recall=False,
                    vanguard_ids=frozenset(),
                    ranger_ids=frozenset(),
                )
            )
            return plan

        # Engage a freshly confirmed target.
        if confirmed is not None:
            strike = select_strike_group(vanguard_ids, ranger_ids, quota)
            if strike.member_ids:
                engaged = replace(
                    state,
                    enabled=True,
                    recall=False,
                    vanguard_ids=frozenset(strike.vanguard_ids),
                    ranger_ids=frozenset(strike.ranger_ids),
                )
                self._raid_state = acquire_raid_target(
                    engaged,
                    confirmed.cell_key,
                    confirmed,
                    snapshot.tick,
                )
                return _apply_raid_strike(plan, snapshot, confirmed, strike)

        self._raid_state = state
        return plan

    def _infer_blocked_cells(self, snapshot: PlanningSnapshot) -> frozenset[str]:
        """Infer permanent obstacles from the previous tick's move failures.

        When a worker planned a MOVE and the engine rejected it with
        ``MOVE_BLOCKED_TERRAIN``, the destination cell is terrain that was
        outside vision (or behind a route the pathfinder trusted). Pairing
        the failure with the previously planned direction yields the blocked
        cell; only the terrain reason is learned — unit-occupancy reasons
        (``MOVE_DESTINATION_OCCUPIED`` / ``CELL_UNIT_LIMIT``) are transient
        and must not become permanent terrain knowledge.
        """

        if not snapshot.move_failures:
            return frozenset()
        terrain_blocked_units = {
            failure.unit_id
            for failure in snapshot.move_failures
            if failure.reason == "MOVE_BLOCKED_TERRAIN"
        }
        if not terrain_blocked_units:
            return frozenset()
        inferred: set[str] = set()
        for unit in snapshot.units:
            if unit.unit_role is not UnitRole.WORKER:
                continue
            unit_id = unit.id.value
            if unit_id not in terrain_blocked_units:
                continue
            direction = self._previous_planned_directions.get(unit_id)
            if direction is None:
                continue
            inferred.add(unit.position.step(direction).cell_key)
        return frozenset(inferred)

    def decide_snapshot(self, snapshot: PlanningSnapshot) -> Plan:
        """Produce one merged plan for a planning snapshot (pure aside from state)."""

        # Obstacles are permanent terrain: accumulate them across ticks so
        # pathfinding never re-routes through a cell it has already seen
        # blocked, and learn the previous tick's MOVE_BLOCKED_TERRAIN
        # destinations (invisible obstacles the engine just rejected).
        inferred_blocked = self._infer_blocked_cells(snapshot)
        for key in sorted(inferred_blocked):
            self._terrain_map.record_blocked_move(parse_cell_key(key))
        accumulated_obstacles = self._terrain_map.observe(snapshot.obstacle_cells)
        if accumulated_obstacles != snapshot.obstacle_cells:
            snapshot = replace(snapshot, obstacle_cells=accumulated_obstacles)

        baseline = self._safety.decide(snapshot).plan
        blocked_cells = (
            self._stuck_blocked_cells(snapshot) if self._config.stuck_guard_enabled else frozenset()
        )

        escape_steps: dict[str, Direction] = {}
        pause_ids: frozenset[str] = frozenset()
        movement_core_action: PlanningCoreAction | None = None
        if self._config.movement_guard_enabled:
            blocked_cells, escape_steps, pause_ids, movement_core_action = (
                self._movement_guard_hook(snapshot, blocked_cells)
            )

        worker_config = self._config.worker_config
        if self._config.economy_expansion_enabled:
            worker_config = replace(
                worker_config,
                mission=replace(
                    worker_config.mission,
                    survey_worker_cap=EXPANSION_SURVEY_CAP,
                ),
            )

        exploration_targets: Mapping[str, Coordinate] | None = None
        if self._config.exploration_v2_enabled:
            worker_config = replace(
                worker_config,
                mission=replace(
                    worker_config.mission,
                    survey_worker_cap=max(
                        worker_config.mission.survey_worker_cap, EXPLORATION_SURVEY_CAP
                    ),
                    max_collection_distance=COLLECTION_MAX_DISTANCE,
                    # Hysteresis: switching to a different cell costs an extra
                    # 0.5 net value, damping deposit-return / re-assignment
                    # churn (the pure layer default is 0.0; the production
                    # path injects this like the other research knobs).
                    switch_threshold=HYSTERESIS_SWITCH_THRESHOLD,
                ),
                claim_preempt_penalty=CLAIM_PREEMPT_PENALTY,
            )
            observe_exploration(snapshot, self._previous_assignments, self._exploration_state)
            snapshot = with_memory_resource_cells(snapshot, self._exploration_state)
            # The permanent survey burst must not starve harvesting: when
            # collectable cells exist, shrink the pre-reserve cap to one so
            # the matrix keeps all but one worker harvesting (production:
            # with cap 3 and pop 2-4 only one harvester remained, so doubling
            # workers did not double income). Migration and respawn recovery
            # are exempt: a moving Core must scout wider, not harvest a dying
            # region's stale memory (production: blind migrations of 300-800
            # ticks with a single scout while other workers WAITed).
            in_migration_or_recovery = (
                self._barren_migration.migration_active or self._respawn_state.active
            )
            if snapshot.resource_cells and not in_migration_or_recovery:
                worker_config = replace(
                    worker_config,
                    mission=replace(
                        worker_config.mission,
                        survey_worker_cap=min(worker_config.mission.survey_worker_cap, 1),
                    ),
                )
            # During respawn in a resource-barren area OR active barren
            # migration, expand the exploration rings immediately (hunger
            # mode: 8/16/24/32/40 radii + 8-ring sweep) instead of waiting
            # HUNGER_TICKS (200) for the hunger clock to trip. This reaches
            # further out per tick so workers find resources faster (a
            # migrating Core with normal 10/20/30 rings walked blind for
            # 300-843 production ticks).
            respawn_barren = (
                self._config.respawn_recovery_enabled
                and self._respawn_state.active
                and not snapshot.resource_cells
            )
            migration_barren = (
                self._config.barren_migration_enabled
                and self._barren_migration.migration_active
            )
            exploration_targets = build_exploration_targets(
                snapshot,
                self._exploration_state,
                hungry=(
                    is_hungry(self._exploration_state, snapshot.tick)
                    or respawn_barren
                    or migration_barren
                ),
                barren=respawn_barren,
            )

        result = assign_worker_tasks(
            snapshot,
            self._previous_assignments,
            config=worker_config,
            survey_burst_active=self._config.survey_burst_active,
            claims=self._claims,
            blocked_cells=blocked_cells,
            exploration_targets=exploration_targets,
        )
        self._previous_assignments = result.plan.assignments
        self._claims = result.claims

        if self._config.exploration_v2_enabled:
            mark_reached(snapshot, result.plan.assignments, self._exploration_state)

        plan = merge_worker_tasks(
            baseline,
            result.plan.assignments,
            snapshot,
            route_aware=self._config.exploration_v2_enabled,
            trails=self._loop_trails,
        )

        if self._config.exploration_v2_enabled:
            # Stranded idle workers far from the Core are recalled home
            # (survivors of old migrations: production t3 workers idled 45-89
            # tiles away harvesting nothing while the Core sat elsewhere).
            plan = _recall_stranded_workers(snapshot, plan)

        if self._config.movement_guard_enabled:
            plan = _apply_movement_overrides(plan, escape_steps, pause_ids)
            if movement_core_action is not None:
                plan = Plan(
                    tick=plan.tick,
                    unit_actions=plan.unit_actions,
                    core_action=movement_core_action,
                )

        if self._config.exploration_v2_enabled:
            # Candidate D: hold cargo-less workers near home while an enemy is
            # at the door, so the economy stops feeding workers into the
            # attacker one by one (production t2 wipe: ~160-tick pickoff).
            plan = _worker_threat_sanctuary(snapshot, plan)

        if self._config.exploration_v2_enabled:
            # A cell holds two entities and the Core occupies one slot, so an
            # idle worker standing on the Core cell blocks deposits from other
            # workers AND any SPAWN (CELL_UNIT_LIMIT). Vacate it every tick as
            # the final word on unit actions — running it after the movement
            # guard matters because a loop-pause can re-WAIT a worker the
            # vacate just moved, and then the tenant burns 5 resources per
            # failed spawn (production t2: six SPAWN_FAILED CELL_UNIT_LIMIT ->
            # self-destruct -> spawn cycles).
            plan = Plan(
                tick=plan.tick,
                unit_actions=_vacate_core_cell_actions(snapshot, plan.unit_actions),
                core_action=plan.core_action,
            )

        if self._config.economy_budget_enabled:
            plan = self._economy_budget_hook(snapshot, plan)

        if self._config.economy_expansion_enabled:
            plan = self._economy_expansion_hook(snapshot, plan)

        if self._config.respawn_recovery_enabled:
            plan = self._respawn_recovery_hook(snapshot, plan)

        if self._config.barren_migration_enabled:
            plan = self._barren_migration_hook(snapshot, plan)

        if self._config.stuck_resources_enabled:
            plan = self._terrain_trap_hook(snapshot, plan)

        if self._config.raid_quota_enabled:
            plan = self._raid_quota_hook(snapshot, plan)

        if self._config.movement_guard_enabled:
            self._previous_move_actions = {
                action.unit_id.value: action.type is UnitActionType.MOVE
                for action in plan.unit_actions
            }
            self._previous_planned_directions = {
                action.unit_id.value: action.direction
                for action in plan.unit_actions
                if action.type is UnitActionType.MOVE and action.direction is not None
            }

        return plan


def compose_decider(config: ComposedDeciderConfig | None = None) -> Decider:
    """Build the deterministic live decider (composition root)."""

    return ComposedDecider(config)


__all__ = [
    "ComposedDecider",
    "ComposedDeciderConfig",
    "compose_decider",
    "merge_worker_tasks",
    "plan_to_decision",
    "snapshot_from_turn",
]
