"""Differential and boundary tests for worker forced tasks."""

from __future__ import annotations

from typing import Any, cast

import pytest

from arena_hero_agent.domain import CURRENT_RULES_VERSION, Coordinate, EntityId, UnitRole
from arena_hero_agent.planning import (
    BeaconInfo,
    PlanningSnapshot,
    PlanningUnit,
    ResourceCellInfo,
    Task,
    TaskType,
    can_deposit,
    can_return_for_heal,
    forced_task_for,
)
from tests.strategies.fixture_loader import load_oracle_fixture

RULES = CURRENT_RULES_VERSION


def _coordinate(value: list[int]) -> Coordinate:
    return Coordinate(value[0], value[1])


def _role(name: str) -> UnitRole:
    return {"WORKER": UnitRole.WORKER, "VANGUARD": UnitRole.VANGUARD, "RANGER": UnitRole.RANGER}[
        name
    ]


def _unit(record: tuple[Any, ...]) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(record[0]),
        unit_role=_role(record[1]),
        position=_coordinate(record[2]),
        health=record[3],
        cargo=record[4],
    )


def _snapshot(
    units: tuple[PlanningUnit, ...] = (),
    *,
    resources: int = 0,
    resource_capacity: int = 10,
    resource_space: int = 10,
    resource_cells: tuple[tuple[int, int], ...] = (),
    visible_resource_cells: bool = True,
    core: tuple[Any, ...] | None = None,
    beacon: dict[str, Any] | None = None,
) -> PlanningSnapshot:
    resource_map = {
        f"{x},{y}": ResourceCellInfo(
            position=Coordinate(x, y),
            visible=visible_resource_cells,
            last_seen_tick=1 if visible_resource_cells else None,
        )
        for x, y in resource_cells
    }
    if beacon is None:
        beacon_info = BeaconInfo(position=Coordinate(0, 0), status="ground", carrier_id=None)
    else:
        beacon_info = BeaconInfo(
            position=_coordinate(beacon["position"]),
            status=beacon["status"].lower(),
            carrier_id=None if beacon.get("carrierId") is None else EntityId(beacon["carrierId"]),
        )
    core_id = core_pos = core_health = core_shield = core_state = None
    if core is not None:
        core_id = core[0]
        core_pos = _coordinate(core[1])
        core_health = core[2]
        core_shield = core[3]
        core_state = core[4].lower()
    return PlanningSnapshot(
        tick=1,
        rules_version=RULES,
        resources=resources,
        resource_capacity=resource_capacity,
        resource_space=resource_space,
        population=0,
        units=units,
        resource_cells=resource_map,
        obstacle_cells=frozenset(),
        enemy_cells=frozenset(),
        enemy_units=(),
        core_id=core_id,
        core_position=core_pos,
        core_health=core_health,
        core_shield=core_shield,
        core_state=core_state,
        beacon=beacon_info,
        threat_map={},
    )


def _core(x: int, y: int, *, state: str = "NORMAL", shield: int = 5) -> tuple[Any, ...]:
    return ("core", [x, y], 5, shield, state)


# Reconstructed inputs mirroring the TypeScript fixture generator (generate-fixtures.ts).
_FORCED_CASES: dict[str, dict[str, Any]] = {
    "cargo_worker_near_core": {
        "units": [("w1", "WORKER", [1, 0], 2, 1)],
        "core": _core(0, 0),
    },
    "worker_on_visible_resource": {
        "units": [("w1", "WORKER", [2, 2], 2, 0)],
        "resource_cells": [(2, 2)],
        "core": _core(0, 0),
    },
    "low_hp_worker_away_from_core": {
        "units": [("w1", "WORKER", [5, 0], 1, 0)],
        "core": _core(0, 0),
    },
    "low_hp_worker_on_core": {
        "units": [("w1", "WORKER", [0, 0], 1, 0)],
        "core": _core(0, 0),
    },
    "beacon_on_worker_cell": {
        "units": [("w1", "WORKER", [3, 3], 2, 0)],
        "beacon": {"position": [3, 3], "status": "GROUND", "carrierId": None},
        "core": _core(0, 0),
    },
    "beacon_carried_elsewhere": {
        "units": [("w1", "WORKER", [3, 3], 2, 0)],
        "beacon": {"position": [3, 3], "status": "CARRIED", "carrierId": "other"},
        "core": _core(0, 0),
    },
    "idle_worker": {
        "units": [("w1", "WORKER", [4, 4], 2, 0)],
        "core": _core(0, 0),
    },
    "cargo_worker_resource_full": {
        "units": [("w1", "WORKER", [1, 0], 2, 1)],
        "resources": 10,
        "resource_capacity": 10,
        "resource_space": 0,
        "core": _core(0, 0),
    },
    "cargo_worker_no_core": {
        "units": [("w1", "WORKER", [1, 0], 2, 1)],
    },
    "mixed_units": {
        "units": [("w1", "WORKER", [1, 0], 2, 1), ("v1", "VANGUARD", [2, 0], 4, 0)],
        "core": _core(0, 0),
    },
}

# The oracle's snapshot has no visibility metadata, so a worker on a resource cell
# that is NOT visible this tick still reports HARVEST_CURRENT there. Python models
# visibility and fail-closes to no forced task; this is a registered
# ALLOWED_DIFFERENCE in docs/planning-differences.md.
_ALLOWED_INVISIBLE_RESOURCE = "worker_on_invisible_resource"


def _normalized_task(task: Task | None) -> dict[str, Any] | None:
    if task is None:
        return None
    return {
        "type": task.type.value,
        "target": [task.target.x, task.target.y] if task.target is not None else None,
        "targetCellKey": task.target_cell_key,
    }


def test_forced_tasks_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["forced_tasks"]:
        if case["name"] == _ALLOWED_INVISIBLE_RESOURCE:
            continue  # registered ALLOWED_DIFFERENCE; asserted separately below
        inputs = _FORCED_CASES[case["name"]]
        snapshot = _snapshot(
            tuple(_unit(unit) for unit in inputs["units"]),
            resources=inputs.get("resources", 0),
            resource_capacity=inputs.get("resource_capacity", 10),
            resource_space=inputs.get("resource_space", 10),
            resource_cells=inputs.get("resource_cells", ()),
            core=inputs.get("core"),
            beacon=inputs.get("beacon"),
        )
        got = {
            unit.id.value: _normalized_task(forced_task_for(unit, snapshot))
            for unit in snapshot.units
        }
        assert got == case["expected"], case["name"]


def test_invisible_resource_fails_closed_as_registered_difference() -> None:
    fixture = load_oracle_fixture()
    case = next(
        case for case in fixture["forced_tasks"] if case["name"] == _ALLOWED_INVISIBLE_RESOURCE
    )
    # The oracle reports HARVEST_CURRENT because its snapshot cannot distinguish
    # a visible cell from a stale memory cell.
    assert case["expected"]["w1"]["type"] == "HARVEST_CURRENT"
    snapshot = _snapshot(
        (_unit(("w1", "WORKER", [2, 2], 2, 0)),),
        resource_cells=((2, 2),),
        visible_resource_cells=False,
    )
    assert forced_task_for(snapshot.units[0], snapshot) is None


def test_can_deposit_boundaries() -> None:
    worker = _unit(("w1", "WORKER", [1, 0], 2, 1))
    with_core = _snapshot((worker,), core=_core(0, 0))
    assert can_deposit(worker, with_core)
    empty_cargo = _snapshot((_unit(("w1", "WORKER", [1, 0], 2, 0)),), core=_core(0, 0))
    assert not can_deposit(empty_cargo.units[0], empty_cargo)
    no_core = _snapshot((worker,))
    assert not can_deposit(worker, no_core)
    full = _snapshot(
        (worker,), resources=10, resource_capacity=10, resource_space=0, core=_core(0, 0)
    )
    assert not can_deposit(worker, full)


def test_can_return_for_heal_boundaries() -> None:
    worker = _unit(("w1", "WORKER", [1, 0], 1, 0))
    assert can_return_for_heal(worker, Coordinate(0, 0))
    assert not can_return_for_heal(worker, None)
    assert not can_return_for_heal(worker, Coordinate(1, 0))
    with pytest.raises(TypeError):
        can_return_for_heal(worker, cast(Coordinate, "core"))


def test_task_requires_target_for_deposit_and_harvest() -> None:
    with pytest.raises(ValueError):
        Task(type=TaskType.DEPOSIT, target=None)
    with pytest.raises(ValueError):
        Task(type=TaskType.HARVEST_CURRENT, target=Coordinate(0, 0), target_cell_key=None)
    with pytest.raises(ValueError):
        Task(type=TaskType.WAIT, target_cell_key="0,0")


def test_forced_task_rejects_invalid_inputs() -> None:
    worker = _unit(("w1", "WORKER", [1, 0], 2, 0))
    snapshot = _snapshot((worker,), core=_core(0, 0))
    with pytest.raises(TypeError):
        forced_task_for(worker, cast(PlanningSnapshot, None))
    with pytest.raises(TypeError):
        forced_task_for(cast(PlanningUnit, None), snapshot)
