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

from dataclasses import replace
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
    CoreActionType,
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


def _all_off() -> ComposedDeciderConfig:
    """Explicit baseline with every research layer disabled for oracle parity tests."""

    return ComposedDeciderConfig(
        survey_burst_active=False,
        stuck_guard_enabled=False,
        movement_guard_enabled=False,
        economy_budget_enabled=False,
        economy_expansion_enabled=False,
        raid_quota_enabled=False,
        exploration_v2_enabled=False,
        respawn_recovery_enabled=False,
    )


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
        plan = ComposedDecider(_all_off()).decide_snapshot(snapshot)
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
        exploration_v2_enabled=False,
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


def test_exploration_v2_shrinks_survey_cap_when_cells_exist() -> None:
    """With collectable cells the burst pre-reserve shrinks to one surveyor.

    The permanent burst previously claimed three workers as explorers before
    the matrix ran, leaving pop 2-4 tenants with a single harvester
    (production: doubling workers did not double income). The exploration-v2
    composition path caps the pre-reserve at one when resource cells exist.
    """

    fixture = load_oracle_fixture()
    case = next(
        item
        for item in fixture["worker_assignments"]
        if item["name"] == "survey_burst_pre_reserve"
    )
    config = ComposedDeciderConfig(
        worker_config=WorkerTaskPlannerConfig(mission=_mission(case.get("mission"))),
        survey_burst_active=True,
        exploration_v2_enabled=True,
        movement_guard_enabled=False,
        economy_expansion_enabled=False,
        respawn_recovery_enabled=False,
        barren_migration_enabled=False,
        stuck_resources_enabled=False,
        raid_quota_enabled=False,
        stuck_guard_enabled=False,
        economy_budget_enabled=False,
    )
    plan = ComposedDecider(config).decide_snapshot(_fixture_snapshot(case["snapshot"]))
    # Exactly one surveyor explores (w1, dense patrol east); the remaining
    # three workers are assigned to resource cells by the matrix.
    direction = _direction(plan, "w1")
    assert direction is not None and direction.value == "east"
    for worker_id in ("w2", "w3", "w4"):
        assert _action(plan, worker_id) is UnitActionType.MOVE


def test_idle_worker_vacates_crowded_core_cell_for_deposit() -> None:
    """An idle worker vacates the Core cell even when every neighbor holds one unit.

    Cell capacity is two (Core plus one unit), so a single friendly occupant
    does not block the vacate. The state-seed replay harness reproduced a
    500-tick deposit stall where cargo workers ringed the Core while a WAIT
    worker held the Core cell and the old vacate treated any occupant as a
    wall. The vacate must step into a cell with one free slot so the deposit
    chain resumes.
    """

    core = Coordinate(0, 0)
    idle_worker = _worker("w_idle", 0, 0)
    cargo_worker = _worker("w_cargo", 1, 0, cargo=1)
    ring = (
        _worker("w_n", 0, 1),
        _worker("w_s", 0, -1),
        _worker("w_w", -1, 0),
    )
    snapshot = _worker_snapshot(
        units=(idle_worker, cargo_worker, *ring),
        core_position=core,
        resources=5,
        population=5,
    )
    config = ComposedDeciderConfig(
        survey_burst_active=False,
        exploration_v2_enabled=True,
        movement_guard_enabled=False,
        economy_expansion_enabled=False,
        respawn_recovery_enabled=False,
        barren_migration_enabled=False,
        stuck_resources_enabled=False,
        raid_quota_enabled=False,
        stuck_guard_enabled=False,
        economy_budget_enabled=False,
    )
    plan = ComposedDecider(config).decide_snapshot(snapshot)
    assert _action(plan, "w_idle") is UnitActionType.MOVE


def test_two_tick_claim_sequence_is_sticky() -> None:
    fixture = load_oracle_fixture()
    case = next(
        item
        for item in fixture["worker_assignments"]
        if item["name"] == "claim_keeps_cell_two_ticks"
    )
    decider = ComposedDecider(_all_off())
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


def test_terrain_trap_requires_consecutive_core_occupancy() -> None:
    """The trap self-destruct waits for TERRAIN_TRAP_CONFIRM_TICKS on-Core ticks.

    A worker passing through the Core cell must not be destroyed the moment
    the stuck-resources timer fires (production t2 burned resources across
    six SPAWN_FAILED -> self-destruct -> spawn cycles).
    """

    def make_decider() -> ComposedDecider:
        return ComposedDecider(
            replace(
                _all_off(),
                stuck_resources_enabled=True,
                stuck_resources_ticks=1,
            )
        )

    def snapshot(tick: int, on_core: bool) -> PlanningSnapshot:
        position = Coordinate(0, 0) if on_core else Coordinate(1, 0)
        base = _worker_snapshot(
            units=(_worker("w1", position.x, position.y),),
            resources=5,
            population=1,
            core_position=Coordinate(0, 0),
        )
        return replace(base, tick=tick)

    # Ticks 1-3: the worker stands on the Core cell. The stuck-resources
    # timer fires from tick 1, but the confirmation needs 3 consecutive
    # occupancy ticks (tracked from tick 1), so no self-destruct before
    # tick 4.
    decider = make_decider()
    assert _action(decider.decide_snapshot(snapshot(1, True)), "w1") is UnitActionType.WAIT
    assert _action(decider.decide_snapshot(snapshot(2, True)), "w1") is UnitActionType.WAIT
    assert _action(decider.decide_snapshot(snapshot(3, True)), "w1") is UnitActionType.WAIT
    assert (
        _action(decider.decide_snapshot(snapshot(4, True)), "w1")
        is UnitActionType.SELF_DESTRUCT
    )

    # A fresh decider: the worker leaves the Core cell after one tick; the
    # suspect is cleared and never destroyed.
    decider = make_decider()
    assert _action(decider.decide_snapshot(snapshot(1, True)), "w1") is UnitActionType.WAIT
    for tick in (2, 3, 4):
        assert (
            _action(decider.decide_snapshot(snapshot(tick, False)), "w1")
            is UnitActionType.WAIT
        )


def _snapshot_for_sanctuary(
    *,
    enemies: tuple[EnemyUnit, ...],
    workers: tuple[tuple[str, int, int, int], ...],
    core: Coordinate | None = None,
) -> PlanningSnapshot:
    """Minimal snapshot for the worker-threat-sanctuary hook (candidate D)."""

    core = Coordinate(0, 0) if core is None else core

    return PlanningSnapshot(
        tick=1,
        rules_version=RULES,
        resources=0,
        resource_capacity=100,
        resource_space=100,
        population=len(workers),
        units=tuple(
            PlanningUnit(
                id=EntityId(unit_id),
                unit_role=UnitRole.WORKER,
                position=Coordinate(x, y),
                health=2,
                cargo=cargo,
            )
            for unit_id, x, y, cargo in workers
        ),
        resource_cells={},
        obstacle_cells=frozenset(),
        enemy_cells=frozenset(),
        enemy_units=enemies,
        core_id="core",
        core_position=core,
        core_health=5,
        core_shield=5,
        core_state="normal",
        beacon=BeaconInfo(position=Coordinate(0, 0), status=None),
        threat_map={},
    )


def _worker_action(plan: Plan, unit_id: str):
    for action in plan.unit_actions:
        if action.unit_id == EntityId(unit_id):
            return action
    raise AssertionError(f"missing action for {unit_id}")


def test_worker_sanctuary_redirects_cargoless_moves_home() -> None:
    """Candidate D: an enemy at the door pulls cargo-less workers home."""

    enemy = EnemyUnit(id=EntityId("e1"), position=Coordinate(0, 4), unit_role=UnitRole.VANGUARD)
    snapshot = _snapshot_for_sanctuary(
        enemies=(enemy,),
        workers=(("far", 6, 0, 0),),
    )
    plan = ComposedDecider().decide_snapshot(snapshot)
    action = _worker_action(plan, "far")
    # The worker's outward MOVE (assigned by the matrix baseline) is replaced
    # by a step toward the Core's ring (never a step away on the x axis).
    assert action.type is UnitActionType.MOVE
    assert action.direction in (Direction.WEST, Direction.SOUTH, Direction.NORTH)


def test_worker_sanctuary_keeps_cargo_workers() -> None:
    enemy = EnemyUnit(id=EntityId("e1"), position=Coordinate(0, 4), unit_role=UnitRole.VANGUARD)
    snapshot = _snapshot_for_sanctuary(
        enemies=(enemy,),
        workers=(("carrier", 6, 0, 1),),
    )
    plan = ComposedDecider().decide_snapshot(snapshot)
    action = _worker_action(plan, "carrier")
    # Cargo workers keep their original move (heading home to deposit).
    assert action.type is UnitActionType.MOVE


def test_worker_sanctuary_inactive_without_enemy() -> None:
    snapshot = _snapshot_for_sanctuary(
        enemies=(),
        workers=(("far", 6, 0, 0),),
    )
    plan = ComposedDecider().decide_snapshot(snapshot)
    # No enemy: the hook leaves the plan untouched (a WAIT or assignment move
    # depending on the matrix, but never a forced re-route crash).
    assert plan.unit_actions


def test_worker_sanctuary_inactive_with_far_enemy() -> None:
    enemy = EnemyUnit(id=EntityId("e1"), position=Coordinate(0, 20), unit_role=UnitRole.VANGUARD)
    snapshot = _snapshot_for_sanctuary(
        enemies=(enemy,),
        workers=(("far", 6, 0, 0),),
    )
    plan = ComposedDecider().decide_snapshot(snapshot)
    assert plan.unit_actions


def _low_yield_snapshot(
    *,
    tick: int,
    resources: int,
    population: int = 2,
) -> PlanningSnapshot:
    """Deep-ring tenant: reachable crumb cell, resources far below spawn cost."""

    core = Coordinate(30, 0)
    return PlanningSnapshot(
        tick=tick,
        rules_version=RULES,
        resources=resources,
        resource_capacity=100,
        resource_space=100 - resources,
        population=population,
        units=(
            _worker("w1", 28, 0),
            _worker("w2", 29, 1),
        ),
        resource_cells={
            "crumb": ResourceCellInfo(
                position=Coordinate(33, 0),
                visible=True,
                last_seen_tick=tick,
                seeded=False,
            )
        },
        obstacle_cells=frozenset(),
        enemy_cells=frozenset(),
        enemy_units=(),
        core_id="core",
        core_position=core,
        core_health=5,
        core_shield=5,
        core_state="normal",
        beacon=BeaconInfo(position=Coordinate(0, 0), status=None),
        threat_map={},
    )


def test_low_yield_trap_migrates_toward_origin() -> None:
    """Candidate E: crumb cells keep the barren latch reset forever, but the
    yield can never afford a Worker — after LOW_YIELD_STALL_TICKS the Core
    migrates toward origin (production t2/t3 deep-ring trap)."""

    decider = ComposedDecider()
    # Seed one local harvest so crumb deposits count as economic activity:
    # the phantom-resource and plain-barren paths stay reset forever, and
    # only the low-yield stall can start a migration.
    decider._exploration_state.harvested_cells["31,0"] = (Coordinate(31, 0), 10**9)
    saw_start_move = None
    for tick in range(1, 131):
        resources = (0, 1, 2, 0, 1)[tick % 5]
        plan = decider.decide_snapshot(_low_yield_snapshot(tick=tick, resources=resources))
        if plan.core_action is not None and plan.core_action.type is CoreActionType.START_MOVE:
            saw_start_move = plan.core_action
            break
    assert saw_start_move is not None, "expected low-yield migration to start"
    # Core at (30, 0): the first migration step must head toward the origin.
    assert saw_start_move.direction is Direction.WEST


def test_low_yield_stall_resets_on_spawn_affordability() -> None:
    """Candidate E: reaching the Worker price proves the region sustains the
    economy — the stall counter resets and no migration starts."""

    decider = ComposedDecider()
    decider._exploration_state.harvested_cells["31,0"] = (Coordinate(31, 0), 10**9)
    for tick in range(1, 131):
        # Every 50 ticks the tenant briefly affords a spawn.
        resources = 5 if tick % 50 == 30 else (0, 1, 2, 0, 1)[tick % 5]
        plan = decider.decide_snapshot(_low_yield_snapshot(tick=tick, resources=resources))
        if plan.core_action is not None:
            assert plan.core_action.type is not CoreActionType.START_MOVE, (
                f"migration fired at tick {tick} despite affordable spawns"
            )


def test_low_yield_stall_ignores_military_tenants() -> None:
    """Candidate E: tenants with military are under a different economy —
    they must not migrate while fielding defenders."""

    decider = ComposedDecider()
    decider._exploration_state.harvested_cells["31,0"] = (Coordinate(31, 0), 10**9)
    for tick in range(1, 131):
        resources = (0, 1, 2, 0, 1)[tick % 5]
        snapshot = _low_yield_snapshot(tick=tick, resources=resources)
        snapshot = replace(
            snapshot,
            population=5,
            units=snapshot.units
            + (
                PlanningUnit(
                    id=EntityId("v1"),
                    unit_role=UnitRole.VANGUARD,
                    position=Coordinate(26, 0),
                    health=4,
                    cargo=0,
                ),
            ),
        )
        plan = decider.decide_snapshot(snapshot)
        if plan.core_action is not None:
            assert plan.core_action.type is not CoreActionType.START_MOVE, (
                f"military tenant migrated at tick {tick}"
            )


def test_worker_sanctuary_personal_flee_during_approach() -> None:
    """Candidate D2: an enemy still approaching the Core (outside the
    sanctuary radius) makes only the worker it is next to reroute home —
    the rest of the economy keeps working (production t2: workers were
    picked off one by one during the approach phase)."""

    enemy = EnemyUnit(id=EntityId("e1"), position=Coordinate(10, 4), unit_role=UnitRole.VANGUARD)
    snapshot = _snapshot_for_sanctuary(
        enemies=(enemy,),
        workers=(("near", 12, 5, 0), ("far", 6, 0, 0)),
    )
    plan = ComposedDecider().decide_snapshot(snapshot)
    near_action = _worker_action(plan, "near")
    assert near_action.type is UnitActionType.MOVE
    # Rerouted home: never a step toward the enemy's side (east).
    assert near_action.direction in (Direction.WEST, Direction.SOUTH, Direction.NORTH)
    far_action = _worker_action(plan, "far")
    # The far worker is outside the personal flee radius and the sanctuary
    # is inactive, so its action is the unmodified assignment.
    assert far_action is not None


def test_worker_sanctuary_personal_flee_skips_cargo_worker() -> None:
    """D2: a cargo worker next to the enemy keeps walking (home to deposit)."""

    enemy = EnemyUnit(id=EntityId("e1"), position=Coordinate(10, 4), unit_role=UnitRole.VANGUARD)
    snapshot = _snapshot_for_sanctuary(
        enemies=(enemy,),
        workers=(("carrier", 12, 5, 1),),
    )
    plan = ComposedDecider().decide_snapshot(snapshot)
    action = _worker_action(plan, "carrier")
    assert action.type is UnitActionType.MOVE
