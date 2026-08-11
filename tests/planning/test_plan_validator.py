"""Differential and fail-closed tests for deterministic plan validation."""

from __future__ import annotations

from typing import Any

import pytest

from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    Coordinate,
    Direction,
    EntityId,
    UnitRole,
)
from arena_hero_agent.planning import (
    BeaconInfo,
    CoreAction,
    CoreActionType,
    EnemyUnit,
    Plan,
    PlanIntent,
    PlanningSnapshot,
    PlanningUnit,
    ResourceCellInfo,
    UnitAction,
    UnitActionType,
    ValidationCode,
    build_threat_map,
    validate_plan,
)
from tests.strategies.fixture_loader import load_oracle_fixture

RULES = CURRENT_RULES_VERSION
DIRECTION = {
    "UP": Direction.NORTH,
    "DOWN": Direction.SOUTH,
    "LEFT": Direction.WEST,
    "RIGHT": Direction.EAST,
}
ROLE = {
    "WORKER": UnitRole.WORKER,
    "VANGUARD": UnitRole.VANGUARD,
    "RANGER": UnitRole.RANGER,
}


def _coordinate(value: list[int]) -> Coordinate:
    return Coordinate(value[0], value[1])


def _planning_unit(record: dict[str, Any]) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(record["id"]),
        unit_role=ROLE[record["unitType"]],
        position=_coordinate(record["position"]),
        health=record.get("hp", 2),
        cargo=record.get("cargo", 0),
    )


def _snapshot(state: dict[str, Any]) -> PlanningSnapshot:
    units = tuple(_planning_unit(unit) for unit in state["units"])
    enemies = tuple(
        EnemyUnit(
            id=EntityId(enemy["id"]),
            position=_coordinate(enemy["position"]),
            unit_role=ROLE[enemy["unitType"]],
        )
        for enemy in state["visible_enemies"]
    )
    resource_cells = {
        f"{x},{y}": ResourceCellInfo(
            position=Coordinate(x, y), visible=True, last_seen_tick=state["tick"]
        )
        for x, y in state["resource_cells"]
    }
    beacon = state.get("beacon")
    if beacon is None:
        beacon_info = BeaconInfo(position=Coordinate(0, 1), status="ground", carrier_id=None)
    else:
        beacon_info = BeaconInfo(
            position=_coordinate(beacon["position"]),
            status=beacon["status"].lower(),
            carrier_id=None if beacon.get("carrierId") is None else EntityId(beacon["carrierId"]),
        )
    core = state.get("core")
    core_id = core_pos = core_health = core_shield = core_state = None
    if core is not None:
        core_id = core["id"]
        core_pos = _coordinate(core["position"])
        core_health = core["hp"]
        core_shield = core.get("shield", 0)
        core_state = core["state"].lower()
    return PlanningSnapshot(
        tick=state["tick"],
        rules_version=RULES,
        resources=state["resources"],
        resource_capacity=state["resource_capacity"],
        resource_space=state["resource_space"],
        population=state["population"],
        units=units,
        resource_cells=resource_cells,
        obstacle_cells=frozenset(f"{x},{y}" for x, y in state["obstacle_cells"]),
        enemy_cells=frozenset(enemy.position.cell_key for enemy in enemies),
        enemy_units=enemies,
        core_id=core_id,
        core_position=core_pos,
        core_health=core_health,
        core_shield=core_shield,
        core_state=core_state,
        beacon=beacon_info,
        threat_map=build_threat_map(enemies),
    )


def _action(record: dict[str, Any]) -> UnitAction:
    return UnitAction(
        unit_id=EntityId(record["unit_id"]),
        type=UnitActionType(record["type"].lower()),
        direction=None if record.get("direction") is None else DIRECTION[record["direction"]],
        target_id=None if record.get("targetId") is None else EntityId(record["targetId"]),
        expected_cell=None
        if record.get("expectedCell") is None
        else _coordinate(record["expectedCell"]),
    )


def _core_action(record: dict[str, Any] | None) -> CoreAction | None:
    if record is None:
        return None
    return CoreAction(
        type=CoreActionType(record["type"].lower()),
        direction=None if record.get("direction") is None else DIRECTION[record["direction"]],
        unit_role=None if record.get("unitType") is None else ROLE[record["unitType"]],
    )


def _plan(record: dict[str, Any]) -> Plan:
    return Plan(
        tick=record["tick"],
        unit_actions=tuple(
            _action({**action, "unit_id": unit_id})
            for unit_id, action in record["unitActions"].items()
        ),
        core_action=_core_action(record["coreAction"]),
        intents=tuple(
            PlanIntent(actor_id=actor, intent=intent) for actor, intent in record["intents"].items()
        ),
    )


def _normalize_result(result: Any) -> dict[str, Any]:
    unit_actions: dict[str, Any] = {}
    for action in result.plan.unit_actions:
        normalized: dict[str, Any] = {"type": action.type.value.upper()}
        if action.direction is not None:
            normalized["direction"] = {
                "north": "UP",
                "east": "RIGHT",
                "south": "DOWN",
                "west": "LEFT",
            }[action.direction.value]
        if action.target_id is not None:
            normalized["targetId"] = action.target_id.value
        if action.expected_cell is not None:
            normalized["expectedCell"] = [action.expected_cell.x, action.expected_cell.y]
        unit_actions[action.unit_id.value] = normalized
    core_action = None
    if result.plan.core_action is not None:
        core_action = {"type": result.plan.core_action.type.value.upper()}
        if result.plan.core_action.direction is not None:
            core_action["direction"] = {
                "north": "UP",
                "east": "RIGHT",
                "south": "DOWN",
                "west": "LEFT",
            }[result.plan.core_action.direction.value]
        if result.plan.core_action.unit_role is not None:
            core_action["unitType"] = result.plan.core_action.unit_role.value.upper()
    return {
        "valid": result.valid,
        "repaired": result.repaired,
        "unitActions": unit_actions,
        "coreAction": core_action,
        "intents": {intent.actor_id: intent.intent for intent in result.plan.intents},
        "issues": [
            {"code": issue.code.value, "actorId": issue.actor_id, "message": issue.message}
            for issue in result.issues
        ],
    }


def _base_state() -> dict[str, Any]:
    return {
        "tick": 1,
        "units": [
            {"id": "w1", "unitType": "WORKER", "position": [0, 1], "hp": 2, "cargo": 0},
            {"id": "v1", "unitType": "VANGUARD", "position": [1, 1], "hp": 4, "cargo": 0},
            {"id": "r1", "unitType": "RANGER", "position": [2, 1], "hp": 2, "cargo": 0},
        ],
        "resource_cells": [(0, 1)],
        "obstacle_cells": [(1, 2)],
        "core": {"id": "core", "position": [0, 0], "hp": 5, "shield": 5, "state": "NORMAL"},
        "visible_enemies": [{"id": "enemy1", "unitType": "RANGER", "position": [5, 1]}],
        "resources": 100,
        "resource_capacity": 100,
        "resource_space": 90,
        "population": 3,
        "beacon": {"position": [0, 1], "status": "GROUND", "carrierId": None},
    }


# Reconstructed plan inputs mirroring the TypeScript fixture generator.
def _validation_cases() -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    base = _base_state()
    cases: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
        "valid_plan": (
            base,
            {
                "tick": 1,
                "unitActions": {
                    "w1": {"type": "HARVEST"},
                    "v1": {"type": "MOVE", "direction": "UP"},
                    "r1": {"type": "SHOOT", "targetId": "enemy1", "expectedCell": [5, 1]},
                },
                "coreAction": {"type": "SPAWN", "unitType": "WORKER"},
                "intents": {"w1": "harvest", "v1": "move"},
            },
        ),
        "tick_mismatch": (base, {"tick": 2, "unitActions": {}, "coreAction": None, "intents": {}}),
        "unknown_unit": (
            base,
            {
                "tick": 1,
                "unitActions": {"ghost": {"type": "WAIT"}},
                "coreAction": None,
                "intents": {},
            },
        ),
        "blocked_move": (
            base,
            {
                "tick": 1,
                "unitActions": {"v1": {"type": "MOVE", "direction": "DOWN"}},
                "coreAction": None,
                "intents": {},
            },
        ),
        "wrong_capability_sweep_on_worker": (
            base,
            {
                "tick": 1,
                "unitActions": {"w1": {"type": "SWEEP", "direction": "UP"}},
                "coreAction": None,
                "intents": {},
            },
        ),
        "invalid_harvest_off_resource": (
            base,
            {
                "tick": 1,
                "unitActions": {"v1": {"type": "HARVEST"}},
                "coreAction": None,
                "intents": {},
            },
        ),
        "invalid_deposit_empty_cargo": (
            base,
            {
                "tick": 1,
                "unitActions": {"w1": {"type": "DEPOSIT"}},
                "coreAction": None,
                "intents": {},
            },
        ),
        "invalid_deposit_not_on_core": (
            base,
            {
                "tick": 1,
                "unitActions": {"w1": {"type": "DEPOSIT"}},
                "coreAction": None,
                "intents": {},
            },
        ),
        "invalid_heal_full_hp": (
            base,
            {"tick": 1, "unitActions": {"w1": {"type": "HEAL"}}, "coreAction": None, "intents": {}},
        ),
        "invalid_heal_off_core": (
            base,
            {"tick": 1, "unitActions": {"v1": {"type": "HEAL"}}, "coreAction": None, "intents": {}},
        ),
        "invalid_shot_out_of_range": (
            base,
            {
                "tick": 1,
                "unitActions": {"r1": {"type": "SHOOT", "targetId": None, "expectedCell": [9, 9]}},
                "coreAction": None,
                "intents": {},
            },
        ),
        "invalid_shot_target_not_visible": (
            base,
            {
                "tick": 1,
                "unitActions": {
                    "r1": {"type": "SHOOT", "targetId": "missing", "expectedCell": [5, 1]}
                },
                "coreAction": None,
                "intents": {},
            },
        ),
        "invalid_beacon_carried": (
            base,
            {
                "tick": 1,
                "unitActions": {"w1": {"type": "PICKUP_BEACON"}},
                "coreAction": None,
                "intents": {},
            },
        ),
        "missing_core": (
            {**base, "core": None},
            {"tick": 1, "unitActions": {}, "coreAction": {"type": "HEAL"}, "intents": {}},
        ),
        "core_spawn_insufficient_resources": (
            {
                **base,
                "resources": 0,
                "resource_capacity": 10,
                "resource_space": 10,
                "population": 0,
            },
            {
                "tick": 1,
                "unitActions": {},
                "coreAction": {"type": "SPAWN", "unitType": "RANGER"},
                "intents": {},
            },
        ),
        "core_spawn_affordable_dynamic_price": (
            {
                **base,
                "resources": 50,
                "population": 25,
                "resource_capacity": 130,
                "resource_space": 80,
            },
            {
                "tick": 1,
                "unitActions": {},
                "coreAction": {"type": "SPAWN", "unitType": "WORKER"},
                "intents": {},
            },
        ),
        "core_repair_shield_insufficient": (
            {**base, "resources": 0},
            {"tick": 1, "unitActions": {}, "coreAction": {"type": "REPAIR_SHIELD"}, "intents": {}},
        ),
        "core_repair_shield_max": (
            {**base, "resources": 50},
            {"tick": 1, "unitActions": {}, "coreAction": {"type": "REPAIR_SHIELD"}, "intents": {}},
        ),
        "core_start_move_moving": (
            {
                **base,
                "core": {"id": "core", "position": [0, 0], "hp": 5, "shield": 5, "state": "MOVING"},
            },
            {
                "tick": 1,
                "unitActions": {},
                "coreAction": {"type": "START_MOVE", "direction": "UP"},
                "intents": {},
            },
        ),
        "core_cancel_move_normal": (
            base,
            {"tick": 1, "unitActions": {}, "coreAction": {"type": "CANCEL_MOVE"}, "intents": {}},
        ),
        "core_drop_beacon_not_carried": (
            base,
            {"tick": 1, "unitActions": {}, "coreAction": {"type": "DROP_BEACON"}, "intents": {}},
        ),
        "intents_dropped_for_invalid_actions": (
            base,
            {
                "tick": 1,
                "unitActions": {"w1": {"type": "MOVE", "direction": "DOWN"}},
                "coreAction": None,
                "intents": {"w1": "move-intent"},
            },
        ),
    }
    return cases


def test_plan_validation_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    cases = _validation_cases()
    for case in fixture["plan_validation"]:
        state, plan = cases[case["name"]]
        result = validate_plan(_snapshot(state), _plan(plan))
        assert _normalize_result(result) == case["expected"], case["name"]


def test_validation_drops_unknown_actions_fail_closed() -> None:
    base = _base_state()
    plan = _plan(
        {"tick": 1, "unitActions": {"w1": {"type": "HEAL"}}, "coreAction": None, "intents": {}}
    )
    result = validate_plan(_snapshot(base), plan)
    assert not result.valid
    assert result.repaired
    assert result.plan.unit_actions == ()
    assert [issue.code for issue in result.issues] == [ValidationCode.INVALID_HEAL]


def test_plan_dto_rejects_contradictory_action_shapes() -> None:
    with pytest.raises(ValueError):
        UnitAction(unit_id=EntityId("w1"), type=UnitActionType.MOVE)  # missing direction
    with pytest.raises(ValueError):
        UnitAction(
            unit_id=EntityId("r1"),
            type=UnitActionType.SHOOT,
        )  # shoot requires expected_cell
    with pytest.raises(ValueError):
        CoreAction(type=CoreActionType.SPAWN)  # spawn requires unit_role


def test_plan_dto_rejects_duplicate_units_and_bad_tick() -> None:
    action = UnitAction(unit_id=EntityId("w1"), type=UnitActionType.WAIT)
    with pytest.raises(ValueError):
        Plan(tick=1, unit_actions=(action, action))
    with pytest.raises(ValueError):
        Plan(tick=0)
    with pytest.raises(ValueError):
        Plan(tick=2**53)
