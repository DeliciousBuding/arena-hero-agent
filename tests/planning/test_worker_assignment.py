"""Differential and fail-closed tests for deterministic worker assignment (P4-12).

Every ``worker_assignments`` fixture case is a pinned capture from the legacy
TypeScript ``WorkerTaskPlanner`` (arena-hero-agent-ts at 8cf5cbb); see
``docs/planning-differences.md`` for the classification rules.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from arena_hero_agent.domain import CURRENT_RULES_VERSION, Coordinate, Direction, EntityId, UnitRole
from arena_hero_agent.planning import (
    Assignment,
    BeaconInfo,
    EnemyUnit,
    MissionConfig,
    PlanningSnapshot,
    PlanningUnit,
    ResourceCellInfo,
    Task,
    TaskType,
    WorkerClaim,
    WorkerTaskPlannerConfig,
    apply_sticky_bonus,
    assign_worker_tasks,
    next_step_toward,
    progress_decay,
    shortest_path_distances,
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


def _task(record: dict[str, Any]) -> Task:
    kwargs: dict[str, Any] = {}
    if "target" in record:
        kwargs["target"] = _coordinate(record["target"])
    if "targetCellKey" in record:
        kwargs["target_cell_key"] = record["targetCellKey"]
    return Task(type=TaskType(record["type"]), **kwargs)


def _assignment(record: dict[str, Any]) -> Assignment:
    return Assignment(unit_id=record["unitId"], task=_task(record["task"]))


def _mission(record: dict[str, Any] | None) -> MissionConfig:
    if not record:
        return MissionConfig()
    return MissionConfig(**{_MISSION_KEYS.get(key, key): value for key, value in record.items()})


def _snapshot(record: dict[str, Any]) -> PlanningSnapshot:
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


def _normalized(assignment: Assignment) -> dict[str, Any]:
    task: dict[str, Any] = {"type": assignment.task.type.value}
    if assignment.task.target is not None:
        task["target"] = [assignment.task.target.x, assignment.task.target.y]
    if assignment.task.target_cell_key is not None:
        task["targetCellKey"] = assignment.task.target_cell_key
    return {"unitId": assignment.unit_id, "task": task}


def _refill_predictions(record: dict[str, Any]) -> dict[str, int] | None:
    predictions = record.get("refillPredictions")
    return None if not predictions else dict(predictions)


def test_progress_decay_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["progress_decay"]:
        assert progress_decay(case["distance"]) == case["expected"], case["name"]


def test_sticky_bonus_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["sticky_bonus"]:
        previous = tuple(_assignment(item) for item in case["previousAssignments"])
        distance = case.get("distance")
        got = apply_sticky_bonus(
            case["unitId"],
            case["targetCellKey"],
            previous,
            case["amount"],
            distance,
        )
        assert got == case["expected"], case["name"]


def test_routing_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["assignment_routing"]:
        got = shortest_path_distances(
            _coordinate(case["start"]),
            tuple(_coordinate(target) for target in case["targets"]),
            frozenset(case["obstacles"]),
            search_radius=case["searchRadius"],
            node_budget=case["nodeBudget"],
        )
        assert got == case["expected"], case["name"]


def test_worker_assignments_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["worker_assignments"]:
        config = WorkerTaskPlannerConfig(
            mission=_mission(case.get("mission")),
            claim_no_progress_ttl_ticks=case.get("plannerConfig", {}).get(
                "claimNoProgressTtlTicks", 10
            ),
        )
        previous = tuple(_assignment(item) for item in case.get("previousAssignments", []))
        if "ticks" in case:
            claims: frozenset[WorkerClaim] = frozenset()
            got = []
            for tick_record in case["ticks"]:
                result = assign_worker_tasks(
                    _snapshot(tick_record),
                    previous,
                    config=config,
                    survey_burst_active=case.get("options", {}).get("surveyBurstActive", False),
                    claims=claims,
                    refill_predictions=_refill_predictions(tick_record),
                )
                got.append(
                    sorted(
                        (_normalized(assignment) for assignment in result.plan.assignments),
                        key=lambda item: item["unitId"],
                    )
                )
                claims = result.claims
                previous = result.plan.assignments
            assert got == case["expected"], case["name"]
        else:
            result = assign_worker_tasks(
                _snapshot(case["snapshot"]),
                previous,
                config=config,
                survey_burst_active=case.get("options", {}).get("surveyBurstActive", False),
                refill_predictions=_refill_predictions(case["snapshot"]),
            )
            got = sorted(
                (_normalized(assignment) for assignment in result.plan.assignments),
                key=lambda item: item["unitId"],
            )
            assert got == case["expected"], case["name"]


def test_assign_worker_tasks_is_deterministic() -> None:
    fixture = load_oracle_fixture()
    case = next(
        item for item in fixture["worker_assignments"] if item["name"] == "two_workers_two_cells"
    )
    snapshot = _snapshot(case["snapshot"])
    first = assign_worker_tasks(snapshot)
    second = assign_worker_tasks(snapshot)
    assert first.plan.assignments == second.plan.assignments
    assert first.claims == second.claims


def test_same_resource_cell_never_assigned_to_two_workers() -> None:
    fixture = load_oracle_fixture()
    case = next(
        item for item in fixture["worker_assignments"] if item["name"] == "two_workers_one_cell"
    )
    result = assign_worker_tasks(_snapshot(case["snapshot"]))
    targets = [
        assignment.task.target_cell_key
        for assignment in result.plan.assignments
        if assignment.task.target_cell_key is not None
    ]
    assert len(targets) == len(set(targets))


def test_unreachable_or_forbidden_cells_fall_to_wait() -> None:
    # A memory cell beyond the 40-cell limit is not a matrix candidate: the
    # worker must WAIT instead of being assigned an empty long-haul run.
    fixture = load_oracle_fixture()
    case = next(
        item
        for item in fixture["worker_assignments"]
        if item["name"] == "single_worker_memory_beyond_40"
    )
    result = assign_worker_tasks(_snapshot(case["snapshot"]))
    assert result.plan.assignments == (Assignment(unit_id="w1", task=Task(type=TaskType.WAIT)),)


def test_enemy_occupied_cell_is_not_assignable() -> None:
    fixture = load_oracle_fixture()
    case = next(
        item
        for item in fixture["worker_assignments"]
        if item["name"] == "enemy_cell_not_assignable"
    )
    result = assign_worker_tasks(_snapshot(case["snapshot"]))
    assert all(
        assignment.task.type is not TaskType.GO_RESOURCE for assignment in result.plan.assignments
    )


def test_go_resource_task_may_declare_target_cell_key() -> None:
    task = Task(
        type=TaskType.GO_RESOURCE,
        target=Coordinate(1, 0),
        target_cell_key="1,0",
    )
    assert task.target_cell_key == "1,0"
    with pytest.raises(ValueError, match="target_cell_key"):
        Task(type=TaskType.WAIT, target_cell_key="0,0")


def test_claims_cross_tick_release_when_worker_disappears() -> None:
    fixture = load_oracle_fixture()
    case = next(
        item
        for item in fixture["worker_assignments"]
        if item["name"] == "claim_keeps_cell_two_ticks"
    )
    tick_one, tick_two = case["ticks"]
    snapshot_one = _snapshot(tick_one)
    snapshot_two = _snapshot(tick_two)
    result_one = assign_worker_tasks(snapshot_one)
    assert result_one.claims, "GO_RESOURCE assignment must create a claim"
    # Same worker, same cell on the next tick: the claim is renewed and kept.
    result_two = assign_worker_tasks(
        snapshot_two,
        result_one.plan.assignments,
        claims=result_one.claims,
    )
    assert result_two.plan.assignments == result_one.plan.assignments
    assert len(result_two.claims) == 1


def test_assign_worker_tasks_rejects_invalid_inputs() -> None:
    fixture = load_oracle_fixture()
    snapshot = _snapshot(fixture["worker_assignments"][0]["snapshot"])
    with pytest.raises(TypeError, match="snapshot"):
        assign_worker_tasks(cast(PlanningSnapshot, None))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="config"):
        assign_worker_tasks(snapshot, config=cast(WorkerTaskPlannerConfig, None))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="survey_burst_active"):
        assign_worker_tasks(snapshot, survey_burst_active=cast(bool, "yes"))
    with pytest.raises(TypeError, match="claims"):
        assign_worker_tasks(snapshot, claims=cast(frozenset[WorkerClaim], []))
    with pytest.raises(TypeError, match="refill_predictions"):
        assign_worker_tasks(
            snapshot,
            refill_predictions=cast(dict[str, int], [("1,0", 1)]),
        )


def test_worker_task_planner_config_validates() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        WorkerTaskPlannerConfig(sticky_bonus=-1.0)
    with pytest.raises(TypeError, match="mission"):
        WorkerTaskPlannerConfig(mission=cast(MissionConfig, {}))  # type: ignore[arg-type]


def test_shortest_path_distances_validates_inputs() -> None:
    with pytest.raises(TypeError, match="start"):
        shortest_path_distances(
            cast(Coordinate, [0, 0]),  # type: ignore[arg-type]
            (),
            frozenset(),
        )
    with pytest.raises(TypeError, match="obstacles"):
        shortest_path_distances(Coordinate(0, 0), (), cast(frozenset[str], []))  # type: ignore[arg-type]


def test_next_step_toward_routes_around_obstacle() -> None:
    start = Coordinate(0, 0)
    target = Coordinate(2, 0)
    # Block the straight east path, forcing a detour.
    obstacles = frozenset({"1,0"})
    step = next_step_toward(start, target, obstacles)
    assert step in (Direction.NORTH, Direction.SOUTH)


def test_next_step_toward_straight_when_clear() -> None:
    step = next_step_toward(Coordinate(0, 0), Coordinate(2, 0), frozenset())
    assert step is Direction.EAST


def test_next_step_toward_returns_none_when_target_blocked() -> None:
    assert next_step_toward(Coordinate(0, 0), Coordinate(1, 0), frozenset({"1,0"})) is None


def test_survey_burst_keeps_one_harvester_when_resources_wait() -> None:
    """The survey-burst pre-reserve must not starve collection (release-030).

    Regression for the live t2 stall: with ``survey_burst_active`` on and a
    ``survey_worker_cap`` exceeding the worker count, every worker used to be
    pre-reserved as an EXPLORE surveyor before the resource matrix ran, so a
    visible resource right beside the Core was never assigned a harvester.
    """
    record = {
        "tick": 1,
        "resources": 0,
        "resourceCapacity": 10,
        "resourceSpace": 10,
        "population": 2,
        "units": [
            {"id": "w1", "unitType": "WORKER", "position": [0, 0], "hp": 2, "cargo": 0},
            {"id": "w2", "unitType": "WORKER", "position": [1, 0], "hp": 2, "cargo": 0},
        ],
        "resourceCells": {
            "5,0": {"position": [5, 0], "visible": True, "lastSeenTick": 1, "seeded": False},
        },
        "obstacleCells": [],
        "enemyCells": [],
        "enemyUnits": [],
        "corePosition": [0, 0],
        "coreHp": 5,
        "coreState": "NORMAL",
        "beacon": {"position": [0, 0], "status": None, "carrierId": None},
        "threatMap": {},
    }
    snapshot = _snapshot(record)
    config = WorkerTaskPlannerConfig(mission=MissionConfig(survey_worker_cap=3))
    result = assign_worker_tasks(snapshot, (), config=config, survey_burst_active=True)
    task_types = {assignment.task.type for assignment in result.plan.assignments}
    assert TaskType.GO_RESOURCE in task_types


def test_claim_preempt_penalty_lets_nearer_worker_take_over() -> None:
    """A much nearer non-claimant preempts a reserved cell (claim softening).

    The pure layer default (0.0) reproduces the oracle's hard exclusion; a
    positive penalty only lets a worker win when its travel advantage beats
    CLAIM_BONUS (20) plus the penalty.
    """

    record = {
        "tick": 2,
        "resources": 0,
        "resourceCapacity": 100,
        "resourceSpace": 100,
        "population": 2,
        "units": [
            {"id": "w1", "unitType": "WORKER", "position": [0, 0], "hp": 2, "cargo": 0},
            {"id": "w2", "unitType": "WORKER", "position": [30, 0], "hp": 2, "cargo": 0},
        ],
        "resourceCells": {
            "20,0": {"position": [20, 0], "visible": True, "lastSeenTick": 2, "seeded": False},
        },
        "obstacleCells": [],
        "enemyCells": [],
        "enemyUnits": [],
        "corePosition": [0, 0],
        "coreHp": 5,
        "coreState": "NORMAL",
        "beacon": {"position": [0, 0], "status": None, "carrierId": None},
        "threatMap": {},
    }
    snapshot = _snapshot(record)
    claims = frozenset(
        {
            WorkerClaim(
                unit_id="w2",
                cell_key="20,0",
                claim_tick=1,
                last_progress_tick=1,
                progress_distance=10,
                last_position=_coordinate([30, 0]),
            )
        }
    )
    # Default penalty 0.0: the reserved cell stays hard-excluded for w1.
    hard = assign_worker_tasks(snapshot, (), config=WorkerTaskPlannerConfig(), claims=claims)
    hard_tasks = {
        assignment.unit_id: assignment.task.type for assignment in hard.plan.assignments
    }
    assert hard_tasks.get("w1") is TaskType.WAIT

    # With the production penalty the nearer worker (w1, 20 tiles) still
    # cannot beat w2's claim: 20 (CLAIM_BONUS) + 6 (penalty) > the 10-tile
    # travel edge. Preemption only fires for overwhelming advantages.
    soft = assign_worker_tasks(
        snapshot,
        (),
        config=WorkerTaskPlannerConfig(claim_preempt_penalty=6.0),
        claims=claims,
    )
    soft_tasks = {
        assignment.unit_id: assignment.task.type for assignment in soft.plan.assignments
    }
    assert soft_tasks.get("w1") is TaskType.WAIT

    # A truly nearer claimant alternative: w1 at distance 0 from the cell.
    near_record = dict(record)
    near_record["units"] = [
        {"id": "w1", "unitType": "WORKER", "position": [19, 0], "hp": 2, "cargo": 0},
        {"id": "w2", "unitType": "WORKER", "position": [30, 0], "hp": 2, "cargo": 0},
    ]
    near_snapshot = _snapshot(near_record)
    taken = assign_worker_tasks(
        near_snapshot,
        (),
        config=WorkerTaskPlannerConfig(claim_preempt_penalty=6.0),
        claims=claims,
    )
    taken_tasks = {
        assignment.unit_id: assignment.task for assignment in taken.plan.assignments
    }
    assert taken_tasks["w1"].type is TaskType.GO_RESOURCE
    assert taken_tasks["w1"].target_cell_key == "20,0"
