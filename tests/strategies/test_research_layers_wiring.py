"""Wiring tests for the research layers composed into the live decider.

Each layer is a pure, already-tested module that :mod:`ComposedDecider` gates
behind its own config flag.  The flags default to ``True``: the layers are
basic, tested capabilities, not experimental switches.  This file pins three
invariants:

- the flags default to ``True``;
- an explicit all-``True`` config is byte-identical to ``ComposedDecider()``
  across a multi-tick fixture sequence;
- enabling each flag changes the merged plan in the expected way, isolated
  against an explicit all-off baseline.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from arena_hero_agent.application.turns import PlayerLifecycle, TurnEvent, TurnObservation
from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    Coordinate,
    Direction,
    EntityId,
    UnitRole,
    WorldProjection,
)
from arena_hero_agent.planning import (
    BeaconInfo,
    CoreActionType,
    MoveFailureEvent,
    Plan,
    PlanningSnapshot,
    PlanningUnit,
    ResourceCellInfo,
    UnitActionType,
)
from arena_hero_agent.planning import (
    UnitAction as PlanningUnitAction,
)
from arena_hero_agent.strategies import ComposedDecider, ComposedDeciderConfig
from arena_hero_agent.strategies.composition import _apply_raid_strike, snapshot_from_turn
from arena_hero_agent.strategies.movement_guard import forced_escape_step
from arena_hero_agent.strategies.raid_quota import StrikeGroup

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
    move_failures: tuple[MoveFailureEvent, ...] = (),
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
        move_failures=move_failures,
    )


def _action(plan: Plan, unit_id: str) -> UnitActionType | None:
    action = plan.action_for(unit_id)
    return None if action is None else action.type


def _direction(plan: Plan, unit_id: str) -> Direction | None:
    action = plan.action_for(unit_id)
    return None if action is None or action.direction is None else action.direction


def _all_off() -> ComposedDeciderConfig:
    """Explicit baseline with every research layer disabled for isolation tests."""

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


def test_research_flags_default_on() -> None:
    config = ComposedDeciderConfig()
    assert config.movement_guard_enabled is True
    assert config.economy_budget_enabled is True
    assert config.economy_expansion_enabled is True
    assert config.raid_quota_enabled is True
    assert config.exploration_v2_enabled is True
    assert config.respawn_recovery_enabled is True


def test_default_byte_identical_to_explicit_all_true() -> None:
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
            movement_guard_enabled=True,
            economy_budget_enabled=True,
            economy_expansion_enabled=True,
            raid_quota_enabled=True,
            exploration_v2_enabled=True,
            respawn_recovery_enabled=True,
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

    disabled = run(ComposedDecider(_all_off()))
    enabled = run(ComposedDecider(replace(_all_off(), movement_guard_enabled=True)))

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

    disabled = run(ComposedDecider(_all_off()))
    enabled = run(ComposedDecider(replace(_all_off(), movement_guard_enabled=True)))

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

    baseline = ComposedDecider(_all_off()).decide_snapshot(snapshot)
    assert baseline.core_action is not None
    assert baseline.core_action.type is CoreActionType.SPAWN

    gated = ComposedDecider(replace(_all_off(), economy_budget_enabled=True)).decide_snapshot(
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
    # r2/r3 are confirmed strikers but sit 6 cells out (beyond shot range 3),
    # so they now close the distance instead of wasting out-of-range shots;
    # vanguards advance on the core.
    assert _action(final, "r2") is UnitActionType.MOVE
    assert _action(final, "r3") is UnitActionType.MOVE
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

    disabled = run(ComposedDecider(_all_off()))
    enabled = run(ComposedDecider(replace(_all_off(), movement_guard_enabled=True)))

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

    default = ComposedDecider(_all_off()).decide_snapshot(make())
    enabled = ComposedDecider(replace(_all_off(), economy_expansion_enabled=True)).decide_snapshot(
        make()
    )

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
    default = ComposedDecider(_all_off()).decide_snapshot(snapshot)
    enabled = ComposedDecider(replace(_all_off(), economy_expansion_enabled=True)).decide_snapshot(
        snapshot
    )

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


def test_raid_strike_vanguard_sweeps_adjacent_core_and_ranger_holds_range() -> None:
    """Regression for the two strike-group combat gaps.

    An adjacent Vanguard must SWEEP the enemy Core (a MOVE only bumps into the
    occupied Core cell and never damages it), and a Ranger must only SHOOT when
    actually on a firing line within range 3 — otherwise it advances.
    """
    target = Coordinate(5, 5)
    adjacent_vanguard = _vanguard("v-adj", 4, 5)
    far_vanguard = _vanguard("v-far", 0, 5)
    in_range_ranger = _ranger("r-in", 3, 5)
    out_of_range_ranger = _ranger("r-out", 0, 4)
    units = (adjacent_vanguard, far_vanguard, in_range_ranger, out_of_range_ranger)

    snapshot = _snapshot(tick=1, units=units, core_position=Coordinate(0, 0))
    base_plan = Plan(
        tick=1,
        unit_actions=tuple(
            PlanningUnitAction(unit_id=unit.id, type=UnitActionType.WAIT) for unit in units
        ),
        core_action=None,
    )
    strike = StrikeGroup(
        vanguard_ids=("v-adj", "v-far"),
        ranger_ids=("r-in", "r-out"),
    )

    struck = _apply_raid_strike(base_plan, snapshot, target, strike)

    adjacent_action = struck.action_for("v-adj")
    assert adjacent_action is not None
    assert adjacent_action.type is UnitActionType.SWEEP
    assert adjacent_action.direction is Direction.EAST

    far_action = struck.action_for("v-far")
    assert far_action is not None
    assert far_action.type is UnitActionType.MOVE
    assert far_action.direction is Direction.EAST

    in_range_action = struck.action_for("r-in")
    assert in_range_action is not None
    assert in_range_action.type is UnitActionType.SHOOT
    assert in_range_action.expected_cell == target

    out_action = struck.action_for("r-out")
    assert out_action is not None
    assert out_action.type is UnitActionType.MOVE
    assert out_action.direction is Direction.EAST


def test_barren_migration_routes_around_blocking_worker() -> None:
    """Regression for CORE_DESTINATION_OCCUPIED migration stalls.

    The engine rejects a Core START_MOVE into an occupied cell. A worker
    standing on the Core's natural toward-origin step must not leave the Core
    retrying that blocked cell forever — the migration step routes around the
    friendly unit instead.
    """
    # Core at [5,0]; origin is west. A worker occupies [4,0], the natural west
    # step. With no resources the barren migration fires and must avoid [4,0].
    config = replace(_all_off(), barren_migration_enabled=True, barren_migration_ticks=2)
    decider = ComposedDecider(config)
    plan = None
    for tick in range(1, 8):
        plan = decider.decide_snapshot(
            _snapshot(
                tick=tick,
                units=(_worker("w-block", 4, 0),),
                core_position=Coordinate(5, 0),
            )
        )
    assert plan is not None
    assert plan.core_action is not None
    assert plan.core_action.type is CoreActionType.START_MOVE
    # The Core must not try to move west into the worker-occupied cell.
    assert plan.core_action.direction is not Direction.WEST


def test_decider_state_summary_exposes_hook_state() -> None:
    """The read-only state digest is what tick_state telemetry persists."""
    decider = ComposedDecider(ComposedDeciderConfig())
    summary = decider.state_summary()
    assert set(summary) >= {
        "barrenMigration",
        "noWorkerDeadlockTicks",
        "stuckResources",
        "respawnRecovery",
        "raid",
        "loopTrails",
        "moveBackoff",
    }
    assert summary["barrenMigration"]["active"] is False
    assert summary["noWorkerDeadlockTicks"] == 0
    # The digest is read-only: calling it must not mutate decision state.
    again = decider.state_summary()
    assert again == summary


def test_no_worker_deadlock_self_destructs_core() -> None:
    """Regression for the pop=0 + under-priced-worker deadlock.

    With no workers alive and stock below the worker price, income can never
    resume (nothing can harvest), so after a grace window the Core must
    self-destruct to force a respawn instead of waiting forever.
    """
    config = replace(_all_off(), economy_expansion_enabled=True)
    decider = ComposedDecider(config)
    core_actions: list[CoreActionType | None] = []
    for tick in range(1, 20):
        plan = decider.decide_snapshot(
            _snapshot(tick=tick, units=(), resources=4, core_position=Coordinate(0, 0))
        )
        core_actions.append(
            plan.core_action.type if plan.core_action is not None else None
        )
    assert CoreActionType.SELF_DESTRUCT in core_actions
    # It must not self-destruct on the very first deadlocked tick (grace window).
    assert core_actions[0] is not CoreActionType.SELF_DESTRUCT


def test_terrain_trap_hook_spares_cargo_carrying_worker() -> None:
    """FFA regression: a cargo-carrying worker standing on the Core is
    mid-deposit, not trapped.  Killing it dropped the cargo and delayed the
    economy (observed: worker walked 15 cells home, stood on the Core with
    cargo=1, and was SELF_DESTRUCTed on the stuck-resources timer)."""
    config = replace(_all_off(), stuck_resources_enabled=True)
    decider = ComposedDecider(config)
    core_actions: list[UnitActionType | None] = []
    for tick in range(1, 30):
        plan = decider.decide_snapshot(
            _snapshot(
                tick=tick,
                units=(_worker("w1", 0, 0, cargo=1),),
                resources=10,
                population=1,
                core_position=Coordinate(0, 0),
            )
        )
        core_actions.append(_action(plan, "w1"))
    assert UnitActionType.SELF_DESTRUCT not in core_actions


def test_terrain_trap_hook_fires_only_when_replacement_affordable() -> None:
    """The trap break only fires when the Core can immediately afford a
    replacement worker; destroying the fleet's only worker below the worker
    price would just shrink the economy."""
    config = replace(_all_off(), stuck_resources_enabled=True)
    poor = ComposedDecider(config)
    rich = ComposedDecider(config)
    poor_actions: list[UnitActionType | None] = []
    rich_actions: list[UnitActionType | None] = []
    for tick in range(1, 30):
        poor_plan = poor.decide_snapshot(
            _snapshot(
                tick=tick,
                units=(_worker("w1", 0, 0),),
                resources=1,
                population=1,
                core_position=Coordinate(0, 0),
            )
        )
        rich_plan = rich.decide_snapshot(
            _snapshot(
                tick=tick,
                units=(_worker("w1", 0, 0),),
                resources=10,
                population=1,
                core_position=Coordinate(0, 0),
            )
        )
        poor_actions.append(_action(poor_plan, "w1"))
        rich_actions.append(_action(rich_plan, "w1"))
    assert UnitActionType.SELF_DESTRUCT not in poor_actions
    assert UnitActionType.SELF_DESTRUCT in rich_actions


def test_terrain_map_learns_terrain_move_failures() -> None:
    """A MOVE_BLOCKED_TERRAIN rejection pairs with the previous planned
    direction to infer an obstacle the vision never reported; the next tick's
    pathfinding must avoid it (production: t1 worker stuck 13+ ticks re-issuing
    move north into an invisible blocked cell)."""
    decider = ComposedDecider(_all_off())
    # The previous tick planned EAST; the engine rejected it as terrain.
    decider._previous_planned_directions = {"w1": Direction.EAST}
    snapshot = _snapshot(
        tick=2,
        units=(_worker("w1", 0, 0),),
        core_position=Coordinate(0, 0),
        move_failures=(MoveFailureEvent(unit_id="w1", reason="MOVE_BLOCKED_TERRAIN"),),
    )
    decider.decide_snapshot(snapshot)
    assert decider._terrain_map.known_obstacle_count == 1
    assert "1,0" in decider._terrain_map


def test_terrain_map_ignores_transient_move_failures() -> None:
    """Occupancy reasons are transient (units move); only the terrain reason
    becomes permanent obstacle knowledge."""
    decider = ComposedDecider(_all_off())
    decider._previous_planned_directions = {"w1": Direction.EAST}
    snapshot = _snapshot(
        tick=2,
        units=(_worker("w1", 0, 0),),
        core_position=Coordinate(0, 0),
        move_failures=(
            MoveFailureEvent(unit_id="w1", reason="MOVE_DESTINATION_OCCUPIED"),
            MoveFailureEvent(unit_id="w1", reason="CELL_UNIT_LIMIT"),
        ),
    )
    decider.decide_snapshot(snapshot)
    assert decider._terrain_map.known_obstacle_count == 0


def test_forced_escape_avoids_known_obstacles() -> None:
    """The escape planner must not walk into a known obstacle cell (regression:
    escape kept returning the blocked primary direction and the worker never
    left the death loop)."""
    step = forced_escape_step(
        Coordinate(0, 0),
        Coordinate(0, 5),  # target lies south
        frozenset({Coordinate(0, 1)}),  # south is terrain-blocked
        repath_side=0,
    )
    assert step is not Direction.SOUTH
    assert step is not None


def test_snapshot_from_turn_extracts_move_failures() -> None:
    """Move-failure events survive the turn -> snapshot projection."""
    observation = TurnObservation(
        tick=2,
        lifecycle=PlayerLifecycle.ACTIVE,
        resources=5,
        population=1,
        projection=WorldProjection(tick=2, rules_version=RULES),
        events=(
            TurnEvent(
                id=EntityId("ev-1"),
                tick=1,
                kind="UNIT_MOVE_FAILED",
                reason="MOVE_BLOCKED_TERRAIN",
                actor_id=EntityId("w1"),
            ),
            TurnEvent(
                id=EntityId("ev-2"),
                tick=1,
                kind="UNIT_HARVESTED",
                actor_id=EntityId("w1"),
            ),
        ),
    )
    snapshot = snapshot_from_turn(observation)
    assert snapshot.move_failures == (
        MoveFailureEvent(unit_id="w1", reason="MOVE_BLOCKED_TERRAIN"),
    )
