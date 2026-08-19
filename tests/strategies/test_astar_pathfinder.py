"""Regression tests for the A* pathfinder.

These pin the first-step routing contract that worker travel, exploration, and
barren migration all depend on. The pathfinder previously returned ``None`` for
trivially reachable targets because the heap entry stored the tie-break sequence
counter where the staleness check expected the path cost, so nearly every node
was discarded. Any regression to that behavior makes all routing fall back to
terrain-blind greedy stepping and must fail loudly here.
"""

from __future__ import annotations

from arena_hero_agent.domain import Coordinate, Direction
from arena_hero_agent.strategies.astar_pathfinder import astar_next_step


def test_straight_route_with_no_obstacles() -> None:
    assert astar_next_step(Coordinate(5, 0), Coordinate(0, 0), frozenset()) is Direction.WEST
    assert astar_next_step(Coordinate(0, 0), Coordinate(0, 4), frozenset()) is Direction.SOUTH


def test_routes_around_single_blocking_obstacle() -> None:
    # The direct west step is blocked; the route must detour, not give up.
    step = astar_next_step(Coordinate(5, 0), Coordinate(0, 0), frozenset({"4,0"}))
    assert step in (Direction.NORTH, Direction.SOUTH)


def test_routes_around_when_first_detour_also_blocked() -> None:
    step = astar_next_step(Coordinate(5, 0), Coordinate(0, 0), frozenset({"4,0", "5,1"}))
    assert step is Direction.NORTH


def test_detours_around_a_wall() -> None:
    # The wall spans x=2 at y=-1..1, so the route must go around it. EAST (to
    # [1,0], still open) and SOUTH/NORTH are all on optimal length-8 paths; the
    # regression contract is that a route is found rather than returning None.
    step = astar_next_step(
        Coordinate(0, 0), Coordinate(4, 0), frozenset({"2,0", "2,1", "2,-1"})
    )
    assert step in (Direction.EAST, Direction.SOUTH, Direction.NORTH)


def test_returns_none_when_start_is_enclosed() -> None:
    enclosed = frozenset({"1,0", "0,1", "-1,0", "0,-1"})
    assert astar_next_step(Coordinate(0, 0), Coordinate(5, 0), enclosed) is None


def test_returns_none_when_start_equals_target() -> None:
    assert astar_next_step(Coordinate(3, 3), Coordinate(3, 3), frozenset()) is None


def test_goal_on_obstacle_falls_back_to_passable_neighbor() -> None:
    # Target cell is blocked; accept a step toward any passable neighbor of it.
    step = astar_next_step(Coordinate(0, 0), Coordinate(2, 0), frozenset({"2,0"}))
    assert step is not None
