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
    manhattan,
    parse_cell_key,
    unit_price,
)
from arena_hero_agent.planning import (
    EXPLORATION_SURVEY_CAP,
    Assignment,
    CoreActionType,
    ExplorationState,
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
    next_step_toward,
    observe_exploration,
    with_memory_resource_cells,
)
from arena_hero_agent.planning import (
    CoreAction as PlanningCoreAction,
)
from arena_hero_agent.planning import (
    UnitAction as PlanningUnitAction,
)

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
    DEFAULT_DETECTION_DISTANCE,
    DEFAULT_RECOVERY_WORKERS,
    RespawnRecoveryState,
    detect_respawn,
)
from .safety_planner import SafetyPlanner, step_toward, worker_dense_direction
from .safety_planner_config import DEFAULT_SAFETY_CONFIG, SafetyPlannerConfig
from .stuck_guard import (
    DEFAULT_STUCK_GUARD_RADIUS,
    DEFAULT_STUCK_GUARD_TICKS,
    detect_stuck_unit_ids,
)
from .tactical_squads import reconcile_tactical_squads
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
    return extract_planning_snapshot(observation.projection, economy)


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


def _route_direction(
    unit: PlanningUnit,
    target: Coordinate,
    obstacles: frozenset[str],
) -> Direction:
    """Obstacle-aware first step toward ``target``, falling back to greedy.

    The assignment matrix prices routes with BFS distances, so the actual move
    must also be BFS-routed or workers repeatedly push into walls (MOVE_BLOCKED
    death-loop) whenever a mine or the Core is behind an obstacle.
    """

    direction = next_step_toward(unit.position, target, obstacles)
    return step_toward(unit.position, target) if direction is None else direction


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
) -> PlanningUnitAction:
    """Convert one deterministic worker task into a planning unit action.

    Forced-task conversion matches the P4-11 task-to-action contract; the
    GO_RESOURCE and EXPLORE conversions mirror the oracle's
    ``DeterministicPlanner`` (GO_RESOURCE steps toward the cell, EXPLORE uses a
    deterministic patrol direction).
    """

    unit = next(
        (candidate for candidate in snapshot.units if candidate.id.value == assignment.unit_id),
        None,
    )
    if unit is None:
        raise ValueError(f"worker assignment references unknown unit {assignment.unit_id!r}")
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
            _route_direction(unit, task.target, snapshot.obstacle_cells)
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
            _route_direction(unit, task.target, snapshot.obstacle_cells)
            if route_aware
            else step_toward(unit.position, task.target)
        )
        return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.MOVE, direction=direction)
    if task.type is TaskType.GO_RESOURCE:
        assert task.target is not None
        if unit.position == task.target:
            return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.HARVEST)
        direction = (
            _route_direction(unit, task.target, snapshot.obstacle_cells)
            if route_aware
            else step_toward(unit.position, task.target)
        )
        return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.MOVE, direction=direction)
    if task.type is TaskType.EXPLORE:
        if task.target is not None:
            if unit.position == task.target:
                return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.WAIT)
            direction = (
                _route_direction(unit, task.target, snapshot.obstacle_cells)
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
) -> Plan:
    """Override the baseline plan's worker actions with the assignment layer.

    WorkerTaskPlanner is the resource-task SSOT in the oracle; forced tasks and
    matrix/explore assignments replace the baseline safety actions for workers.
    """

    actions = {action.unit_id.value: action for action in plan.unit_actions}
    for assignment in assignments:
        actions[assignment.unit_id] = _task_action(assignment, snapshot, route_aware=route_aware)
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
            actions.append(
                PlanningUnitAction(
                    unit_id=action.unit_id,
                    type=UnitActionType.SHOOT,
                    expected_cell=target,
                )
            )
        elif unit_id in strike.vanguard_ids and unit.position != target:
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
        self._safety = SafetyPlanner(effective)
        self._previous_assignments: tuple[Assignment, ...] = ()
        self._claims: frozenset[WorkerClaim] = frozenset()
        self._position_history: dict[str, tuple[Coordinate, ...]] = {}
        self._loop_trails: dict[str, LoopTrail] = {}
        self._deposit_progress: dict[str, DepositProgress] = {}
        self._move_backoff: dict[str, MoveBackoffState] = {}
        self._previous_move_actions: dict[str, bool] = {}
        self._squad_by_unit: dict[str, str] = {}
        self._stationary_cores: dict[str, StationaryCore] = {}
        self._cargo_spin_history: dict[str, tuple[Coordinate, ...]] = {}
        self._raid_state = RaidState()
        self._replacement_queue = ReplacementQueue()
        self._previous_unit_roles: dict[str, str] = {}
        self._exploration_state = ExplorationState()
        self._respawn_state = RespawnRecoveryState()
        self._previous_core_position: Coordinate | None = None

    @property
    def config(self) -> ComposedDeciderConfig:
        return self._config

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
            if should_pause_move(backoff, tick=snapshot.tick):
                pause_ids.add(unit_id)

            trail = observe_loop_position(
                previous_trail,
                unit.position,
                window=self._config.movement_loop_window,
            )
            assignment = self._previous_assignment_for(unit_id)
            target = self._movement_target_for(assignment)
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
                    soft_obstacles_from_trail(trail, unit.position),
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
                        soft_obstacles_from_trail(trail, unit.position),
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
            self._previous_core_position = current_core

        if not self._config.respawn_recovery_enabled:
            return plan
        if not self._respawn_state.active:
            return plan
        if snapshot.core_state != "normal":
            # A moving (migrating) Core is handled by migration; do not fight it.
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
        workers = sum(1 for unit in snapshot.units if unit.unit_role is UnitRole.WORKER)
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

    def decide_snapshot(self, snapshot: PlanningSnapshot) -> Plan:
        """Produce one merged plan for a planning snapshot (pure aside from state)."""

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
                ),
            )
            observe_exploration(snapshot, self._previous_assignments, self._exploration_state)
            snapshot = with_memory_resource_cells(snapshot, self._exploration_state)
            exploration_targets = build_exploration_targets(
                snapshot,
                self._exploration_state,
                hungry=is_hungry(self._exploration_state, snapshot.tick),
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
        )

        if self._config.movement_guard_enabled:
            plan = _apply_movement_overrides(plan, escape_steps, pause_ids)
            if movement_core_action is not None:
                plan = Plan(
                    tick=plan.tick,
                    unit_actions=plan.unit_actions,
                    core_action=movement_core_action,
                )

        if self._config.economy_budget_enabled:
            plan = self._economy_budget_hook(snapshot, plan)

        if self._config.economy_expansion_enabled:
            plan = self._economy_expansion_hook(snapshot, plan)

        if self._config.respawn_recovery_enabled:
            plan = self._respawn_recovery_hook(snapshot, plan)

        if self._config.raid_quota_enabled:
            plan = self._raid_quota_hook(snapshot, plan)

        if self._config.movement_guard_enabled:
            self._previous_move_actions = {
                action.unit_id.value: action.type is UnitActionType.MOVE
                for action in plan.unit_actions
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
