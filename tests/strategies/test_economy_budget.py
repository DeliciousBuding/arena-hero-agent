"""Economy-budget layer tests: pure deposit/heal, dispersal, expansion, and routing."""

from __future__ import annotations

from arena_hero_agent.domain import Coordinate, UnitRole
from arena_hero_agent.strategies.economy_budget import (
    FullCapacityRoute,
    HomeRoute,
    can_afford_heal,
    full_capacity_worker_route,
    heal_reserve,
    ignore_occupancy_home_step,
    projected_core_resources,
    spawn_clear_until,
    spawn_suspended,
    unit_max_health,
    worker_expansion_threshold,
)


def test_unit_max_health() -> None:
    assert unit_max_health(UnitRole.VANGUARD) == 4
    assert unit_max_health(UnitRole.WORKER) == 2
    assert unit_max_health(UnitRole.RANGER) == 2


def test_heal_reserve() -> None:
    assert heal_reserve([UnitRole.WORKER, UnitRole.VANGUARD, UnitRole.RANGER]) == 5
    assert heal_reserve([]) == 0


def test_projected_core_resources_adds_deposit() -> None:
    assert (
        projected_core_resources(
            resources=10,
            resource_space=20,
            deposit_cargo=8,
            healing_reserve=5,
        )
        == 13
    )


def test_projected_core_resources_caps_deposit_at_space() -> None:
    assert (
        projected_core_resources(
            resources=10,
            resource_space=2,
            deposit_cargo=8,
            healing_reserve=5,
        )
        == 7
    )


def test_projected_core_resources_floors_at_zero() -> None:
    assert (
        projected_core_resources(
            resources=2,
            resource_space=10,
            deposit_cargo=0,
            healing_reserve=5,
        )
        == 0
    )


def test_can_afford_heal() -> None:
    assert can_afford_heal(resources=15, missing_health=5) is True
    assert can_afford_heal(resources=14, missing_health=5) is False
    assert can_afford_heal(resources=5, missing_health=5, reserve=0) is True


def test_spawn_clear_until_advances() -> None:
    assert spawn_clear_until(tick=10, previous_clear_until=0, cell_unit_limit=True) == 13


def test_spawn_clear_until_keeps_later_deadline() -> None:
    assert spawn_clear_until(tick=10, previous_clear_until=20, cell_unit_limit=True) == 20


def test_spawn_clear_until_unchanged_without_limit() -> None:
    assert spawn_clear_until(tick=10, previous_clear_until=0, cell_unit_limit=False) == 0


def test_spawn_suspended() -> None:
    assert spawn_suspended(tick=12, clear_until=13) is True
    assert spawn_suspended(tick=13, clear_until=13) is False


def test_full_capacity_vacates_core_cell() -> None:
    assert full_capacity_worker_route(
        core=Coordinate(0, 0),
        position=Coordinate(0, 0),
        worker_index=0,
    ) == FullCapacityRoute(next_step=Coordinate(0, -1), staging_goal=Coordinate(3, 0))


def test_full_capacity_vacates_core_skips_full_neighbor() -> None:
    assert full_capacity_worker_route(
        core=Coordinate(0, 0),
        position=Coordinate(0, 0),
        worker_index=0,
        occupancy={Coordinate(0, -1): 2},
    ) == FullCapacityRoute(next_step=Coordinate(1, 0), staging_goal=Coordinate(3, 0))


def test_full_capacity_disperses_toward_staging() -> None:
    assert full_capacity_worker_route(
        core=Coordinate(0, 0),
        position=Coordinate(5, 0),
        worker_index=0,
    ) == FullCapacityRoute(next_step=Coordinate(4, 0), staging_goal=Coordinate(3, 0))


def test_full_capacity_routes_around_full_cell() -> None:
    assert full_capacity_worker_route(
        core=Coordinate(0, 0),
        position=Coordinate(5, 0),
        worker_index=0,
        occupancy={Coordinate(4, 0): 2},
    ) == FullCapacityRoute(next_step=Coordinate(5, 1), staging_goal=Coordinate(3, 0))


def test_worker_expansion_threshold_early() -> None:
    assert (
        worker_expansion_threshold(
            worker_count=4,
            worker_target=12,
            resource_capacity=100,
            population=4,
        )
        == 15
    )


def test_worker_expansion_threshold_early_clamped_to_capacity() -> None:
    assert (
        worker_expansion_threshold(
            worker_count=0,
            worker_target=12,
            resource_capacity=12,
            population=0,
        )
        == 12
    )


def test_worker_expansion_threshold_late_stage() -> None:
    assert (
        worker_expansion_threshold(
            worker_count=6,
            worker_target=12,
            resource_capacity=100,
            population=6,
        )
        == 25
    )


def test_worker_expansion_threshold_prices_dynamic_population() -> None:
    assert (
        worker_expansion_threshold(
            worker_count=6,
            worker_target=12,
            resource_capacity=100,
            population=20,
        )
        == 29
    )


def test_worker_expansion_threshold_final_worker_stage() -> None:
    assert (
        worker_expansion_threshold(
            worker_count=11,
            worker_target=12,
            resource_capacity=100,
            population=11,
        )
        == 20
    )


def test_ignore_occupancy_home_step() -> None:
    assert ignore_occupancy_home_step(
        position=Coordinate(5, 0),
        core=Coordinate(0, 0),
    ) == HomeRoute(next_step=Coordinate(4, 0), target=Coordinate(1, 0))


def test_ignore_occupancy_home_step_at_target() -> None:
    assert ignore_occupancy_home_step(
        position=Coordinate(1, 0),
        core=Coordinate(0, 0),
    ) == HomeRoute(next_step=None, target=Coordinate(1, 0))


def test_ignore_occupancy_home_step_skips_blocked_adjacent() -> None:
    assert ignore_occupancy_home_step(
        position=Coordinate(-5, 0),
        core=Coordinate(0, 0),
        terrain_blocked=frozenset({Coordinate(1, 0)}),
    ) == HomeRoute(next_step=Coordinate(-4, 0), target=Coordinate(-1, 0))
