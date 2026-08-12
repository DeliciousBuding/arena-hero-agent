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

from dataclasses import dataclass
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
)
from arena_hero_agent.planning import (
    Assignment,
    CoreActionType,
    Plan,
    PlanningSnapshot,
    PlanningUnit,
    TaskType,
    UnitActionType,
    WorkerClaim,
    WorkerTaskPlannerConfig,
    assign_worker_tasks,
    extract_planning_snapshot,
)
from arena_hero_agent.planning import (
    CoreAction as PlanningCoreAction,
)
from arena_hero_agent.planning import (
    UnitAction as PlanningUnitAction,
)

from .safety_planner import SafetyPlanner, step_toward, worker_dense_direction
from .safety_planner_config import DEFAULT_SAFETY_CONFIG, SafetyPlannerConfig
from .stuck_guard import (
    DEFAULT_STUCK_GUARD_RADIUS,
    DEFAULT_STUCK_GUARD_TICKS,
    detect_stuck_unit_ids,
)
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
    survey_burst_active: bool = False
    stuck_guard_enabled: bool = False
    stuck_guard_ticks: int = DEFAULT_STUCK_GUARD_TICKS
    stuck_guard_radius: int = DEFAULT_STUCK_GUARD_RADIUS

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
        if isinstance(self.stuck_guard_radius, bool) or not isinstance(self.stuck_guard_radius, int):
            raise TypeError("stuck_guard_radius must be an integer")
        if self.stuck_guard_radius < 1:
            raise ValueError("stuck_guard_radius must be at least 1")


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


def _worker_ordinal(snapshot: PlanningSnapshot, unit: PlanningUnit) -> int:
    """Return the unit's ordinal among controlled workers in snapshot order."""

    index = 0
    for candidate in snapshot.units:
        if candidate.id == unit.id:
            return index
        if candidate.unit_role is UnitRole.WORKER:
            index += 1
    return index


def _task_action(assignment: Assignment, snapshot: PlanningSnapshot) -> PlanningUnitAction:
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
        return PlanningUnitAction(
            unit_id=unit.id,
            type=UnitActionType.MOVE,
            direction=step_toward(unit.position, task.target),
        )
    if task.type is TaskType.HARVEST_CURRENT:
        return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.HARVEST)
    if task.type is TaskType.PICKUP_BEACON:
        return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.PICKUP_BEACON)
    if task.type is TaskType.RETURN_FOR_HEAL:
        assert task.target is not None
        return PlanningUnitAction(
            unit_id=unit.id,
            type=UnitActionType.MOVE,
            direction=step_toward(unit.position, task.target),
        )
    if task.type is TaskType.GO_RESOURCE:
        assert task.target is not None
        if unit.position == task.target:
            return PlanningUnitAction(unit_id=unit.id, type=UnitActionType.HARVEST)
        return PlanningUnitAction(
            unit_id=unit.id,
            type=UnitActionType.MOVE,
            direction=step_toward(unit.position, task.target),
        )
    if task.type is TaskType.EXPLORE:
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
) -> Plan:
    """Override the baseline plan's worker actions with the assignment layer.

    WorkerTaskPlanner is the resource-task SSOT in the oracle; forced tasks and
    matrix/explore assignments replace the baseline safety actions for workers.
    """

    actions = {action.unit_id.value: action for action in plan.unit_actions}
    for assignment in assignments:
        actions[assignment.unit_id] = _task_action(assignment, snapshot)
    return Plan(
        tick=plan.tick,
        unit_actions=tuple(actions.values()),
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

    @property
    def config(self) -> ComposedDeciderConfig:
        return self._config

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

    def decide_snapshot(self, snapshot: PlanningSnapshot) -> Plan:
        """Produce one merged plan for a planning snapshot (pure aside from state)."""

        baseline = self._safety.decide(snapshot).plan
        blocked_cells = (
            self._stuck_blocked_cells(snapshot)
            if self._config.stuck_guard_enabled
            else frozenset()
        )
        result = assign_worker_tasks(
            snapshot,
            self._previous_assignments,
            config=self._config.worker_config,
            survey_burst_active=self._config.survey_burst_active,
            claims=self._claims,
            blocked_cells=blocked_cells,
        )
        self._previous_assignments = result.plan.assignments
        self._claims = result.claims
        return merge_worker_tasks(baseline, result.plan.assignments, snapshot)


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
