"""Wiring tests for the three disabled-by-default research layers.

Each layer is a pure, already-tested module that :mod:`ComposedDecider` now
gates behind its own config flag.  This file pins three invariants:

- all three flags default to ``False``;
- an explicit all-``False`` config is byte-identical to ``ComposedDecider()``
  across a multi-tick fixture sequence;
- enabling each flag changes the merged plan in the expected way.
"""

from __future__ import annotations

from typing import cast

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
    CoreActionType,
    Plan,
    PlanningSnapshot,
    PlanningUnit,
    ResourceCellInfo,
    UnitActionType,
)
from arena_hero_agent.strategies import ComposedDecider, ComposedDeciderConfig

RULES = CURRENT_RULES_VERSION


def _cell(x: int, y: int) -> ResourceCellInfo:
    return ResourceCellInfo(position=Coordinate(x, y), visible=True, last_seen_tick=1)


def _worker(identifier: str, x: int, y: int, *, cargo: int = 0, health: int = 2) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(identifier),
        unit_role=UnitRole.WORKER,
        position=Coordinate(x, y),
        health=health,
        cargo=cargo,
    )


def _vanguard(identifier: str, x: int, y: int, *, health: int = 4) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(identifier),
        unit_role=UnitRole.VANGUARD,
        position=Coordinate(x, y),
        health=health,
        cargo=0,
    )


def _ranger(identifier: str, x: int, y: int, *, health: int = 2) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(identifier),
        unit_role=UnitRole.RANGER,
        position=Coordinate(x, y),
        health=health,
        cargo=0,
    )


def _snapshot(
    *,
    tick: int = 1,
    units: tuple[PlanningUnit, ...] = (),
    resources: int = 0,
    resource_capacity: int = 100,
    population: int = 0,
    resource_cells: dict[str, ResourceCellInfo] | None = None,
    enemy_cells: frozenset[str] = frozenset(),
    enemy_units: tuple = (),
    core_position: Coordinate | None = None,
    core_state: str = "normal",
) -> PlanningSnapshot:
    return PlanningSnapshot(
        tick=tick,
        rules_version=RULES,
        resources=resources,
        resource_capacity=resource_capacity,
        resource_space=resource_capacity - resources,
        population=population,
        units=units,
        resource_cells={} if resource_cells is None else resource_cells,
        obstacle_cells=frozenset(),
        enemy_cells=enemy_cells,
        enemy_units=enemy_units,
        core_id=None if core_position is None else "core",
        core_position=core_position,
        core_health=None if core_position is None else 5,
        core_shield=None if core_position is None else 5,
        core_state=None if core_position is None else core_state,
        beacon=BeaconInfo(position=Coordinate(0, 0), status=None),
        threat_map={},
    )


def _action(plan: Plan, unit_id: str) -> UnitActionType | None:
    action = plan.action_for(unit_id)
    return None if action is None else action.type


def _direction(plan: Plan, unit_id: str) -> Direction | None:
    action = plan.action_for(unit_id)
    return None if action is None or action.direction is None else action.direction


def test_research_flags_default_off() -> None:
    config = ComposedDeciderConfig()
    assert config.movement_guard_enabled is False
    assert config.economy_budget_enabled is False
    assert config.economy_expansion_enabled is False
    assert config.raid_quota_enabled is False


def test_default_byte_identical_to_explicit_all_false() -> None:
    cells = {"5,0": _cell(5, 0)}
    ticks = [
        _snapshot(
            tick=1,
            units=(_worker("w1", 0, 0), _worker("w2", 2, 0)),
            resource_cells=cells,
            core_position=Coordinate(0, 0),
        ),
        _snapshot(
            tick=2,
            units=(_worker("w1", 0, 1), _worker("w2", 3, 0)),
            resource_cells=cells,
            core_position=Coordinate(0, 0),
        ),
        _snapshot(
            tick=3,
            units=(_worker("w1", 0, 0), _worker("w2", 4, 0)),
            resource_cells=cells,
            core_position=Coordinate(0, 0),
        ),
    ]
    default = ComposedDecider()
    explicit = ComposedDecider(
        ComposedDeciderConfig(
            movement_guard_enabled=False,
            economy_budget_enabled=False,
            economy_expansion_enabled=False,
            raid_quota_enabled=False,
        )
    )
    for snapshot in ticks:
        assert default.decide_snapshot(snapshot) == explicit.decide_snapshot(snapshot)


def test_movement_guard_blocks_looping_resource_target() -> None:
    cells = {"5,0": _cell(5, 0)}
    positions = [Coordinate(0, 0), Coordinate(0, 1)] * 3

    def run(decider: ComposedDecider) -> list[Plan]:
        return [
            decider.decide_snapshot(
                _snapshot(
                    tick=index,
                    units=(_worker("w1", position.x, position.y),),
                    resource_cells=cells,
                    core_position=Coordinate(0, 0),
                )
            )
            for index, position in enumerate(positions, start=1)
        ]

    disabled = run(ComposedDecider())
    enabled = run(ComposedDecider(ComposedDeciderConfig(movement_guard_enabled=True)))

    assert _action(disabled[0], "w1") is UnitActionType.MOVE
    assert _action(disabled[-1], "w1") is UnitActionType.MOVE
    assert _action(enabled[0], "w1") is UnitActionType.MOVE
    # Once the guard detects the confined footprint it blocks the target cell,
    # forcing a reassignment away from the looping GO_RESOURCE target.
    assert _action(enabled[-1], "w1") is UnitActionType.WAIT


def test_movement_guard_forces_escape_direction() -> None:
    cells = {"2,2": _cell(2, 2), "10,0": _cell(10, 0)}
    positions = [Coordinate(0, 0), Coordinate(0, 1)] * 3

    def run(decider: ComposedDecider) -> Plan:
        for index, position in enumerate(positions, start=1):
            plan = decider.decide_snapshot(
                _snapshot(
                    tick=index,
                    units=(_worker("w1", position.x, position.y),),
                    resource_cells=cells,
                    core_position=Coordinate(0, 0),
                )
            )
        return plan

    disabled = run(ComposedDecider())
    enabled = run(ComposedDecider(ComposedDeciderConfig(movement_guard_enabled=True)))

    assert _direction(disabled, "w1") is Direction.EAST
    assert _direction(enabled, "w1") is Direction.SOUTH


def test_economy_budget_skips_spawn_when_heal_reserve_short() -> None:
    snapshot = _snapshot(
        tick=1,
        units=(_vanguard("v1", 0, 0, health=1),),
        resources=5,
        resource_capacity=10,
        population=0,
        core_position=Coordinate(0, 0),
    )

    baseline = ComposedDecider().decide_snapshot(snapshot)
    assert baseline.core_action is not None
    assert baseline.core_action.type is CoreActionType.SPAWN

    gated = ComposedDecider(ComposedDeciderConfig(economy_budget_enabled=True)).decide_snapshot(
        snapshot
    )
    assert gated.core_action is not None
    assert gated.core_action.type is CoreActionType.WAIT


def test_raid_quota_strikes_confirmed_stationary_core() -> None:
    units = (
        _vanguard("v1", 0, 0),
        _vanguard("v2", 1, 0),
        _vanguard("v3", 0, 1),
        _ranger("r1", 1, 1),
        _ranger("r2", -1, 1),
        _ranger("r3", 0, -1),
    )

    def make(tick: int) -> PlanningSnapshot:
        return _snapshot(
            tick=tick,
            units=units,
            enemy_cells=frozenset({"5,5"}),
            core_position=Coordinate(0, 0),
        )

    decider = ComposedDecider(ComposedDeciderConfig(raid_quota_enabled=True))
    plans = [decider.decide_snapshot(make(tick)) for tick in (1, 2, 3)]

    # Not confirmed until the third consecutive observation.
    assert _action(plans[0], "r2") is not UnitActionType.SHOOT
    assert _action(plans[1], "r2") is not UnitActionType.SHOOT

    final = plans[2]
    assert _action(final, "r2") is UnitActionType.SHOOT
    r2_action = final.action_for("r2")
    assert r2_action is not None
    assert r2_action.expected_cell == Coordinate(5, 5)
    assert _action(final, "r3") is UnitActionType.SHOOT
    assert _action(final, "v3") is UnitActionType.MOVE
    # Home-defense members stay behind and keep their baseline actions.
    assert _action(final, "v1") is not UnitActionType.SHOOT
    assert _action(final, "r1") is not UnitActionType.SHOOT


def test_research_config_validates() -> None:
    with pytest.raises(TypeError, match="movement_guard_enabled"):
        ComposedDeciderConfig(movement_guard_enabled=cast(bool, "yes"))
    with pytest.raises(TypeError, match="economy_budget_enabled"):
        ComposedDeciderConfig(economy_budget_enabled=cast(bool, "yes"))
    with pytest.raises(TypeError, match="raid_quota_enabled"):
        ComposedDeciderConfig(raid_quota_enabled=cast(bool, "yes"))
    with pytest.raises(ValueError, match="movement_loop_window"):
        ComposedDeciderConfig(movement_loop_window=0)
    with pytest.raises(ValueError, match="raid_min_fighters"):
        ComposedDeciderConfig(raid_min_fighters=0)
    with pytest.raises(ValueError, match="raid_max_distance"):
        ComposedDeciderConfig(raid_max_distance=-1)


def test_cargo_spin_core_self_heal_starts_core_move() -> None:
    positions = [Coordinate(10, 0), Coordinate(10, 1)] * 8

    def run(decider: ComposedDecider) -> Plan:
        for index, position in enumerate(positions, start=1):
            plan = decider.decide_snapshot(
                _snapshot(
                    tick=index,
                    units=(_worker("w1", position.x, position.y, cargo=1),),
                    core_position=Coordinate(0, 0),
                )
            )
        return plan

    disabled = run(ComposedDecider())
    enabled = run(ComposedDecider(ComposedDeciderConfig(movement_guard_enabled=True)))

    assert (
        disabled.core_action is None or disabled.core_action.type is not CoreActionType.START_MOVE
    )
    assert enabled.core_action is not None
    assert enabled.core_action.type is CoreActionType.START_MOVE
    assert enabled.core_action.direction is Direction.EAST


def test_raid_target_stays_stable_after_confirmation() -> None:
    units = (
        _vanguard("v1", 0, 0),
        _vanguard("v2", 1, 0),
        _vanguard("v3", 0, 1),
        _ranger("r1", 1, 1),
        _ranger("r2", -1, 1),
        _ranger("r3", 0, -1),
    )

    def make(tick: int) -> PlanningSnapshot:
        return _snapshot(
            tick=tick,
            units=units,
            enemy_cells=frozenset({"5,5"}),
            core_position=Coordinate(0, 0),
        )

    decider = ComposedDecider(ComposedDeciderConfig(raid_quota_enabled=True))
    decider.decide_snapshot(make(1))
    decider.decide_snapshot(make(2))
    decider.decide_snapshot(make(3))

    assert decider.raid_state.enabled is True
    assert decider.raid_state.core_position == Coordinate(5, 5)
    assert decider.raid_state.acquired_tick == 3

    decider.decide_snapshot(make(4))
    # The target is retained across ticks instead of being re-acquired.
    assert decider.raid_state.core_position == Coordinate(5, 5)
    assert decider.raid_state.acquired_tick == 3


def test_raid_target_clears_when_enemy_vanishes() -> None:
    units = (
        _vanguard("v1", 0, 0),
        _vanguard("v2", 1, 0),
        _vanguard("v3", 0, 1),
        _ranger("r1", 1, 1),
        _ranger("r2", -1, 1),
        _ranger("r3", 0, -1),
    )

    decider = ComposedDecider(ComposedDeciderConfig(raid_quota_enabled=True))
    for tick in (1, 2, 3):
        decider.decide_snapshot(
            _snapshot(
                tick=tick,
                units=units,
                enemy_cells=frozenset({"5,5"}),
                core_position=Coordinate(0, 0),
            )
        )
    assert decider.raid_state.enabled is True

    plan = decider.decide_snapshot(
        _snapshot(
            tick=4,
            units=units,
            enemy_cells=frozenset(),
            core_position=Coordinate(0, 0),
        )
    )
    assert decider.raid_state.enabled is False
    assert decider.raid_state.core_position is None
    assert all(action.type is not UnitActionType.SHOOT for action in plan.unit_actions)


def test_raid_recalls_when_fighters_drop_below_minimum() -> None:
    full_units = (
        _vanguard("v1", 0, 0),
        _vanguard("v2", 1, 0),
        _vanguard("v3", 0, 1),
        _ranger("r1", 1, 1),
        _ranger("r2", -1, 1),
        _ranger("r3", 0, -1),
    )
    decider = ComposedDecider(ComposedDeciderConfig(raid_quota_enabled=True))
    for tick in (1, 2, 3):
        decider.decide_snapshot(
            _snapshot(
                tick=tick,
                units=full_units,
                enemy_cells=frozenset({"5,5"}),
                core_position=Coordinate(0, 0),
            )
        )
    assert decider.raid_state.enabled is True

    decider.decide_snapshot(
        _snapshot(
            tick=4,
            units=(_vanguard("v1", 0, 0), _ranger("r1", 1, 1)),
            enemy_cells=frozenset({"5,5"}),
            core_position=Coordinate(0, 0),
        )
    )
    assert decider.raid_state.recall is True
    assert decider.raid_state.core_position is None


def test_raid_replacement_queue_tracks_unit_churn() -> None:
    decider = ComposedDecider(ComposedDeciderConfig(raid_quota_enabled=True))

    decider.decide_snapshot(
        _snapshot(
            tick=1,
            units=(
                _vanguard("v1", 0, 0),
                _vanguard("v2", 1, 0),
                _ranger("r1", 1, 1),
            ),
        )
    )
    assert decider.replacement_queue.to_mapping() == {}

    decider.decide_snapshot(
        _snapshot(
            tick=2,
            units=(_vanguard("v1", 0, 0), _vanguard("v2", 1, 0)),
        )
    )
    assert decider.replacement_queue.to_mapping() == {"ranger": 1}

    decider.decide_snapshot(
        _snapshot(
            tick=3,
            units=(
                _vanguard("v1", 0, 0),
                _vanguard("v2", 1, 0),
                _ranger("r2", 1, 1),
            ),
        )
    )
    assert decider.replacement_queue.to_mapping() == {}


def test_cargo_spin_config_validates() -> None:
    with pytest.raises(ValueError, match="movement_cargo_spin_ticks"):
        ComposedDeciderConfig(movement_cargo_spin_ticks=0)


def test_economy_expansion_routes_idle_workers_to_explore() -> None:
    def make() -> PlanningSnapshot:
        return _snapshot(
            tick=1,
            units=(_worker("w1", 0, 0), _worker("w2", 1, 0), _worker("w3", 2, 0)),
            core_position=Coordinate(0, 0),
        )

    default = ComposedDecider().decide_snapshot(make())
    enabled = ComposedDecider(
        ComposedDeciderConfig(economy_expansion_enabled=True)
    ).decide_snapshot(make())

    for unit_id in ("w1", "w2", "w3"):
        assert _action(default, unit_id) is UnitActionType.WAIT
        assert _action(enabled, unit_id) is UnitActionType.MOVE


def test_economy_expansion_spawns_worker_from_inflight_deposit() -> None:
    snapshot = _snapshot(
        tick=1,
        units=(_worker("w1", 0, 0, cargo=1),),
        resources=4,
        population=1,
        core_position=Coordinate(0, 0),
    )
    default = ComposedDecider().decide_snapshot(snapshot)
    enabled = ComposedDecider(
        ComposedDeciderConfig(economy_expansion_enabled=True)
    ).decide_snapshot(snapshot)

    assert default.core_action is None
    assert enabled.core_action is not None
    assert enabled.core_action.type is CoreActionType.SPAWN
    assert enabled.core_action.unit_role is UnitRole.WORKER


def test_economy_expansion_waits_when_unaffordable() -> None:
    snapshot = _snapshot(
        tick=1,
        units=(_worker("w1", 0, 0),),
        resources=3,
        population=1,
        core_position=Coordinate(0, 0),
    )
    enabled = ComposedDecider(
        ComposedDeciderConfig(economy_expansion_enabled=True)
    ).decide_snapshot(snapshot)
    assert enabled.core_action is not None
    assert enabled.core_action.type is CoreActionType.WAIT
