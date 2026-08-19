"""Behavioral tests for the deterministic SafetyPlanner composition."""

from __future__ import annotations

from typing import cast

import pytest

from arena_hero_agent.domain import CURRENT_RULES_VERSION, Coordinate, EntityId, UnitRole
from arena_hero_agent.planning import (
    BeaconInfo,
    CoreActionType,
    EnemyUnit,
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
    beacon: BeaconInfo | None = None,
    enemy_units: tuple[EnemyUnit, ...] = (),
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
        enemy_units=enemy_units,
        core_id=None if core_position is None else "core",
        core_position=core_position,
        core_health=None if core_position is None else (core_health or 5),
        core_shield=core_shield,
        core_state=core_state,
        beacon=BeaconInfo(position=Coordinate(0, 0), status=None) if beacon is None else beacon,
        threat_map={},
    )


def _vanguard(x: int, y: int) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(f"v{x}_{y}"),
        unit_role=UnitRole.VANGUARD,
        position=Coordinate(x, y),
        health=4,
        cargo=0,
    )


def _ranger(x: int, y: int) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(f"r{x}_{y}"),
        unit_role=UnitRole.RANGER,
        position=Coordinate(x, y),
        health=2,
        cargo=0,
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
    assert (
        decision.plan.core_action is None
        or decision.plan.core_action.type is not CoreActionType.HEAL
    )


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


def test_vanguard_contests_ground_beacon_within_range() -> None:
    vanguard = _vanguard(3, 3)
    beacon = BeaconInfo(position=Coordinate(10, 10), status="ground", carrier_id=None)
    snapshot = _snapshot(
        units=(vanguard,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=beacon,
    )
    decision = SafetyPlanner().decide(snapshot)
    action = decision.plan.action_for(vanguard.id.value)
    assert action is not None
    assert action.type is UnitActionType.MOVE
    assert action.direction == step_toward(Coordinate(3, 3), Coordinate(10, 10))


def test_ranger_picks_up_ground_beacon_on_its_cell() -> None:
    ranger = _ranger(10, 10)
    beacon = BeaconInfo(position=Coordinate(10, 10), status="ground", carrier_id=None)
    snapshot = _snapshot(
        units=(ranger,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=beacon,
    )
    decision = SafetyPlanner().decide(snapshot)
    action = decision.plan.action_for(ranger.id.value)
    assert action is not None
    assert action.type is UnitActionType.PICKUP_BEACON


def test_beacon_contest_yields_to_visible_enemy() -> None:
    vanguard = _vanguard(3, 3)
    beacon = BeaconInfo(position=Coordinate(10, 10), status="ground", carrier_id=None)
    enemy = EnemyUnit(
        id=EntityId("enemy-1"),
        position=Coordinate(2, 3),
        unit_role=UnitRole.VANGUARD,
    )
    snapshot = _snapshot(
        units=(vanguard,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=beacon,
        enemy_units=(enemy,),
    )
    decision = SafetyPlanner().decide(snapshot)
    action = decision.plan.action_for(vanguard.id.value)
    assert action is not None
    assert action.type is not UnitActionType.PICKUP_BEACON
    # The adjacent enemy gets swept before anything else.
    assert action.type is UnitActionType.SWEEP


def test_beacon_carrier_returns_and_parks_next_to_core() -> None:
    vanguard = _vanguard(10, 10)
    beacon = BeaconInfo(
        position=Coordinate(10, 10),
        status="carried",
        carrier_id=vanguard.id,
    )
    snapshot = _snapshot(
        units=(vanguard,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=beacon,
    )
    decision = SafetyPlanner().decide(snapshot)
    action = decision.plan.action_for(vanguard.id.value)
    assert action is not None
    assert action.type is UnitActionType.MOVE

    parked = _vanguard(1, 0)
    snapshot = _snapshot(
        units=(parked,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=BeaconInfo(
            position=Coordinate(1, 0),
            status="carried",
            carrier_id=parked.id,
        ),
    )
    decision = SafetyPlanner().decide(snapshot)
    action = decision.plan.action_for(parked.id.value)
    assert action is not None
    assert action.type is UnitActionType.WAIT


def test_worker_carrier_deposits_cargo_before_parking() -> None:
    worker = _worker(3, 0, cargo=2)
    snapshot = _snapshot(
        units=(worker,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=BeaconInfo(
            position=Coordinate(3, 0),
            status="carried",
            carrier_id=worker.id,
        ),
    )
    decision = SafetyPlanner().decide(snapshot)
    action = decision.plan.action_for(worker.id.value)
    assert action is not None
    assert action.type is UnitActionType.MOVE
    assert action.direction == step_toward(Coordinate(3, 0), Coordinate(0, 0))


def test_core_repairs_shield_to_beacon_cap_when_held() -> None:
    carrier = _vanguard(1, 0)
    worker = _worker(0, 1)
    snapshot = _snapshot(
        units=(worker, carrier),
        resources=6,
        resource_capacity=100,
        population=2,
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_health=5,
        core_shield=5,
        beacon=BeaconInfo(
            position=Coordinate(1, 0),
            status="carried",
            carrier_id=carrier.id,
        ),
    )
    # worker_target 1: the next military spawn (10) is unaffordable at res 6,
    # so the idle Core repairs toward the Beacon shield cap 10.
    decision = SafetyPlanner(config=SafetyPlannerConfig(worker_target=1)).decide(snapshot)
    assert decision.plan.core_action is not None
    assert decision.plan.core_action.type is CoreActionType.REPAIR_SHIELD


def test_core_skips_beacon_shield_repair_when_not_held() -> None:
    worker = _worker(1, 0)
    snapshot = _snapshot(
        units=(worker,),
        resources=6,
        resource_capacity=100,
        population=1,
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_health=5,
        core_shield=5,
    )
    # Not holding the Beacon: shield 5 == cap 5, so nothing to repair.
    decision = SafetyPlanner(config=SafetyPlannerConfig(worker_target=1)).decide(snapshot)
    assert decision.plan.core_action is None


def test_threat_vanguard_waits_for_worker_floor() -> None:
    enemy = EnemyUnit(
        id=EntityId("enemy-1"),
        position=Coordinate(1, 0),
        unit_role=UnitRole.VANGUARD,
    )
    # Two workers: the economy comes first even under threat.
    snapshot = _snapshot(
        units=(_worker(0, 1), _worker(0, -1)),
        resources=15,
        resource_capacity=100,
        population=2,
        core_state="normal",
        core_position=Coordinate(0, 0),
        enemy_units=(enemy,),
    )
    decision = SafetyPlanner().decide(snapshot)
    assert decision.plan.core_action is not None
    assert decision.plan.core_action.unit_role is UnitRole.WORKER

    # Four workers and a fat bank: the threat Vanguard is bought.
    snapshot = _snapshot(
        units=(_worker(0, 1), _worker(0, -1), _worker(1, 0), _worker(-1, 0)),
        resources=16,
        resource_capacity=100,
        population=4,
        core_state="normal",
        core_position=Coordinate(0, 0),
        enemy_units=(enemy,),
    )
    decision = SafetyPlanner().decide(snapshot)
    assert decision.plan.core_action is not None
    assert decision.plan.core_action.type is CoreActionType.SPAWN
    assert decision.plan.core_action.unit_role is UnitRole.VANGUARD
