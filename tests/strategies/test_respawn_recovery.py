"""Respawn recovery tests: teleport detection, latch, and composed decider wiring."""

from __future__ import annotations

import pytest

from arena_hero_agent.domain import CURRENT_RULES_VERSION, Coordinate, UnitRole
from arena_hero_agent.planning import Plan, PlanningSnapshot
from arena_hero_agent.strategies import ComposedDecider, ComposedDeciderConfig
from arena_hero_agent.strategies.respawn_recovery import (
    RespawnRecoveryState,
    detect_respawn,
)

RULES = CURRENT_RULES_VERSION


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
    # Establish the pre-respawn position: 8 workers -> baseline chooses military.
    pre = _snapshot(
        tick=1,
        core_position=Coordinate(0, 0),
        workers=8,
        resources=100,
        population=8,
    )
    pre_plan = decider.decide_snapshot(pre)
    assert _core_role(pre_plan) in (UnitRole.VANGUARD, UnitRole.RANGER)

    # Teleport the Core far away with 10 workers: recovery must force WORKER,
    # even though the baseline would still choose military at 10 workers.
    post = _snapshot(
        tick=2,
        core_position=Coordinate(100, 100),
        workers=10,
        resources=100,
        population=10,
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
            workers=8,
            resources=100,
            population=8,
        )
    )
    plan = decider.decide_snapshot(
        _snapshot(
            tick=2,
            core_position=Coordinate(100, 100),
            workers=10,
            resources=100,
            population=10,
        )
    )
    # Disabled: recovery does not override the military spawn chosen by baseline.
    assert _core_role(plan) in (UnitRole.VANGUARD, UnitRole.RANGER)
