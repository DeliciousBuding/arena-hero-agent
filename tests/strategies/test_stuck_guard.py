"""Stuck-guard layer tests: pure detection plus composition gating.

The stuck guard blocks a worker's current resource target once its recent
movement looks frozen or confined, forcing reassignment. It is enabled by
default; isolation tests disable the other research layers explicitly.
"""

from __future__ import annotations

from dataclasses import replace
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
    Assignment,
    BeaconInfo,
    Plan,
    PlanningSnapshot,
    PlanningUnit,
    ResourceCellInfo,
    Task,
    TaskType,
    UnitActionType,
    assign_worker_tasks,
)
from arena_hero_agent.strategies import (
    ComposedDecider,
    ComposedDeciderConfig,
    detect_stuck_unit_ids,
)
from arena_hero_agent.strategies.stuck_guard import positions_stuck

RULES = CURRENT_RULES_VERSION


def _worker(identifier: str, x: int, y: int) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(identifier),
        unit_role=UnitRole.WORKER,
        position=Coordinate(x, y),
        health=2,
        cargo=0,
    )


def _cell(x: int, y: int) -> ResourceCellInfo:
    return ResourceCellInfo(position=Coordinate(x, y), visible=True, last_seen_tick=1)


def _snapshot(
    tick: int,
    *,
    worker_position: Coordinate,
    resource_cells: dict[str, ResourceCellInfo],
    core_position: Coordinate | None = None,
) -> PlanningSnapshot:
    return PlanningSnapshot(
        tick=tick,
        rules_version=RULES,
        resources=0,
        resource_capacity=100,
        resource_space=100,
        population=1,
        units=(_worker("w1", worker_position.x, worker_position.y),),
        resource_cells=resource_cells,
        obstacle_cells=frozenset(),
        enemy_cells=frozenset(),
        enemy_units=(),
        core_id="core" if core_position is not None else None,
        core_position=core_position,
        core_health=5 if core_position is not None else None,
        core_shield=5 if core_position is not None else None,
        core_state="normal" if core_position is not None else None,
        beacon=BeaconInfo(position=Coordinate(0, 0), status=None),
        threat_map={},
    )


def _action(plan: Plan, unit_id: str) -> UnitActionType | None:
    action = plan.action_for(unit_id)
    return None if action is None else action.type


def _direction(plan: Plan, unit_id: str) -> Direction | None:
    action = plan.action_for(unit_id)
    return None if action is None else action.direction


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


def test_detect_stuck_unchanged_positions() -> None:
    assert detect_stuck_unit_ids(
        {"w1": [Coordinate(0, 0), Coordinate(0, 0), Coordinate(0, 0)]},
        n_ticks=3,
        k_cells=1,
    ) == frozenset({"w1"})


def test_detect_stuck_confined_positions() -> None:
    assert detect_stuck_unit_ids(
        {"w1": [Coordinate(0, 0), Coordinate(0, 1), Coordinate(0, 0)]},
        n_ticks=3,
        k_cells=1,
    ) == frozenset({"w1"})


def test_detect_stuck_requires_n_ticks() -> None:
    assert (
        detect_stuck_unit_ids(
            {"w1": [Coordinate(0, 0), Coordinate(0, 1)]},
            n_ticks=3,
            k_cells=1,
        )
        == frozenset()
    )


def test_detect_stuck_ignores_wide_movement() -> None:
    assert (
        detect_stuck_unit_ids(
            {"w1": [Coordinate(0, 0), Coordinate(0, 5), Coordinate(0, 0)]},
            n_ticks=3,
            k_cells=1,
        )
        == frozenset()
    )


def test_positions_stuck_handles_empty() -> None:
    assert positions_stuck([], 1) is False


def test_blocked_cell_forces_reassignment() -> None:
    snapshot = _snapshot(
        1,
        worker_position=Coordinate(0, 0),
        resource_cells={"1,0": _cell(1, 0)},
        core_position=Coordinate(0, 0),
    )
    unblocked = assign_worker_tasks(snapshot)
    assert unblocked.plan.assignments == (
        Assignment(
            unit_id="w1",
            task=Task(type=TaskType.GO_RESOURCE, target=Coordinate(1, 0), target_cell_key="1,0"),
        ),
    )
    blocked = assign_worker_tasks(snapshot, blocked_cells=frozenset({"1,0"}))
    assert blocked.plan.assignments == (Assignment(unit_id="w1", task=Task(type=TaskType.WAIT)),)


def test_stuck_guard_disabled_keeps_default_behavior() -> None:
    resource_cells = {"2,0": _cell(2, 0)}
    positions = [Coordinate(0, 0), Coordinate(0, 1), Coordinate(0, 0)]
    default = ComposedDecider()
    explicit = ComposedDecider(ComposedDeciderConfig())
    for tick, position in enumerate(positions, start=1):
        snapshot = _snapshot(
            tick,
            worker_position=position,
            resource_cells=resource_cells,
            core_position=Coordinate(0, 0),
        )
        plan_default = default.decide_snapshot(snapshot)
        plan_explicit = explicit.decide_snapshot(snapshot)
        assert plan_default == plan_explicit
        assert _action(plan_default, "w1") is UnitActionType.MOVE
        direction = _direction(plan_default, "w1")
        assert direction is not None
        assert direction.value == "east"


def test_stuck_guard_enabled_reassigns_spinning_unit() -> None:
    resource_cells = {"2,0": _cell(2, 0)}
    positions = [Coordinate(0, 0), Coordinate(0, 1), Coordinate(0, 0)]
    decider = ComposedDecider(
        replace(
            _all_off(),
            stuck_guard_enabled=True,
            stuck_guard_ticks=3,
            stuck_guard_radius=1,
        )
    )
    plans = [
        decider.decide_snapshot(
            _snapshot(
                tick,
                worker_position=position,
                resource_cells=resource_cells,
                core_position=Coordinate(0, 0),
            )
        )
        for tick, position in enumerate(positions, start=1)
    ]
    assert _action(plans[0], "w1") is UnitActionType.MOVE
    assert _action(plans[1], "w1") is UnitActionType.MOVE
    assert _action(plans[2], "w1") is UnitActionType.WAIT


def test_stuck_guard_config_validates() -> None:
    with pytest.raises(TypeError, match="stuck_guard_enabled"):
        ComposedDeciderConfig(stuck_guard_enabled=cast(bool, "yes"))
    with pytest.raises(TypeError, match="stuck_guard_ticks"):
        ComposedDeciderConfig(stuck_guard_ticks=cast(int, "3"))
    with pytest.raises(ValueError, match="stuck_guard_ticks"):
        ComposedDeciderConfig(stuck_guard_ticks=0)
    with pytest.raises(ValueError, match="stuck_guard_radius"):
        ComposedDeciderConfig(stuck_guard_radius=0)
