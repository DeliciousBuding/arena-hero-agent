"""Behavioral tests for the deterministic SafetyPlanner composition."""

from __future__ import annotations

from typing import cast

import pytest

from arena_hero_agent.domain import CURRENT_RULES_VERSION, Coordinate, EntityId, UnitRole
from arena_hero_agent.planning import (
    BeaconInfo,
    CoreActionType,
    PlanningSnapshot,
    PlanningUnit,
    UnitActionType,
)
from arena_hero_agent.strategies import (
    AGGRESSIVE_SAFETY_CONFIG,
    DEFAULT_SAFETY_CONFIG,
    SafetyPlanner,
    SafetyPlannerConfig,
    step_toward,
)

RULES = CURRENT_RULES_VERSION


def _snapshot(
    *,
    tick: int = 1,
    units: tuple[PlanningUnit, ...] = (),
    resources: int = 0,
    population: int = 0,
    resource_capacity: int = 10,
    core_state: str | None = None,
    core_position: Coordinate | None = None,
    core_health: int | None = None,
    core_shield: int | None = None,
    obstacle_cells: frozenset[str] = frozenset(),
) -> PlanningSnapshot:
    return PlanningSnapshot(
        tick=tick,
        rules_version=RULES,
        resources=resources,
        resource_capacity=resource_capacity,
        resource_space=resource_capacity - resources,
        population=population,
        units=units,
        resource_cells={},
        obstacle_cells=obstacle_cells,
        enemy_cells=frozenset(),
        enemy_units=(),
        core_id=None if core_position is None else "core",
        core_position=core_position,
        core_health=None if core_position is None else (core_health or 5),
        core_shield=core_shield,
        core_state=core_state,
        beacon=BeaconInfo(position=Coordinate(0, 0), status=None),
        threat_map={},
    )


def _worker(x: int, y: int, *, cargo: int = 0, health: int = 2) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(f"w{x}_{y}"),
        unit_role=UnitRole.WORKER,
        position=Coordinate(x, y),
        health=health,
        cargo=cargo,
    )


def test_empty_world_waits_without_core_action() -> None:
    planner = SafetyPlanner()
    snapshot = _snapshot()
    decision = planner.decide(snapshot)
    assert decision.plan.tick == 1
    assert decision.plan.unit_actions == ()
    assert decision.plan.core_action is None
    assert not decision.budget_exhausted
    assert decision.computed_actions == 0


def test_worker_forced_deposit_produces_move_toward_core() -> None:
    planner = SafetyPlanner()
    worker = _worker(2, 2, cargo=1)
    snapshot = _snapshot(
        units=(worker,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_shield=5,
    )
    decision = planner.decide(snapshot)
    action = decision.plan.action_for(worker.id.value)
    assert action is not None
    assert action.type is UnitActionType.MOVE
    assert step_toward(Coordinate(2, 2), Coordinate(0, 0)) == action.direction


def test_worker_on_core_with_cargo_deposits() -> None:
    planner = SafetyPlanner()
    worker = _worker(0, 0, cargo=1)
    snapshot = _snapshot(
        units=(worker,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_shield=5,
    )
    decision = planner.decide(snapshot)
    action = decision.plan.action_for(worker.id.value)
    assert action is not None
    assert action.type is UnitActionType.DEPOSIT


def test_planner_is_deterministic() -> None:
    planner = SafetyPlanner()
    snapshot = _snapshot(
        units=(_worker(1, 0, cargo=1), _worker(3, 3)),
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_shield=5,
    )
    first = planner.decide(snapshot)
    second = planner.decide(snapshot)
    assert first.plan == second.plan
    assert first.computed_actions == second.computed_actions
    assert first.budget_exhausted == second.budget_exhausted


def test_budget_zero_forces_wait_and_marks_exhausted() -> None:
    planner = SafetyPlanner()
    snapshot = _snapshot(
        units=(_worker(1, 0, cargo=1), _worker(3, 3)),
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_shield=5,
    )
    decision = planner.decide(snapshot, budget=0)
    assert decision.budget_exhausted
    assert decision.computed_actions == 0
    assert all(action.type is UnitActionType.WAIT for action in decision.plan.unit_actions)


def test_budget_bounds_computation() -> None:
    planner = SafetyPlanner()
    snapshot = _snapshot(
        units=(_worker(1, 0, cargo=1), _worker(3, 3)),
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_shield=5,
    )
    decision = planner.decide(snapshot, budget=1)
    assert decision.computed_actions == 1
    assert decision.budget_exhausted
    actions = list(decision.plan.unit_actions)
    assert len(actions) == 2
    assert any(action.type is UnitActionType.WAIT for action in actions)


def test_core_spawns_worker_when_affordable() -> None:
    planner = SafetyPlanner(config=SafetyPlannerConfig(worker_target=8))
    snapshot = _snapshot(
        units=(_worker(1, 0),),
        resources=100,
        resource_capacity=100,
        population=1,
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_shield=5,
    )
    decision = planner.decide(snapshot)
    assert decision.plan.core_action is not None
    assert decision.plan.core_action.type is CoreActionType.SPAWN
    assert decision.plan.core_action.unit_role is UnitRole.WORKER


def test_core_does_not_spawn_when_unaffordable() -> None:
    planner = SafetyPlanner(config=SafetyPlannerConfig(worker_target=8))
    snapshot = _snapshot(
        units=(_worker(1, 0),),
        resources=0,
        population=1,
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_shield=5,
    )
    decision = planner.decide(snapshot)
    assert decision.plan.core_action is None


def test_core_heals_when_critically_damaged() -> None:
    planner = SafetyPlanner()
    snapshot = _snapshot(
        units=(_worker(1, 0),),
        resources=9,
        resource_capacity=100,
        population=1,
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_health=2,
        core_shield=5,
    )
    decision = planner.decide(snapshot)
    assert decision.plan.core_action is not None
    assert decision.plan.core_action.type is CoreActionType.HEAL


def test_core_skips_heal_when_reserve_too_low() -> None:
    planner = SafetyPlanner()
    snapshot = _snapshot(
        units=(_worker(1, 0),),
        resources=6,
        resource_capacity=100,
        population=1,
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_health=1,
        core_shield=5,
    )
    decision = planner.decide(snapshot)
    # 6 < CRITICAL_HEAL_MIN_RESOURCES (7): heal must not drain the economy.
    assert decision.plan.core_action is None or decision.plan.core_action.type is not CoreActionType.HEAL


def test_core_repairs_shield_when_idle_and_full_hp() -> None:
    planner = SafetyPlanner(config=SafetyPlannerConfig(worker_target=1))
    snapshot = _snapshot(
        units=(_worker(1, 0),),
        resources=6,
        resource_capacity=100,
        population=1,
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_health=5,
        core_shield=3,
    )
    decision = planner.decide(snapshot)
    assert decision.plan.core_action is not None
    assert decision.plan.core_action.type is CoreActionType.REPAIR_SHIELD


def test_planner_rejects_invalid_config_and_inputs() -> None:
    with pytest.raises(TypeError):
        SafetyPlanner(config=cast(SafetyPlannerConfig, "config"))
    with pytest.raises(ValueError):
        SafetyPlannerConfig(worker_target=-1)
    with pytest.raises(ValueError):
        SafetyPlannerConfig(vanguard_ratio=2.0)
    planner = SafetyPlanner()
    with pytest.raises(TypeError):
        planner.decide(cast(PlanningSnapshot, None))
    with pytest.raises(ValueError):
        planner.decide(_snapshot(), budget=-1)


def test_aggressive_config_is_distinct_and_valid() -> None:
    assert AGGRESSIVE_SAFETY_CONFIG is not DEFAULT_SAFETY_CONFIG
    planner = SafetyPlanner(config=AGGRESSIVE_SAFETY_CONFIG)
    decision = planner.decide(_snapshot())
    assert decision.plan.unit_actions == ()
