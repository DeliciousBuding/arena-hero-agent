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


def test_beacon_contest_gate_blocks_below_population_threshold() -> None:
    vanguard = _vanguard(3, 3)
    beacon = BeaconInfo(position=Coordinate(10, 10), status="ground", carrier_id=None)
    snapshot = _snapshot(
        units=(vanguard,),
        resources=50,
        population=3,
        resource_capacity=100,
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=beacon,
    )
    planner = SafetyPlanner(
        SafetyPlannerConfig(
            beacon_contest_min_population=6,
            beacon_contest_min_resources=10,
        )
    )
    decision = planner.decide(snapshot)
    action = decision.plan.action_for(vanguard.id.value)
    assert action is not None
    # Gated out: the unit guards instead of walking toward the Beacon.
    assert action.type is not UnitActionType.PICKUP_BEACON
    if action.type is UnitActionType.MOVE:
        assert action.direction != step_toward(vanguard.position, beacon.position)


def test_beacon_contest_gate_blocks_below_resource_threshold() -> None:
    vanguard = _vanguard(3, 3)
    beacon = BeaconInfo(position=Coordinate(10, 10), status="ground", carrier_id=None)
    snapshot = _snapshot(
        units=(vanguard,),
        resources=4,
        population=8,
        resource_capacity=100,
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=beacon,
    )
    planner = SafetyPlanner(
        SafetyPlannerConfig(
            beacon_contest_min_population=6,
            beacon_contest_min_resources=10,
        )
    )
    decision = planner.decide(snapshot)
    action = decision.plan.action_for(vanguard.id.value)
    assert action is not None
    assert action.type is not UnitActionType.PICKUP_BEACON
    if action.type is UnitActionType.MOVE:
        assert action.direction != step_toward(vanguard.position, beacon.position)


def test_beacon_contest_gate_passes_when_economy_ready() -> None:
    vanguard = _vanguard(3, 3)
    beacon = BeaconInfo(position=Coordinate(10, 10), status="ground", carrier_id=None)
    snapshot = _snapshot(
        units=(vanguard,),
        resources=12,
        population=7,
        resource_capacity=100,
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=beacon,
    )
    planner = SafetyPlanner(
        SafetyPlannerConfig(
            beacon_contest_min_population=6,
            beacon_contest_min_resources=10,
        )
    )
    decision = planner.decide(snapshot)
    action = decision.plan.action_for(vanguard.id.value)
    assert action is not None
    assert action.type is UnitActionType.MOVE
    assert action.direction == step_toward(Coordinate(3, 3), Coordinate(10, 10))


def test_beacon_contest_only_one_contestant_per_tick() -> None:
    first = _vanguard(3, 3)
    second = _ranger(4, 4)
    beacon = BeaconInfo(position=Coordinate(10, 10), status="ground", carrier_id=None)
    snapshot = _snapshot(
        units=(first, second),
        resources=12,
        population=7,
        resource_capacity=100,
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=beacon,
    )
    decision = SafetyPlanner().decide(snapshot)
    first_action = decision.plan.action_for(first.id.value)
    second_action = decision.plan.action_for(second.id.value)
    assert first_action is not None
    assert second_action is not None
    # The first unit claims the contest; the second keeps guarding instead of
    # also walking toward the Beacon.
    assert first_action.type is UnitActionType.MOVE
    assert first_action.direction == step_toward(first.position, beacon.position)
    if second_action.type is UnitActionType.MOVE:
        assert second_action.direction != step_toward(second.position, beacon.position)


def test_beacon_contest_claim_resets_between_ticks() -> None:
    vanguard = _vanguard(3, 3)
    beacon = BeaconInfo(position=Coordinate(10, 10), status="ground", carrier_id=None)
    snapshot = _snapshot(
        units=(vanguard,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        beacon=beacon,
    )
    planner = SafetyPlanner()
    first = planner.decide(snapshot)
    second = planner.decide(snapshot)
    first_action = first.plan.action_for(vanguard.id.value)
    second_action = second.plan.action_for(vanguard.id.value)
    assert first_action is not None and second_action is not None
    # A fresh tick must allow the same unit to keep contesting.
    assert first_action.type is UnitActionType.MOVE
    assert second_action.type is UnitActionType.MOVE


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
        position=Coordinate(0, 4),
        unit_role=UnitRole.VANGUARD,
    )
    # Two workers with an enemy at distance 4 (beyond the imminent tier):
    # the economy comes first under elevated-but-not-imminent threat.
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


def test_ranger_predictive_fire_leads_out_of_range_enemy() -> None:
    ranger = _ranger(0, 0)
    enemy = EnemyUnit(
        id=EntityId("enemy-1"),
        position=Coordinate(4, 0),
        unit_role=UnitRole.VANGUARD,
    )
    snapshot = _snapshot(
        tick=1,
        units=(ranger,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        enemy_units=(enemy,),
    )
    planner = SafetyPlanner(config=SafetyPlannerConfig(ranger_predictive_fire=True))
    decision = planner.decide(snapshot)
    action = decision.plan.action_for(ranger.id.value)
    assert action is not None
    assert action.type is UnitActionType.SHOOT
    # Direct shot is out of range (distance 4 > 3); the shot leads the enemy
    # at its predicted next cell (4,0) -> (3,0).
    assert action.expected_cell == Coordinate(3, 0)


def test_ranger_direct_shot_beats_predictive() -> None:
    ranger = _ranger(0, 0)
    enemy = EnemyUnit(
        id=EntityId("enemy-1"),
        position=Coordinate(3, 0),
        unit_role=UnitRole.VANGUARD,
    )
    snapshot = _snapshot(
        tick=1,
        units=(ranger,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        enemy_units=(enemy,),
    )
    planner = SafetyPlanner(config=SafetyPlannerConfig(ranger_predictive_fire=True))
    decision = planner.decide(snapshot)
    action = decision.plan.action_for(ranger.id.value)
    assert action is not None
    assert action.type is UnitActionType.SHOOT
    assert action.expected_cell == Coordinate(3, 0)


def test_ranger_predictive_fire_disabled_by_default() -> None:
    ranger = _ranger(0, 0)
    enemy = EnemyUnit(
        id=EntityId("enemy-1"),
        position=Coordinate(4, 0),
        unit_role=UnitRole.VANGUARD,
    )
    snapshot = _snapshot(
        tick=1,
        units=(ranger,),
        core_state="normal",
        core_position=Coordinate(0, 0),
        enemy_units=(enemy,),
    )
    decision = SafetyPlanner().decide(snapshot)
    action = decision.plan.action_for(ranger.id.value)
    assert action is not None
    assert action.type is not UnitActionType.SHOOT


def test_ranger_predictive_miss_cap_triggers_cooldown() -> None:
    ranger = _ranger(0, 0)
    planner = SafetyPlanner(
        config=SafetyPlannerConfig(
            ranger_predictive_fire=True,
            ranger_predictive_miss_cap=3,
            ranger_predictive_cooldown_ticks=12,
        )
    )
    for tick in range(1, 5):
        enemy = EnemyUnit(
            id=EntityId("enemy-1"),
            position=Coordinate(4, 0),
            unit_role=UnitRole.VANGUARD,
        )
        snapshot = _snapshot(
            tick=tick,
            units=(ranger,),
            core_state="normal",
            core_position=Coordinate(0, 0),
            enemy_units=(enemy,),
        )
        decision = planner.decide(snapshot)
        action = decision.plan.action_for(ranger.id.value)
        assert action is not None
        if tick <= 3:
            assert action.type is UnitActionType.SHOOT, f"tick {tick}"
        else:
            # Three consecutive misses (enemy never moved) -> cooldown.
            assert action.type is not UnitActionType.SHOOT, f"tick {tick}"


def test_massarmy_stage_composition_switches_spawn_roles() -> None:
    workers = tuple(_worker(x, 0) for x in range(8))
    snapshot = _snapshot(
        tick=1,
        units=workers,
        resources=100,
        population=8,
        resource_capacity=100,
        core_state="normal",
        core_position=Coordinate(0, 0),
        core_shield=5,
    )
    flat = SafetyPlanner(config=SafetyPlannerConfig(worker_target=12)).decide(snapshot)
    staged = SafetyPlanner(config=SafetyPlannerConfig(massarmy_stages=True)).decide(snapshot)
    assert flat.plan.core_action is not None
    assert staged.plan.core_action is not None
    # Flat target 12: still building Workers. Stage 1 (8,1,1) is worker-
    # complete at 8, so the next spawn is a Vanguard.
    assert flat.plan.core_action.unit_role is UnitRole.WORKER
    assert staged.plan.core_action.unit_role is UnitRole.VANGUARD


def _converge_snapshots(
    *,
    workers_count: int = 2,
    resources: int = 10,
    population: int = 2,
    first_distance: int = 4,
    second_distance: int = 3,
) -> tuple:
    """Two consecutive ticks with an enemy moving closer to the Core."""

    workers = tuple(_worker(i, 0) for i in range(1, workers_count + 1))

    def snapshot_for(tick: int, distance: int) -> PlanningSnapshot:
        return _snapshot(
            tick=tick,
            units=workers,
            resources=resources,
            population=population,
            resource_capacity=100,
            core_state="normal",
            core_position=Coordinate(0, 0),
            core_shield=5,
            enemy_units=(
                EnemyUnit(
                    id=EntityId("enemy-1"),
                    position=Coordinate(0, distance),
                    unit_role=UnitRole.VANGUARD,
                ),
            ),
        )

    return snapshot_for(1, first_distance), snapshot_for(2, second_distance)


def test_imminent_threat_spawns_vanguard_below_tier1_floors() -> None:
    """Candidate C: a CONVERGING enemy inside the imminent distance with 2
    workers and the exact Vanguard price fields a defender even though the
    tier-1 floors (4 workers / 16 resources) are not met."""

    planner = SafetyPlanner()
    first, second = _converge_snapshots()
    planner.decide(first)
    decision = planner.decide(second)
    assert decision.plan.core_action is not None
    assert decision.plan.core_action.type is CoreActionType.SPAWN
    assert decision.plan.core_action.unit_role is UnitRole.VANGUARD


def test_imminent_threat_ignores_non_converging_enemy() -> None:
    """A wanderer holding distance 3 (no movement toward the Core) must not
    drain the economy."""

    planner = SafetyPlanner()
    first, second = _converge_snapshots(first_distance=3, second_distance=3)
    planner.decide(first)
    decision = planner.decide(second)
    action = decision.plan.core_action
    assert action is None or action.unit_role is not UnitRole.VANGUARD


def test_imminent_threat_needs_affordable_vanguard() -> None:
    planner = SafetyPlanner()
    first, second = _converge_snapshots(resources=9)
    planner.decide(first)
    decision = planner.decide(second)
    action = decision.plan.core_action
    # 9 < Vanguard price (10): no imminent defender, no regular spawn either.
    assert action is None or action.unit_role is not UnitRole.VANGUARD


def test_imminent_threat_requires_two_workers() -> None:
    planner = SafetyPlanner()
    first, second = _converge_snapshots(workers_count=1, resources=20, population=1)
    planner.decide(first)
    decision = planner.decide(second)
    action = decision.plan.core_action
    assert action is None or action.unit_role is not UnitRole.VANGUARD


def test_imminent_threat_requires_close_enemy() -> None:
    planner = SafetyPlanner()
    first, second = _converge_snapshots(first_distance=6, second_distance=5, resources=20)
    planner.decide(first)
    decision = planner.decide(second)
    action = decision.plan.core_action
    assert action is None or action.unit_role is not UnitRole.VANGUARD
