from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arena_hero_agent.domain import (
    TS_COMPATIBLE_SEARCH_LIMITS,
    Bounds,
    Coordinate,
    Direction,
    NavigationGrid,
    SearchLimitExceeded,
    SearchLimits,
    UnknownTraversalPolicy,
    UnreachableError,
    chebyshev,
    explore_radius_for_ring,
    first_step,
    is_reachable,
    manhattan,
    reachable_cells,
    shortest_path,
    shot_line_blocked,
    vision_line_blocked,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ts_world_nav_known_answers.json"
INT32_COORDINATES = st.builds(
    Coordinate,
    st.integers(min_value=-10_000, max_value=10_000),
    st.integers(min_value=-10_000, max_value=10_000),
)


def _coordinate(value: list[int]) -> Coordinate:
    return Coordinate(value[0], value[1])


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_ts_known_answers_are_pinned_with_source_metadata() -> None:
    fixture = _load_fixture()
    metadata = fixture["metadata"]
    assert metadata["oracle_commit"] == "8cf5cbbcccf396a8feee94404af44969c5388e15"
    assert "packages/arena-agent/src/domain/nav.ts" in metadata["source_files"]
    assert "packages/arena-agent/src/domain/world.ts" in metadata["source_files"]

    move_case = fixture["moves"]
    start = _coordinate(move_case["start"])
    for direction_name, expected in move_case["expected"].items():
        assert start.step(Direction(direction_name)) == _coordinate(expected)

    distance_case = fixture["distances"]["chebyshev"]
    assert (
        chebyshev(
            _coordinate(distance_case["start"]),
            _coordinate(distance_case["target"]),
        )
        == distance_case["expected"]
    )

    for case in fixture["first_steps"]:
        blocked = frozenset(_coordinate(cell) for cell in case.get("blocked", []))
        grid = NavigationGrid(blocked_cells=blocked)
        assert first_step(
            grid,
            _coordinate(case["start"]),
            _coordinate(case["target"]),
            unknown_policy=UnknownTraversalPolicy.ALLOW,
            limits=TS_COMPATIBLE_SEARCH_LIMITS,
        ) is Direction(case["expected"])

    for case in fixture["unreachable"]:
        blocked = frozenset(_coordinate(cell) for cell in case["blocked"])
        with pytest.raises(UnreachableError):
            shortest_path(
                NavigationGrid(blocked_cells=blocked),
                _coordinate(case["start"]),
                _coordinate(case["target"]),
                unknown_policy=UnknownTraversalPolicy.ALLOW,
                limits=TS_COMPATIBLE_SEARCH_LIMITS,
            )

    vision = fixture["vision"]
    origin = _coordinate(vision["origin"])
    target = _coordinate(vision["target"])
    for blocker in vision["corner_blockers"]:
        assert vision_line_blocked(origin, target, frozenset({_coordinate(blocker)}))
    target_obstacle = _coordinate(vision["target_obstacle_does_not_block"])
    assert not vision_line_blocked(origin, target, frozenset({target_obstacle}))

    # Shot lines differ from vision: obstacles beside a diagonal do NOT block
    # a Ranger shot (official combat rules), while an obstacle on the line
    # itself still does.
    shot_origin = _coordinate(vision["origin"])
    shot_target = _coordinate(vision["target"])
    for blocker in vision["corner_blockers"]:
        assert not shot_line_blocked(
            shot_origin, shot_target, frozenset({_coordinate(blocker)})
        )
    line_blocker = vision["line_blocker"]
    assert shot_line_blocked(
        shot_origin, shot_target, frozenset({_coordinate(line_blocker)})
    )

    rings = fixture["explore_rings"]
    assert [explore_radius_for_ring(rings["base"], index) for index in rings["indices"]] == rings[
        "expected"
    ]


@given(INT32_COORDINATES, INT32_COORDINATES)
def test_distances_are_symmetric(first: Coordinate, second: Coordinate) -> None:
    assert manhattan(first, second) == manhattan(second, first)
    assert chebyshev(first, second) == chebyshev(second, first)


@given(INT32_COORDINATES, INT32_COORDINATES, INT32_COORDINATES)
def test_distances_obey_triangle_inequality(
    first: Coordinate,
    middle: Coordinate,
    last: Coordinate,
) -> None:
    assert manhattan(first, last) <= manhattan(first, middle) + manhattan(middle, last)
    assert chebyshev(first, last) <= chebyshev(first, middle) + chebyshev(middle, last)


@given(
    st.integers(min_value=-15, max_value=15),
    st.integers(min_value=-15, max_value=15),
)
def test_open_unknown_path_is_shortest(dx: int, dy: int) -> None:
    start = Coordinate(0, 0)
    target = Coordinate(dx, dy)
    if target == start:
        assert shortest_path(
            NavigationGrid(),
            start,
            target,
            unknown_policy=UnknownTraversalPolicy.ALLOW,
            limits=TS_COMPATIBLE_SEARCH_LIMITS,
        ) == (start,)
        return
    path = shortest_path(
        NavigationGrid(),
        start,
        target,
        unknown_policy=UnknownTraversalPolicy.ALLOW,
        limits=TS_COMPATIBLE_SEARCH_LIMITS,
    )
    assert len(path) - 1 == manhattan(start, target)
    assert all(manhattan(left, right) == 1 for left, right in zip(path, path[1:], strict=False))


def test_obstacle_insertion_order_does_not_change_path() -> None:
    obstacles = [Coordinate(1, 0), Coordinate(2, 0), Coordinate(3, 0)]
    forward = NavigationGrid(blocked_cells=frozenset(obstacles))
    reverse = NavigationGrid(blocked_cells=frozenset(reversed(obstacles)))
    start = Coordinate(0, 0)
    target = Coordinate(4, 0)
    first = shortest_path(
        forward,
        start,
        target,
        unknown_policy=UnknownTraversalPolicy.ALLOW,
        limits=TS_COMPATIBLE_SEARCH_LIMITS,
    )
    second = shortest_path(
        reverse,
        start,
        target,
        unknown_policy=UnknownTraversalPolicy.ALLOW,
        limits=TS_COMPATIBLE_SEARCH_LIMITS,
    )
    assert first == second
    assert not set(first) & set(obstacles)


def test_unknown_policy_is_explicit_and_changes_reachability() -> None:
    start = Coordinate(0, 0)
    target = Coordinate(1, 0)
    grid = NavigationGrid(open_cells=frozenset({start}))

    assert not is_reachable(
        grid,
        start,
        target,
        unknown_policy=UnknownTraversalPolicy.BLOCK,
    )
    assert is_reachable(
        grid,
        start,
        target,
        unknown_policy=UnknownTraversalPolicy.ALLOW,
        limits=SearchLimits(node_budget=16, search_radius=2),
    )


def test_bounded_reachability_respects_obstacles_and_bounds() -> None:
    bounds = Bounds(-1, 1, -1, 1)
    blocked = Coordinate(1, 1)
    grid = NavigationGrid(blocked_cells=frozenset({blocked}), bounds=bounds)
    cells = reachable_cells(
        grid,
        Coordinate(0, 0),
        unknown_policy=UnknownTraversalPolicy.ALLOW,
    )

    assert len(cells) == 8
    assert blocked not in cells
    assert all(bounds.contains(cell) for cell in cells)


def test_unbounded_unknown_search_requires_limits_and_terminates_when_limited() -> None:
    start = Coordinate(0, 0)
    target = Coordinate(3, 0)
    ring = frozenset(
        {
            Coordinate(2, 0),
            Coordinate(3, -1),
            Coordinate(4, 0),
            Coordinate(3, 1),
        }
    )
    grid = NavigationGrid(blocked_cells=ring)

    with pytest.raises(ValueError, match="explicit SearchLimits"):
        shortest_path(
            grid,
            start,
            target,
            unknown_policy=UnknownTraversalPolicy.ALLOW,
        )

    started = perf_counter()
    with pytest.raises(SearchLimitExceeded, match="before reachability was known"):
        shortest_path(
            grid,
            start,
            target,
            unknown_policy=UnknownTraversalPolicy.ALLOW,
            limits=SearchLimits(node_budget=256, search_radius=8),
        )
    assert perf_counter() - started < 1.0


def test_budget_exhaustion_is_not_reported_as_proven_unreachable() -> None:
    with pytest.raises(SearchLimitExceeded, match="node budget"):
        shortest_path(
            NavigationGrid(),
            Coordinate(0, 0),
            Coordinate(5, 0),
            unknown_policy=UnknownTraversalPolicy.ALLOW,
            limits=SearchLimits(node_budget=1, search_radius=8),
        )


def test_finite_bounds_can_prove_enclosed_unknown_target_unreachable() -> None:
    target = Coordinate(1, 1)
    ring = frozenset(
        {
            Coordinate(0, 1),
            Coordinate(1, 0),
            Coordinate(2, 1),
            Coordinate(1, 2),
        }
    )
    grid = NavigationGrid(blocked_cells=ring, bounds=Bounds(-2, 3, -2, 3))
    with pytest.raises(UnreachableError):
        shortest_path(
            grid,
            Coordinate(-1, -1),
            target,
            unknown_policy=UnknownTraversalPolicy.ALLOW,
        )


def test_invalid_limits_and_unbounded_reachable_cells_fail_fast() -> None:
    with pytest.raises(ValueError):
        SearchLimits(node_budget=0, search_radius=1)
    with pytest.raises(ValueError):
        SearchLimits(node_budget=1, search_radius=0)
    with pytest.raises(ValueError, match="unbounded unknown traversal"):
        reachable_cells(
            NavigationGrid(),
            Coordinate(0, 0),
            unknown_policy=UnknownTraversalPolicy.ALLOW,
        )
