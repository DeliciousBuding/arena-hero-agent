"""Respawn recovery tests: teleport detection, latch, and composed decider wiring."""

from __future__ import annotations

from typing import cast

import pytest

from arena_hero_agent.domain import CURRENT_RULES_VERSION, Coordinate, UnitRole
from arena_hero_agent.planning import Plan, PlanningSnapshot
from arena_hero_agent.strategies import ComposedDecider, ComposedDeciderConfig
from arena_hero_agent.strategies.respawn_recovery import (
    BarrenMigrationState,
    RespawnRecoveryState,
    detect_respawn,
)

RULES = CURRENT_RULES_VERSION


def test_barren_migration_requires_consecutive_ticks() -> None:
    state = BarrenMigrationState()
    assert state.observe(has_resource_cells=False, tick=1, core_migrating=False) is False
    assert (
        state.observe(
            has_resource_cells=False,
            tick=1 + 29,
            core_migrating=False,
        )
        is False
    )
    assert state.observe(has_resource_cells=False, tick=31, core_migrating=False) is True


def test_barren_migration_economic_activity_resets_latch() -> None:
    state = BarrenMigrationState()
    # First barren tick only records the start; the threshold elapses later.
    assert (
        state.observe(
            has_resource_cells=False,
            tick=40,
            core_migrating=False,
            barren_threshold=10,
        )
        is False
    )
    # Migration latches once the barren threshold has passed.
    assert (
        state.observe(
            has_resource_cells=False,
            tick=50,
            core_migrating=False,
            barren_threshold=10,
        )
        is True
    )
    assert state.migration_active is True
    # A deposit or spawn proves the region yields: cancel the migration latch.
    assert (
        state.observe(
            has_resource_cells=False,
            tick=51,
            core_migrating=False,
            barren_threshold=10,
            economic_activity=True,
        )
        is False
    )
    assert state.migration_active is False
    assert state.barren_since_tick is None
    # The counter restarts from scratch.
    assert state.observe(has_resource_cells=False, tick=52, core_migrating=False) is False
    assert state.barren_since_tick == 52


def test_barren_migration_activity_resets_phantom_reset_count() -> None:
    state = BarrenMigrationState()
    # Two phantom resets (visible cells that never yield) would normally arm
    # the phantom protection at max_resets=3.
    for tick in (10, 20):
        assert (
            state.observe(
                has_resource_cells=True,
                tick=tick,
                core_migrating=False,
                barren_threshold=5,
                max_resets=3,
            )
            is False
        )
    assert state.reset_count == 2
    # Hard economic evidence fully resets the phantom counter too.
    state.observe(
        has_resource_cells=False,
        tick=21,
        core_migrating=False,
        economic_activity=True,
    )
    assert state.reset_count == 0


def test_detect_respawn_requires_previous_observation() -> None:
    assert detect_respawn(None, Coordinate(100, 100)) is False


def test_detect_respawn_rejects_normal_step() -> None:
    assert detect_respawn(Coordinate(0, 0), Coordinate(1, 0)) is False
    assert detect_respawn(Coordinate(0, 0), Coordinate(31, 0)) is False


def test_detect_respawn_detects_teleport() -> None:
    assert detect_respawn(Coordinate(0, 0), Coordinate(32, 0)) is True
    assert detect_respawn(Coordinate(0, 0), Coordinate(-100, 100)) is True


def test_detect_respawn_rejects_bad_distance() -> None:
    with pytest.raises(ValueError):
        detect_respawn(Coordinate(0, 0), Coordinate(1, 0), detection_distance=0)


def test_recovery_latch_sets_and_clears() -> None:
    state = RespawnRecoveryState()
    assert state.active is False
    state.note_respawn(5)
    assert state.active is True
    assert state.detected_tick == 5
    state.note_respawn(6)  # already active, keeps first detection tick
    assert state.detected_tick == 5
    state.note_recovered()
    assert state.active is False
    assert state.detected_tick is None


def _worker(identifier: str, x: int, y: int, *, cargo: int = 0):
    from arena_hero_agent.domain import EntityId
    from arena_hero_agent.planning import PlanningUnit

    return PlanningUnit(
        id=EntityId(identifier),
        unit_role=UnitRole.WORKER,
        position=Coordinate(x, y),
        health=2,
        cargo=cargo,
    )


def _snapshot(
    *,
    tick: int,
    core_position: Coordinate,
    workers: int,
    resources: int,
    population: int,
) -> PlanningSnapshot:
    from arena_hero_agent.domain import EntityId
    from arena_hero_agent.planning import BeaconInfo, PlanningUnit

    units = tuple(
        PlanningUnit(
            id=EntityId(f"w{i}"),
            unit_role=UnitRole.WORKER,
            position=Coordinate(core_position.x + i, core_position.y),
            health=2,
            cargo=0,
        )
        for i in range(workers)
    )
    return PlanningSnapshot(
        tick=tick,
        rules_version=RULES,
        resources=resources,
        resource_capacity=200,
        resource_space=200 - resources,
        population=population,
        units=units,
        resource_cells={},
        obstacle_cells=frozenset(),
        enemy_cells=frozenset(),
        enemy_units=(),
        core_id="core",
        core_position=core_position,
        core_health=5,
        core_shield=5,
        core_state="normal",
        beacon=BeaconInfo(position=Coordinate(0, 0), status=None),
        threat_map={},
    )


def _core_role(plan: Plan) -> UnitRole | None:
    action = plan.core_action
    return None if action is None else action.unit_role


def test_recovery_forces_worker_after_teleport() -> None:
    decider = ComposedDecider(
        ComposedDeciderConfig(
            respawn_recovery_enabled=True,
            respawn_worker_target=16,
            respawn_detection_distance=32,
        )
    )
    # Establish the pre-respawn position: at the worker target (12) the
    # baseline chooses military.
    pre = _snapshot(
        tick=1,
        core_position=Coordinate(0, 0),
        workers=12,
        resources=100,
        population=12,
    )
    pre_plan = decider.decide_snapshot(pre)
    assert _core_role(pre_plan) in (UnitRole.VANGUARD, UnitRole.RANGER)

    # Teleport the Core far away with 14 workers: recovery must force WORKER,
    # even though the baseline would still choose military past the target.
    post = _snapshot(
        tick=2,
        core_position=Coordinate(100, 100),
        workers=14,
        resources=100,
        population=14,
    )
    post_plan = decider.decide_snapshot(post)
    assert _core_role(post_plan) is UnitRole.WORKER


def test_recovery_exits_once_worker_target_reached() -> None:
    decider = ComposedDecider(
        ComposedDeciderConfig(
            respawn_recovery_enabled=True,
            respawn_worker_target=12,
            respawn_detection_distance=32,
        )
    )
    decider.decide_snapshot(
        _snapshot(
            tick=1,
            core_position=Coordinate(0, 0),
            workers=8,
            resources=100,
            population=8,
        )
    )
    # Teleport + already past the recovery worker target -> baseline military.
    plan = decider.decide_snapshot(
        _snapshot(
            tick=2,
            core_position=Coordinate(100, 100),
            workers=13,
            resources=100,
            population=13,
        )
    )
    assert _core_role(plan) in (UnitRole.VANGUARD, UnitRole.RANGER)


def test_disabled_recovery_leaves_teleport_untouched() -> None:
    decider = ComposedDecider(
        ComposedDeciderConfig(
            respawn_recovery_enabled=False,
            respawn_worker_target=16,
        )
    )
    decider.decide_snapshot(
        _snapshot(
            tick=1,
            core_position=Coordinate(0, 0),
            workers=12,
            resources=100,
            population=12,
        )
    )
    plan = decider.decide_snapshot(
        _snapshot(
            tick=2,
            core_position=Coordinate(100, 100),
            workers=14,
            resources=100,
            population=14,
        )
    )
    # Disabled: recovery does not override the military spawn chosen by baseline.
    assert _core_role(plan) in (UnitRole.VANGUARD, UnitRole.RANGER)


def test_has_local_yield_detects_harvest_near_core() -> None:
    from arena_hero_agent.strategies.respawn_recovery import has_local_yield

    harvested = {
        "10,12": (Coordinate(10, 12), 100),
        "60,0": (Coordinate(60, 0), 90),
    }
    core = Coordinate(0, 0)
    assert has_local_yield(harvested, core, radius=40) is True


def test_has_local_yield_ignores_distant_harvests() -> None:
    from arena_hero_agent.strategies.respawn_recovery import has_local_yield

    harvested = {
        "60,0": (Coordinate(60, 0), 90),
        "-45,-45": (Coordinate(-45, -45), 90),
    }
    core = Coordinate(0, 0)
    assert has_local_yield(harvested, core, radius=40) is False


def test_has_local_yield_empty_ledger_is_not_yield() -> None:
    from arena_hero_agent.strategies.respawn_recovery import has_local_yield

    assert has_local_yield({}, Coordinate(0, 0), radius=40) is False


def test_has_local_yield_rejects_invalid_inputs() -> None:
    from arena_hero_agent.strategies.respawn_recovery import has_local_yield

    with pytest.raises(TypeError):
        has_local_yield({}, cast(Coordinate, "core"), radius=40)
    with pytest.raises(ValueError):
        has_local_yield({}, Coordinate(0, 0), radius=-1)
