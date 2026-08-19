"""Deterministic safety planner (P4-11 core) and direction helpers.

The legacy TypeScript ``SafetyPlanner`` is stateful and spans thousands of lines.
This Python core is the deterministic, fail-closed composition of the migrated
helper behaviors:

- workers follow the forced-task contract (``forced_task_for``) and the legacy
  task-to-action conversion (DEPOSIT returns home, HARVEST_CURRENT harvests,
  PICKUP_BEACON picks up, RETURN_FOR_HEAL walks home);
- vanguards sweep adjacent hostiles, otherwise guard from a defense post;
- rangers shoot the nearest defensive-priority hostile in line of sight,
  otherwise guard;
- the Core spawns the next unit when affordable.

The composition is intentionally simpler than the stateful oracle and is
registered as an ALLOWED_DIFFERENCE in the behavior-difference registry; every
helper it calls is fixture-compared against the TypeScript oracle. A deterministic
unit-action budget bounds the work and falls back to safe WAIT actions when
exhausted, so the planner is always bounded and reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key
from typing import Final

from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    Coordinate,
    Direction,
    EntityId,
    UnitRole,
    assert_current_rules_version,
    direction_to_adjacent,
    manhattan,
    unit_price,
)

from ..planning.plan import CoreAction, CoreActionType, Plan, UnitAction, UnitActionType
from ..planning.planning_snapshot import PlanningSnapshot, PlanningUnit
from ..planning.task import TaskType, forced_task_for
from .safety_helpers import (
    VisibleEnemy,
    can_shoot,
    defense_post,
    defensive_shot_priority,
    home_cell,
    next_spawn,
)
from .safety_planner_config import DEFAULT_SAFETY_CONFIG, SafetyPlannerConfig

EXPLORE_DIRECTION_COUNT = 8

# Official core attributes (rules/core-and-economy.md): HP 5, shield 5.
CORE_MAX_HP: Final = 5
CORE_MAX_SHIELD: Final = 5
# Heal once at or below this HP, keeping enough resources for the next Worker
# (base price 5) plus a small margin after HEAL resolves.
CRITICAL_CORE_HP: Final = 2
CRITICAL_HEAL_MIN_RESOURCES: Final = 7
# One shield point costs exactly one resource; repair only when idle at full HP.
SHIELD_REPAIR_MIN_RESOURCES: Final = 6


def worker_dense_direction(index: int) -> int:
    """Map a worker ordinal to a dense 16-direction scan slot."""

    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("index must be an integer")
    if index < 0:
        raise ValueError("index cannot be negative")
    if index < 8:
        return ((index * 3 + 7) % 8) * 2
    return (((index - 8) * 3 + 7) % 8) * 2 + 1


def threat_weighted_direction(index: int, threat_sector: int | None) -> int:
    """Weight the first four patrol directions toward the threat sector."""

    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("index must be an integer")
    if index < 0:
        raise ValueError("index cannot be negative")
    if threat_sector is not None and (
        isinstance(threat_sector, bool) or not isinstance(threat_sector, int)
    ):
        raise TypeError("threat_sector must be an integer or None")
    spread = (index * 3 + 7) % EXPLORE_DIRECTION_COUNT
    if threat_sector is None:
        return spread
    if index < 4:
        offset = 0 if index == 0 else 1 if index == 1 else -1 if index == 2 else 2
        return (threat_sector + offset + EXPLORE_DIRECTION_COUNT) % EXPLORE_DIRECTION_COUNT
    return spread


def step_toward(from_position: Coordinate, target: Coordinate) -> Direction:
    """Deterministic one step toward a target: x axis first, then y."""

    if not isinstance(from_position, Coordinate) or not isinstance(target, Coordinate):
        raise TypeError("from_position and target must be Coordinate values")
    dx = target.x - from_position.x
    dy = target.y - from_position.y
    if dx != 0:
        return Direction.EAST if dx > 0 else Direction.WEST
    return Direction.SOUTH if dy > 0 else Direction.NORTH


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """One deterministic safety plan plus its budget accounting."""

    __canonical_name__ = "arena-hero.safety-decision.v1"

    plan: Plan
    computed_actions: int
    budget_exhausted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.plan, Plan):
            raise TypeError("plan must be a Plan")
        if isinstance(self.computed_actions, bool) or not isinstance(self.computed_actions, int):
            raise TypeError("computed_actions must be an integer")
        if self.computed_actions < 0:
            raise ValueError("computed_actions cannot be negative")
        if not isinstance(self.budget_exhausted, bool):
            raise TypeError("budget_exhausted must be a boolean")


class SafetyPlanner:
    """Deterministic, budget-bounded safety planner for one observed tick."""

    def __init__(self, config: SafetyPlannerConfig = DEFAULT_SAFETY_CONFIG) -> None:
        if not isinstance(config, SafetyPlannerConfig):
            raise TypeError("config must be a SafetyPlannerConfig")
        self._config = config

    @property
    def config(self) -> SafetyPlannerConfig:
        return self._config

    def decide(
        self,
        snapshot: PlanningSnapshot,
        *,
        budget: int | None = None,
    ) -> SafetyDecision:
        """Produce the safety plan for one snapshot, bounded by an action budget."""

        if not isinstance(snapshot, PlanningSnapshot):
            raise TypeError("snapshot must be a PlanningSnapshot")
        assert_current_rules_version(snapshot.rules_version)
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int)):
            raise TypeError("budget must be an integer or None")
        if budget is not None and budget < 0:
            raise ValueError("budget cannot be negative")

        core_action = self._decide_core(snapshot)
        unit_actions: list[UnitAction] = []
        computed = 0
        budget_exhausted = False
        for unit in snapshot.units:
            if budget is not None and computed >= budget:
                budget_exhausted = True
                unit_actions.append(_wait_action(unit))
                continue
            action = self._decide_unit(snapshot, unit)
            unit_actions.append(action)
            computed += 1

        return SafetyDecision(
            plan=Plan(
                tick=snapshot.tick, unit_actions=tuple(unit_actions), core_action=core_action
            ),
            computed_actions=computed,
            budget_exhausted=budget_exhausted,
        )

    def _decide_core(self, snapshot: PlanningSnapshot) -> CoreAction | None:
        if snapshot.core_state != "normal":
            return None
        workers = sum(1 for unit in snapshot.units if unit.unit_role is UnitRole.WORKER)
        vanguards = sum(1 for unit in snapshot.units if unit.unit_role is UnitRole.VANGUARD)
        rangers = sum(1 for unit in snapshot.units if unit.unit_role is UnitRole.RANGER)

        # Survival first: a critically damaged Core heals before any other
        # spending. HEAL auto-continues until full HP or empty resources, so
        # gate it on a reserve that keeps the next Worker affordable
        # (production: t3 took CORE_DAMAGED 10 times in 400 ticks while never
        # healing; a 1-resource-per-HP repair is the cheapest survival layer).
        core_health = snapshot.core_health
        if (
            core_health is not None
            and core_health <= CRITICAL_CORE_HP
            and snapshot.resources >= CRITICAL_HEAL_MIN_RESOURCES
        ):
            return CoreAction(type=CoreActionType.HEAL)

        role = next_spawn(workers, vanguards, rangers, self._config.worker_target, self._config)
        cost = unit_price(role, snapshot.population, CURRENT_RULES_VERSION)
        if snapshot.resources >= cost:
            return CoreAction(type=CoreActionType.SPAWN, unit_role=role)

        # Shield repair only when idle and at full HP: one resource per shield
        # point, keeping the Core at max defense before the next raid window.
        core_shield = snapshot.core_shield
        if (
            core_health == CORE_MAX_HP
            and core_shield is not None
            and core_shield < CORE_MAX_SHIELD
            and snapshot.resources >= SHIELD_REPAIR_MIN_RESOURCES
        ):
            return CoreAction(type=CoreActionType.REPAIR_SHIELD)
        return None

    def _decide_unit(self, snapshot: PlanningSnapshot, unit: PlanningUnit) -> UnitAction:
        if unit.unit_role is UnitRole.WORKER:
            return self._decide_worker(snapshot, unit)
        if unit.unit_role is UnitRole.VANGUARD:
            return self._decide_vanguard(snapshot, unit)
        if unit.unit_role is UnitRole.RANGER:
            return self._decide_ranger(snapshot, unit)
        return _wait_action(unit)

    def _decide_worker(self, snapshot: PlanningSnapshot, unit: PlanningUnit) -> UnitAction:
        task = forced_task_for(unit, snapshot)
        if task is None:
            return _wait_action(unit)
        if task.type is TaskType.DEPOSIT:
            assert task.target is not None
            if unit.position == snapshot.core_position:
                return UnitAction(unit_id=unit.id, type=UnitActionType.DEPOSIT)
            return UnitAction(
                unit_id=unit.id,
                type=UnitActionType.MOVE,
                direction=step_toward(unit.position, task.target),
            )
        if task.type is TaskType.HARVEST_CURRENT:
            return UnitAction(unit_id=unit.id, type=UnitActionType.HARVEST)
        if task.type is TaskType.PICKUP_BEACON:
            return UnitAction(unit_id=unit.id, type=UnitActionType.PICKUP_BEACON)
        if task.type is TaskType.RETURN_FOR_HEAL:
            assert task.target is not None
            return UnitAction(
                unit_id=unit.id,
                type=UnitActionType.MOVE,
                direction=step_toward(unit.position, task.target),
            )
        return _wait_action(unit)

    def _decide_vanguard(self, snapshot: PlanningSnapshot, unit: PlanningUnit) -> UnitAction:
        enemies = _visible_enemies(snapshot)
        for enemy in enemies:
            if manhattan(unit.position, enemy.position) == 1:
                return UnitAction(
                    unit_id=unit.id,
                    type=UnitActionType.SWEEP,
                    direction=direction_to_adjacent(unit.position, enemy.position),
                )
        return _guard_action(snapshot, unit, enemies, UnitRole.VANGUARD)

    def _decide_ranger(self, snapshot: PlanningSnapshot, unit: PlanningUnit) -> UnitAction:
        enemies = _visible_enemies(snapshot)
        ordered = sorted(
            enemies,
            key=cmp_to_key(lambda left, right: defensive_shot_priority(unit.position, left, right)),
        )
        for enemy in ordered:
            if can_shoot(unit.position, enemy.position, snapshot.obstacle_cells):
                return UnitAction(
                    unit_id=unit.id,
                    type=UnitActionType.SHOOT,
                    target_id=EntityId(enemy.id),
                    expected_cell=enemy.position,
                )
        return _guard_action(snapshot, unit, enemies, UnitRole.RANGER)


def _visible_enemies(snapshot: PlanningSnapshot) -> tuple[VisibleEnemy, ...]:
    return tuple(
        VisibleEnemy(
            id=enemy.id.value,
            position=enemy.position,
            kind="UNIT",
            unit_role=enemy.unit_role,
        )
        for enemy in snapshot.enemy_units
    )


def _guard_action(
    snapshot: PlanningSnapshot,
    unit: PlanningUnit,
    enemies: tuple[VisibleEnemy, ...],
    role: UnitRole,
) -> UnitAction:
    if snapshot.core_position is None:
        return _wait_action(unit)
    index = _military_index(snapshot, unit)
    post = defense_post(
        snapshot.core_position,
        enemies,
        snapshot.obstacle_cells,
        role,
        index,
    )
    if post is None:
        post = home_cell(snapshot.core_position, snapshot.obstacle_cells, index)
    if post is None or unit.position == post:
        return _wait_action(unit)
    return UnitAction(
        unit_id=unit.id,
        type=UnitActionType.MOVE,
        direction=step_toward(unit.position, post),
    )


def _military_index(snapshot: PlanningSnapshot, unit: PlanningUnit) -> int:
    index = 0
    for candidate in snapshot.units:
        if candidate.id == unit.id:
            return index
        if candidate.unit_role is not UnitRole.WORKER:
            index += 1
    return index


def _wait_action(unit: PlanningUnit) -> UnitAction:
    return UnitAction(unit_id=unit.id, type=UnitActionType.WAIT)
