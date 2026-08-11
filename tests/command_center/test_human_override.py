"""Human override (P4-13): audit/apply/reject/expiry loop + oracle parity.

The deterministic override loop is the highest-priority control surface above
the agent plan. Every oracle-expressible field of the captured ``human_override``
fixture section must MATCH the TS oracle at the pinned commit; the Python result
additionally exposes an explicit ``stale`` flag for audit (registered allowed
difference). Behavior tests below pin the fail-closed contract: unknown units,
capability mismatches, malformed actions, stale overrides, and disabled stores
never weaken the base plan.
"""

from __future__ import annotations

from typing import Any, cast

from arena_hero_agent.command_center import (
    STALE_OVERRIDE_MAX_AGE_MS,
    GoalEntry,
    HumanCommand,
    HumanOverrideResult,
    HumanStore,
    action_from_wire,
    apply_human_overrides,
    basic_check,
    goal_action_for_unit,
    is_stale_override,
    read_and_apply_human_overrides,
)
from arena_hero_agent.domain import (
    Coordinate,
    Direction,
    EntityId,
    RulesVersion,
    UnitRole,
)
from arena_hero_agent.planning.plan import (
    CoreAction,
    CoreActionType,
    Plan,
    UnitAction,
    UnitActionType,
)
from arena_hero_agent.planning.planning_snapshot import (
    BeaconInfo,
    PlanningSnapshot,
    PlanningUnit,
    ResourceCellInfo,
)
from tests.strategies.fixture_loader import load_oracle_fixture

WORKER = "22222222-2222-2222-2222-222222222222"
RANGER = "33333333-3333-3333-3333-333333333333"
CORE = "core-1"
NOW = 1_752_000_000_000

_WIRE_DIRECTION_TO_DOMAIN = {
    "UP": Direction.NORTH,
    "DOWN": Direction.SOUTH,
    "LEFT": Direction.WEST,
    "RIGHT": Direction.EAST,
}
_DOMAIN_DIRECTION_TO_WIRE = {domain: wire for wire, domain in _WIRE_DIRECTION_TO_DOMAIN.items()}


def _snapshot(case_snapshot: object) -> PlanningSnapshot:
    s = cast(dict[str, Any], case_snapshot)
    units = tuple(
        PlanningUnit(
            id=EntityId(unit["id"]),
            unit_role=UnitRole(unit["role"].lower()),
            position=Coordinate(*unit["position"]),
            health=unit.get("health", 2),
            cargo=unit.get("cargo", 0),
        )
        for unit in s.get("units", [])
    )
    resource_cells = {
        key: ResourceCellInfo(position=Coordinate(*position), visible=True)
        for key, position in s.get("resourceCells", {}).items()
    }
    core_position = Coordinate(*s["corePosition"]) if s.get("corePosition") else None
    return PlanningSnapshot(
        tick=s.get("tick", 10),
        rules_version=RulesVersion("v0.14"),
        resources=s.get("resources", 10),
        resource_capacity=100,
        resource_space=s.get("resourceSpace", 90),
        population=s.get("population", 1),
        units=units,
        resource_cells=resource_cells,
        obstacle_cells=frozenset(s.get("obstacleCells", [])),
        enemy_cells=frozenset(),
        enemy_units=(),
        core_id=s.get("coreId", CORE) if core_position else None,
        core_position=core_position,
        core_health=s.get("coreHealth"),
        core_shield=s.get("coreShield"),
        core_state=s.get("coreState", "normal") if core_position else None,
        beacon=BeaconInfo(position=Coordinate(100, 100), status=None, carrier_id=None),
        threat_map={},
    )


def _store(raw: object) -> HumanStore:
    data = cast(dict[str, Any], raw)
    commands = [
        HumanCommand(
            id=command["id"],
            unit_id=command["unitId"],
            action=command["action"],
            created_at=command.get("createdAt", "x"),
        )
        for command in data.get("commands", [])
    ]
    goals = [
        GoalEntry(
            id=goal["id"],
            unit_id=goal["unitId"],
            kind=goal["kind"],
            target=tuple(goal["target"]),
            created_at=goal.get("createdAt", "x"),
        )
        for goal in data.get("goals", [])
    ]
    return HumanStore(
        version=data.get("version", 1),
        mode=data.get("mode", "override"),
        commands=commands,
        goals=goals,
        updated_at=data.get("updatedAt"),
        tenant="t1",
    )


def _wire_action(action: UnitAction | CoreAction | None) -> dict[str, object] | None:
    if action is None:
        return None
    out: dict[str, object] = {"type": action.type.value.upper()}
    if action.direction is not None:
        out["direction"] = _DOMAIN_DIRECTION_TO_WIRE[action.direction]
    if isinstance(action, UnitAction):
        if action.type is UnitActionType.SHOOT:
            out["targetId"] = None if action.target_id is None else action.target_id.value
        elif action.target_id is not None:
            out["targetId"] = action.target_id.value
        if action.expected_cell is not None:
            out["expectedCell"] = [action.expected_cell.x, action.expected_cell.y]
    if isinstance(action, CoreAction) and action.unit_role is not None:
        out["unitType"] = action.unit_role.name
    return out


def _normalize(result: HumanOverrideResult) -> dict[str, object]:
    unit_actions = {
        action.unit_id.value: _wire_action(action) for action in result.plan.unit_actions
    }
    return {
        "active": result.active,
        "applied": list(result.applied),
        "rejected": [
            {"unitId": rejection.unit_id, "reason": rejection.reason}
            for rejection in result.rejected
        ],
        "satisfied": list(result.satisfied),
        "updatedAt": result.updated_at,
        "unitActions": unit_actions,
        "coreAction": _wire_action(result.plan.core_action),
    }


def _now_ms_for(case: dict[str, object]) -> int:
    # A sentinel in the far future makes a valid ISO updatedAt stale without
    # touching invalid/missing timestamps (oracle parity).
    return 1 << 62


def test_human_override_matches_oracle_fixture() -> None:
    fixture = load_oracle_fixture()
    cases = fixture["human_override"]
    assert len(cases) >= 20
    for case in cases:
        result = apply_human_overrides(
            _snapshot(case["snapshot"]),
            Plan(tick=cast(dict[str, Any], case["snapshot"]).get("tick", 10)),
            _store(case["store"]),
            now_ms=_now_ms_for(case),
        )
        assert _normalize(result) == case["expected"], case["name"]
        if case["name"] == "stale_override_ignored":
            assert result.stale is True, "expired override must be marked stale"
        else:
            assert result.stale is False


def test_stale_override_is_ignored_wholesale() -> None:
    snapshot = _snapshot(
        {"units": [{"id": WORKER, "role": "WORKER", "position": [1, 1]}]}
    )
    store = _store(
        {
            "version": 1,
            "mode": "override",
            "updatedAt": "2026-08-12T00:00:00.000Z",
            "commands": [],
            "goals": [
                {"id": "g1", "unitId": WORKER, "kind": "goto", "target": [50, 50]}
            ],
        }
    )
    from datetime import datetime

    updated_at_ms = int(datetime.fromisoformat("2026-08-12T00:00:00+00:00").timestamp() * 1000)
    base = Plan(tick=10)
    result = apply_human_overrides(
        snapshot, base, store, now_ms=updated_at_ms + STALE_OVERRIDE_MAX_AGE_MS + 1
    )
    assert result.stale is True
    assert result.active is False
    assert result.plan is base
    assert result.applied == () and result.satisfied == ()
    assert result.updated_at == "2026-08-12T00:00:00.000Z"


def test_invalid_or_missing_updated_at_never_expires() -> None:
    snapshot = _snapshot(
        {"units": [{"id": WORKER, "role": "WORKER", "position": [1, 1]}]}
    )
    for updated_at in ("x", None):
        store = _store(
            {
                "version": 1,
                "mode": "override",
                "updatedAt": updated_at,
                "commands": [],
                "goals": [{"id": "g1", "unitId": WORKER, "kind": "goto", "target": [5, 1]}],
            }
        )
        assert is_stale_override(store, NOW) is False
        result = apply_human_overrides(snapshot, Plan(tick=10), store, now_ms=NOW)
        assert result.active is True and result.stale is False


def test_disabled_store_hands_control_to_agent() -> None:
    snapshot = _snapshot(
        {"units": [{"id": WORKER, "role": "WORKER", "position": [5, 1]}]}
    )
    store = _store(
        {
            "version": 1,
            "mode": "disabled",
            "updatedAt": "x",
            "commands": [{"id": "c1", "unitId": WORKER, "action": {"type": "HARVEST"}}],
            "goals": [],
        }
    )
    result = apply_human_overrides(snapshot, Plan(tick=10), store, now_ms=NOW)
    assert result.active is False
    assert result.applied == ()
    assert result.plan.unit_actions == ()
    assert result.updated_at is None  # oracle parity


def test_action_from_wire_parses_and_rejects() -> None:
    assert action_from_wire("u1", {"type": "MOVE", "direction": "UP"}, is_core=False) == UnitAction(
        unit_id=EntityId("u1"), type=UnitActionType.MOVE, direction=Direction.NORTH
    )
    assert action_from_wire("u1", {"type": "SWEEP", "direction": "DOWN"}, is_core=False) == (
        UnitAction(unit_id=EntityId("u1"), type=UnitActionType.SWEEP, direction=Direction.SOUTH)
    )
    shoot = action_from_wire(
        "u1", {"type": "SHOOT", "targetId": "", "expectedCell": [3, 3]}, is_core=False
    )
    assert isinstance(shoot, UnitAction)
    assert shoot.type is UnitActionType.SHOOT
    assert shoot.target_id is None  # empty-string targetId normalizes to null
    assert shoot.expected_cell == Coordinate(3, 3)
    start_move = action_from_wire(
        "core-1", {"type": "START_MOVE", "direction": "LEFT"}, is_core=True
    )
    assert start_move == CoreAction(type=CoreActionType.START_MOVE, direction=Direction.WEST)
    assert action_from_wire("core-1", {"type": "SPAWN", "unitType": "VANGUARD"}, is_core=True) == (
        CoreAction(type=CoreActionType.SPAWN, unit_role=UnitRole.VANGUARD)
    )
    assert action_from_wire("u1", {"type": "MOVE"}, is_core=False) is None  # missing direction
    assert action_from_wire("u1", {"type": "MOVE", "direction": "NOPE"}, is_core=False) is None
    assert action_from_wire("core-1", {"type": "SPAWN", "unitType": "DRONE"}, is_core=True) is None
    assert action_from_wire("u1", {"type": "START_MOVE", "direction": "UP"}, is_core=False) is None
    assert action_from_wire("u1", {"type": "HARVEST"}, is_core=False) is not None
    assert action_from_wire("u1", {"type": "HARVEST"}, is_core=True) is None


def test_basic_check_rejects_unknown_and_capability_mismatch() -> None:
    snapshot = _snapshot(
        {"units": [{"id": WORKER, "role": "WORKER", "position": [1, 1]}]}
    )
    assert basic_check(snapshot, "ghost-unit", _move_up()) == "unknown_unit"
    assert basic_check(snapshot, WORKER, _shoot()) == "action_requires_ranger"
    assert basic_check(snapshot, WORKER, _move_up()) is None
    vanguard = _snapshot(
        {"units": [{"id": "v1", "role": "VANGUARD", "position": [1, 1]}]}
    )
    assert basic_check(vanguard, "v1", _shoot()) == "action_requires_ranger"


def test_goal_action_for_unit_mine_and_goto() -> None:
    snapshot = _snapshot(
        {
            "units": [{"id": WORKER, "role": "WORKER", "position": [1, 1]}],
            "resourceCells": {"5,1": [5, 1]},
        }
    )
    unit = snapshot.units[0]
    mine = GoalEntry(id="g1", unit_id=WORKER, kind="mine", target=(5, 1), created_at="x")
    action = goal_action_for_unit(snapshot, unit, mine)
    assert isinstance(action, UnitAction) and action.type is UnitActionType.MOVE
    exhausted = GoalEntry(id="g2", unit_id=WORKER, kind="mine", target=(9, 9), created_at="x")
    assert goal_action_for_unit(snapshot, unit, exhausted) is None  # satisfied
    arrived = _snapshot(
        {"units": [{"id": WORKER, "role": "WORKER", "position": [5, 1]}]}
    )
    assert goal_action_for_unit(arrived, arrived.units[0], mine) == UnitAction(
        unit_id=EntityId(WORKER), type=UnitActionType.HARVEST
    )


def test_read_and_apply_human_overrides_uses_persistence(tmp_path) -> None:
    from arena_hero_agent.command_center import write_human_store

    store = _store(
        {
            "version": 1,
            "mode": "override",
            "updatedAt": "x",
            "commands": [{"id": "c1", "unitId": WORKER, "action": {"type": "HARVEST"}}],
            "goals": [],
        }
    )
    write_human_store(tmp_path, "t1", store, now_ms=NOW)
    snapshot = _snapshot(
        {
            "units": [{"id": WORKER, "role": "WORKER", "position": [5, 1]}],
            "resourceCells": {"5,1": [5, 1]},
        }
    )
    result = read_and_apply_human_overrides(
        str(tmp_path), "t1", snapshot, Plan(tick=10), now_ms=NOW
    )
    assert result.active is True
    assert result.applied == (WORKER,)


def _move_up() -> UnitAction:
    return UnitAction(unit_id=EntityId("u"), type=UnitActionType.MOVE, direction=Direction.NORTH)


def _shoot() -> UnitAction:
    return UnitAction(
        unit_id=EntityId("u"),
        type=UnitActionType.SHOOT,
        expected_cell=Coordinate(3, 3),
    )
