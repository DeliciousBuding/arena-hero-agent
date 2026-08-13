"""Composed decider tests: TS-known worker snapshots, projection, and DTO mapping.

The composed decider (P4-21) runs the fixture-compared P4-11 safety planner and
P4-12 worker assignment layers in the oracle's order and converts the merged
plan into the application ``Decision`` DTO. Worker tasks in this file come from
the pinned TypeScript oracle capture (``known_answers_v1.json`` at 8cf5cbb);
the concrete action expectations pin the deterministic task-to-action
conversion of the composition. The composition itself is registered as
``planner_composition`` ALLOWED_DIFFERENCE (see docs/planning-differences.md).
"""

from __future__ import annotations

from typing import Any

import pytest

from arena_hero_agent.application import CoreAction, PlayerLifecycle, TurnObservation
from arena_hero_agent.application.turns import UnitAction
from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    BeaconObservation,
    BeaconStatus,
    Coordinate,
    DeadlineBudget,
    Direction,
    EntityId,
    UnitRole,
    WorldProjection,
)
from arena_hero_agent.planning import (
    Assignment,
    BeaconInfo,
    EnemyUnit,
    MissionConfig,
    Plan,
    PlanningSnapshot,
    PlanningUnit,
    ResourceCellInfo,
    Task,
    TaskType,
    UnitActionType,
    WorkerTaskPlannerConfig,
)
from arena_hero_agent.strategies import (
    ComposedDecider,
    ComposedDeciderConfig,
    compose_decider,
    merge_worker_tasks,
    plan_to_decision,
    snapshot_from_turn,
)
from tests.strategies.fixture_loader import load_oracle_fixture

RULES = CURRENT_RULES_VERSION

_MISSION_KEYS = {
    "collectionValueFloor": "collection_value_floor",
    "maxCollectionDistance": "max_collection_distance",
    "surveyWorkerCap": "survey_worker_cap",
    "surveyBurstTicks": "survey_burst_ticks",
    "surveyWorkerFloor": "survey_worker_floor",
    "visibleBonus": "visible_bonus",
    "seedAgeDecay": "seed_age_decay",
    "refillLookahead": "refill_lookahead",
    "refillBonus": "refill_bonus",
    "deadMineOverdueTicks": "dead_mine_overdue_ticks",
    "migrationScout": "migration_scout",
    "alwaysSurvey": "always_survey",
    "switchThreshold": "switch_threshold",
    "surveyOnSupplyGap": "survey_on_supply_gap",
}


def _coordinate(value: list[int]) -> Coordinate:
    return Coordinate(value[0], value[1])


def _role(name: str) -> UnitRole:
    return {"WORKER": UnitRole.WORKER, "VANGUARD": UnitRole.VANGUARD, "RANGER": UnitRole.RANGER}[
        name
    ]


def _unit(record: dict[str, Any]) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(record["id"]),
        unit_role=_role(record["unitType"]),
        position=_coordinate(record["position"]),
        health=record["hp"],
        cargo=record["cargo"],
    )


def _mission(record: dict[str, Any] | None) -> MissionConfig:
    if not record:
        return MissionConfig()
    return MissionConfig(**{_MISSION_KEYS.get(key, key): value for key, value in record.items()})


def _fixture_snapshot(record: dict[str, Any]) -> PlanningSnapshot:
    resource_cells = {
        key: ResourceCellInfo(
            position=_coordinate(info["position"]),
            visible=info.get("visible", False),
            last_seen_tick=info.get("lastSeenTick"),
            seeded=info.get("seeded", False),
        )
        for key, info in record["resourceCells"].items()
    }
    beacon = record["beacon"]
    core = record.get("corePosition")
    return PlanningSnapshot(
        tick=record["tick"],
        rules_version=RULES,
        resources=record["resources"],
        resource_capacity=record["resourceCapacity"],
        resource_space=record["resourceSpace"],
        population=record["population"],
        units=tuple(_unit(unit) for unit in record["units"]),
        resource_cells=resource_cells,
        obstacle_cells=frozenset(record["obstacleCells"]),
        enemy_cells=frozenset(record["enemyCells"]),
        enemy_units=tuple(
            EnemyUnit(
                id=EntityId(enemy["id"]),
                position=_coordinate(enemy["position"]),
                unit_role=_role(enemy["unitType"]),
            )
            for enemy in record["enemyUnits"]
        ),
        core_id="core" if core is not None else None,
        core_position=None if core is None else _coordinate(core),
        core_health=record.get("coreHp"),
        core_shield=5 if core is not None else None,
        core_state=None if core is None else record.get("coreState", "NORMAL").lower(),
        beacon=BeaconInfo(
            position=_coordinate(beacon["position"]),
            status=None if beacon["status"] is None else beacon["status"].lower(),
            carrier_id=(None if beacon.get("carrierId") is None else EntityId(beacon["carrierId"])),
        ),
        threat_map=dict(record["threatMap"]),
    )


def _worker_snapshot(
    *,
    units: tuple[PlanningUnit, ...],
    resources: int = 0,
    population: int = 0,
    resource_capacity: int = 100,
    resource_cells: dict[str, ResourceCellInfo] | None = None,
    core_position: Coordinate | None = None,
) -> PlanningSnapshot:
    return PlanningSnapshot(
        tick=1,
        rules_version=RULES,
        resources=resources,
        resource_capacity=resource_capacity,
        resource_space=resource_capacity - resources,
        population=population,
        units=units,
        resource_cells={} if resource_cells is None else resource_cells,
        obstacle_cells=frozenset(),
        enemy_cells=frozenset(),
        enemy_units=(),
        core_id=None if core_position is None else "core",
        core_position=core_position,
        core_health=None if core_position is None else 5,
        core_shield=None if core_position is None else 5,
        core_state=None if core_position is None else "normal",
        beacon=BeaconInfo(position=Coordinate(0, 0), status=None),
        threat_map={},
    )


def _worker(identifier: str, x: int, y: int, *, cargo: int = 0) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(identifier),
        unit_role=UnitRole.WORKER,
        position=Coordinate(x, y),
        health=2,
        cargo=cargo,
    )


def _action(plan: Plan, unit_id: str) -> UnitActionType | None:
    action = plan.action_for(unit_id)
    return None if action is None else action.type


def _direction(plan: Plan, unit_id: str) -> Direction | None:
    action = plan.action_for(unit_id)
    return None if action is None or action.direction is None else action.direction


def test_compose_decider_returns_decider_protocol_callable() -> None:
    decider = compose_decider()
    assert callable(decider)
    assert isinstance(decider, ComposedDecider)
    assert decider.config == ComposedDeciderConfig()


def test_worker_assignment_known_answers_convert_to_actions() -> None:
    fixture = load_oracle_fixture()
    cases = {
        "single_worker_single_cell",
        "cargo_worker_forced_deposit",
        "worker_on_visible_cell_forced_harvest",
        "beacon_on_worker_cell_forced_pickup",
        "two_workers_one_cell",
        "no_candidate_cells_all_workers_wait",
    }
    expected_actions = {
        # TS task -> deterministic conversion pinned by this test.
        "single_worker_single_cell": {"w1": (UnitActionType.MOVE, "east")},
        "cargo_worker_forced_deposit": {"w1": (UnitActionType.MOVE, "west")},
        "worker_on_visible_cell_forced_harvest": {"w1": (UnitActionType.HARVEST, None)},
        "beacon_on_worker_cell_forced_pickup": {"w1": (UnitActionType.PICKUP_BEACON, None)},
        "two_workers_one_cell": {
            "w1": (UnitActionType.MOVE, "east"),
            "w2": (UnitActionType.WAIT, None),
        },
        "no_candidate_cells_all_workers_wait": {
            "w1": (UnitActionType.WAIT, None),
            "w2": (UnitActionType.WAIT, None),
        },
    }
    for case in fixture["worker_assignments"]:
        if case["name"] not in cases:
            continue
        snapshot = _fixture_snapshot(case["snapshot"])
        plan = ComposedDecider().decide_snapshot(snapshot)
        for unit_id, (action_type, direction) in expected_actions[case["name"]].items():
            assert _action(plan, unit_id) is action_type, case["name"]
            got_direction = _direction(plan, unit_id)
            expected_direction = None if direction is None else direction
            if expected_direction is None:
                assert got_direction is None, case["name"]
            else:
                assert got_direction is not None
                assert got_direction.value == expected_direction, case["name"]


def test_survey_burst_pre_reserve_pins_explore_patrol() -> None:
    fixture = load_oracle_fixture()
    case = next(
        item for item in fixture["worker_assignments"] if item["name"] == "survey_burst_pre_reserve"
    )
    config = ComposedDeciderConfig(
        worker_config=WorkerTaskPlannerConfig(mission=_mission(case.get("mission"))),
        survey_burst_active=True,
    )
    plan = ComposedDecider(config).decide_snapshot(_fixture_snapshot(case["snapshot"]))
    assert _action(plan, "w1") is UnitActionType.MOVE
    direction = _direction(plan, "w1")
    assert direction is not None and direction.value == "east"
    assert _action(plan, "w2") is UnitActionType.MOVE
    direction = _direction(plan, "w2")
    assert direction is not None and direction.value == "south"
    assert _action(plan, "w3") is UnitActionType.MOVE
    direction = _direction(plan, "w3")
    assert direction is not None and direction.value == "west"
    assert _action(plan, "w4") is UnitActionType.MOVE
    direction = _direction(plan, "w4")
    assert direction is not None and direction.value == "east"


def test_two_tick_claim_sequence_is_sticky() -> None:
    fixture = load_oracle_fixture()
    case = next(
        item
        for item in fixture["worker_assignments"]
        if item["name"] == "claim_keeps_cell_two_ticks"
    )
    decider = ComposedDecider()
    for tick_record in case["ticks"]:
        plan = decider.decide_snapshot(_fixture_snapshot(tick_record))
        assert _action(plan, "w1") is UnitActionType.MOVE
        direction = _direction(plan, "w1")
        assert direction is not None and direction.value == "east"
        assert _action(plan, "w2") is UnitActionType.WAIT


def test_composed_decider_is_deterministic() -> None:
    snapshot = _worker_snapshot(
        units=(_worker("w1", 0, 0), _worker("w2", 2, 0)),
        resource_cells={
            "1,0": ResourceCellInfo(position=Coordinate(1, 0), visible=True, last_seen_tick=1)
        },
        core_position=Coordinate(0, 0),
    )
    first = ComposedDecider().decide_snapshot(snapshot)
    second = ComposedDecider().decide_snapshot(snapshot)
    assert first == second
    assert _action(first, "w1") is UnitActionType.MOVE
    direction = _direction(first, "w1")
    assert direction is not None and direction.value == "east"


def test_exhausted_budget_returns_safe_wait_decision() -> None:
    observation = TurnObservation(
        tick=1,
        lifecycle=PlayerLifecycle.ACTIVE,
        resources=0,
        population=0,
        projection=_empty_projection(1),
    )
    decision = compose_decider()(observation, DeadlineBudget(0))
    assert decision.tick == 1
    assert decision.unit_intents == ()
    assert decision.core_intent is not None and decision.core_intent.action is CoreAction.WAIT


def test_snapshot_from_turn_projects_economy_and_tick() -> None:
    observation = TurnObservation(
        tick=3,
        lifecycle=PlayerLifecycle.ACTIVE,
        resources=7,
        population=2,
        projection=_empty_projection(3),
    )
    snapshot = snapshot_from_turn(observation)
    assert snapshot.tick == 3
    assert snapshot.resources == 7
    assert snapshot.population == 2
    assert snapshot.resource_space == snapshot.resource_capacity - 7


def test_plan_to_decision_maps_core_and_unit_intents() -> None:
    unit = _worker("w1", 0, 0)
    snapshot = _worker_snapshot(
        units=(unit,),
        resources=10,
        population=1,
        resource_capacity=10,
        core_position=Coordinate(0, 0),
    )
    plan = ComposedDecider().decide_snapshot(snapshot)
    decision = plan_to_decision(plan)
    assert decision.tick == plan.tick
    assert decision.core_intent is not None and decision.core_intent.action is CoreAction.SPAWN
    assert len(decision.unit_intents) == 1
    intent = decision.unit_intents[0]
    assert intent.unit_id.value == "w1"
    assert intent.action in {
        UnitAction.MOVE,
        UnitAction.WAIT,
        UnitAction.HARVEST,
    }


def test_composed_decider_rejects_unknown_variant() -> None:
    config = ComposedDeciderConfig(variants=("not-a-variant-v1",))
    with pytest.raises(ValueError):
        ComposedDecider(config)


def _empty_projection(tick: int) -> WorldProjection:
    return WorldProjection(
        tick=tick,
        rules_version=CURRENT_RULES_VERSION,
        core=None,
        units=(),
        entities=(),
        resources=(),
        terrain=(),
        beacon=BeaconObservation(position=Coordinate(0, 0), status=BeaconStatus.UNKNOWN),
    )


def test_route_aware_deposit_waits_when_core_cell_occupied() -> None:
    """A full worker adjacent to an occupied Core cell WAITs instead of pushing.

    The FFA engine only allows two entities per cell and the Core permanently
    occupies its own cell, so a worker that steps onto an occupied Core cell
    deterministically fails with CELL_UNIT_LIMIT. The route-aware deposit path
    must wait one tick for the resident to deposit and vacate.
    """

    core = Coordinate(0, 0)
    resident = _worker("resident", 0, 0, cargo=1)
    full = _worker("full", 0, 1, cargo=1)
    snapshot = _worker_snapshot(
        units=(resident, full),
        core_position=core,
        resource_capacity=100,
    )
    assignment = Assignment(
        unit_id="full",
        task=Task(type=TaskType.DEPOSIT, target=core),
    )
    plan = merge_worker_tasks(
        Plan(tick=1, unit_actions=(), core_action=None),
        (assignment,),
        snapshot,
        route_aware=True,
    )
    assert _action(plan, "full") is UnitActionType.WAIT


def test_route_aware_deposit_moves_when_core_cell_free() -> None:
    """With the Core cell free the same full worker still steps toward it."""

    core = Coordinate(0, 0)
    full = _worker("full", 0, 1, cargo=1)
    snapshot = _worker_snapshot(
        units=(full,),
        core_position=core,
        resource_capacity=100,
    )
    assignment = Assignment(
        unit_id="full",
        task=Task(type=TaskType.DEPOSIT, target=core),
    )
    plan = merge_worker_tasks(
        Plan(tick=1, unit_actions=(), core_action=None),
        (assignment,),
        snapshot,
        route_aware=True,
    )
    assert _action(plan, "full") is UnitActionType.MOVE
