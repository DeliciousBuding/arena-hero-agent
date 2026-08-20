"""Differential and boundary tests for deterministic safety/tactical helpers."""

from __future__ import annotations

from functools import cmp_to_key
from typing import Any, cast

import pytest

from arena_hero_agent.domain import Coordinate, Direction, EntityId, UnitRole
from arena_hero_agent.strategies import (
    SafetyPlannerConfig,
    VisibleEnemy,
    aggressive_shot_priority,
    can_shoot,
    core_shelter_target,
    defense_post,
    defensive_shot_priority,
    guard_home_cell,
    home_cell,
    is_core_shelter,
    kite_cell,
    massarmy_stage_targets,
    nearest_enemy,
    next_military,
    next_spawn,
    next_spawn_massarmy,
    occupancy_counts,
    predicted_enemy_cell,
    retreat_direction,
    step_toward,
    threat_weighted_direction,
    tier_of_damage_rank,
    worker_dense_direction,
    yield_anchor,
)
from tests.strategies.fixture_loader import load_oracle_fixture

ROLE = {
    "WORKER": UnitRole.WORKER,
    "VANGUARD": UnitRole.VANGUARD,
    "RANGER": UnitRole.RANGER,
}
DIRECTION_NAME = {
    Direction.NORTH: "UP",
    Direction.EAST: "RIGHT",
    Direction.SOUTH: "DOWN",
    Direction.WEST: "LEFT",
}


def _coordinate(value: list[int]) -> Coordinate:
    return Coordinate(value[0], value[1])


def _obstacles(records: list[list[int]]) -> frozenset[str]:
    return frozenset(f"{x},{y}" for x, y in records)


def _enemy(record: dict[str, Any]) -> VisibleEnemy:
    return VisibleEnemy(
        id=record["id"],
        position=_coordinate(record["position"]),
        kind=record["kind"],
        unit_role=ROLE[record["unitType"]],
    )


def _enemies(records: list[dict[str, Any]]) -> tuple[VisibleEnemy, ...]:
    return tuple(_enemy(record) for record in records)


def test_worker_dense_direction_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["worker_dense_directions"]:
        assert worker_dense_direction(case["index"]) == case["expected"], case["index"]


def test_threat_weighted_direction_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["threat_weighted_directions"]:
        got = threat_weighted_direction(case["index"], case["sector"])
        assert got == case["expected"], case


def test_tier_of_damage_rank_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["tier_of_damage_rank"]:
        assert tier_of_damage_rank(case["rank"]).value == case["expected"], case["rank"]


def test_spawn_choice_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["spawn_choice"]:
        config = SafetyPlannerConfig(
            worker_target=case["workerTarget"],
            vanguard_ratio=case["vanguardRatio"],
        )
        assert (
            next_spawn(
                case["workers"],
                case["vanguards"],
                case["rangers"],
                case["workerTarget"],
                config,
            ).name
            == case["nextSpawn"]
        ), case["name"]
        assert (
            next_military(case["vanguards"], case["rangers"], config).name == case["nextMilitary"]
        ), case["name"]


def test_defense_posts_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["defense_posts"]:
        got = defense_post(
            _coordinate(case["core"]),
            _enemies(case["enemies"]),
            _obstacles(case["obstacles"]),
            ROLE[case["unitType"]],
            case["index"],
        )
        expected = None if case["expected"] is None else _coordinate(case["expected"])
        assert got == expected, case["name"]


def test_home_cells_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["home_cells"]:
        got = home_cell(
            _coordinate(case["core"]),
            _obstacles(case.get("obstacles", [])),
            case.get("index", 0),
        )
        expected = None if case["expected"] is None else _coordinate(case["expected"])
        assert got == expected, case["name"]


def test_guard_home_cells_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["guard_home_cells"]:
        avoid = _obstacles(case["avoid"]) if case.get("avoid") is not None else None
        got = guard_home_cell(
            _coordinate(case["core"]),
            _obstacles(case.get("obstacles", [])),
            case.get("index", 0),
            avoid,
            corner_spacing=case["name"].startswith("corner_spacing"),
        )
        expected = None if case["expected"] is None else _coordinate(case["expected"])
        assert got == expected, case["name"]


def test_yield_anchors_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["yield_anchors"]:
        got = yield_anchor(
            _coordinate(case["core"]),
            _obstacles(case["obstacles"]),
            dict(case.get("occupancy", {})),
            _enemies(case.get("enemies", [])),
        )
        expected = None if case["expected"] is None else _coordinate(case["expected"])
        assert got == expected, case["name"]


def test_occupancy_counts_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    cases = {
        "core_and_units": ([0, 0], [("w1", "WORKER", [0, 0]), ("v1", "VANGUARD", [1, 0])]),
        "stacked_two_units": ([0, 0], [("w1", "WORKER", [0, 0]), ("w2", "WORKER", [0, 0])]),
    }
    from arena_hero_agent.planning import PlanningUnit

    for case in fixture["occupancy_counts"]:
        core, units = cases[case["name"]]
        planned = tuple(
            PlanningUnit(
                id=EntityId(unit_id),
                unit_role=ROLE[role],
                position=_coordinate(position),
                health=2,
                cargo=0,
            )
            for unit_id, role, position in units
        )
        assert occupancy_counts(_coordinate(core), planned) == case["expected"], case["name"]


def test_nearest_enemies_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["nearest_enemies"]:
        got = nearest_enemy(_enemies(case["enemies"]), _coordinate(case["position"]))
        expected = case["expected"]
        if expected is None:
            assert got is None, case["name"]
        else:
            assert got is not None
            assert got.id == expected["id"], case["name"]
            assert [got.position.x, got.position.y] == expected["position"], case["name"]


def test_retreat_directions_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["retreat_directions"]:
        got = retreat_direction(
            _coordinate(case["core"]),
            _enemies(case["enemies"]),
            _obstacles(case["obstacles"]),
            _coordinate(case["beacon"]),
            case.get("scoring", "distance"),
        )
        expected = case["expected"]
        if expected is None:
            assert got is None, case["name"]
        else:
            assert got is not None
            assert DIRECTION_NAME[got] == expected, case["name"]


def test_shot_priorities_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    shot = fixture["shot_priorities"]
    enemies = (
        VisibleEnemy(id="w1", position=Coordinate(1, 0), kind="UNIT", unit_role=UnitRole.WORKER),
        VisibleEnemy(id="r1", position=Coordinate(2, 0), kind="UNIT", unit_role=UnitRole.RANGER),
        VisibleEnemy(id="v1", position=Coordinate(3, 0), kind="UNIT", unit_role=UnitRole.VANGUARD),
        VisibleEnemy(id="c1", position=Coordinate(4, 0), kind="CORE", unit_role=None),
    )
    aggressive = sorted(enemies, key=cmp_to_key(aggressive_shot_priority))
    defensive = sorted(
        enemies,
        key=cmp_to_key(lambda left, right: defensive_shot_priority(Coordinate(0, 0), left, right)),
    )
    assert [enemy.id for enemy in aggressive] == shot["aggressive"]
    assert [enemy.id for enemy in defensive] == shot["defensive"]


def test_can_shoot_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["can_shoot"]:
        got = can_shoot(
            _coordinate(case["from"]),
            _coordinate(case["target"]),
            _obstacles(case["obstacles"]),
        )
        assert got == case["expected"], case["name"]


def test_predicted_enemy_cells_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["predicted_enemy_cells"]:
        got = predicted_enemy_cell(_coordinate(case["actor"]), _coordinate(case["enemy"]))
        expected = None if case["expected"] is None else _coordinate(case["expected"])
        assert got == expected, case["name"]


def test_kite_cells_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["kite_cells"]:
        got = kite_cell(
            _coordinate(case["from"]),
            _coordinate(case["threat"]),
            _obstacles(case["obstacles"]),
            dict(case.get("occupancy", {})),
            _enemies(case["enemies"]),
        )
        expected = None if case["expected"] is None else _coordinate(case["expected"])
        assert got == expected, case["name"]


def test_core_shelters_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["core_shelters"]:
        got = core_shelter_target(
            _coordinate(case["core"]),
            _obstacles(case["obstacles"]),
            _obstacles(case["resourceCells"]),
        )
        expected = None
        if case["expected"] is not None:
            expected = (
                _coordinate(case["expected"]["target"]),
                _coordinate(case["expected"]["entrance"]),
            )
        assert got == expected, case["name"]


def test_is_core_shelter_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["is_core_shelter"]:
        got = is_core_shelter(_coordinate(case["core"]), _obstacles(case["obstacles"]))
        expected = None if case["expected"] is None else _coordinate(case["expected"])
        assert got == expected, case["name"]


def test_step_toward_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["step_toward"]:
        got = step_toward(_coordinate(case["from"]), _coordinate(case["target"]))
        assert DIRECTION_NAME[got] == case["expected"], case


def test_step_toward_prefers_x_then_y() -> None:
    assert step_toward(Coordinate(0, 0), Coordinate(2, 2)) is Direction.EAST
    assert step_toward(Coordinate(2, 2), Coordinate(0, 0)) is Direction.WEST
    assert step_toward(Coordinate(0, 0), Coordinate(0, 0)) is Direction.NORTH


def test_helpers_reject_invalid_inputs() -> None:
    with pytest.raises(TypeError):
        next_spawn(1, 1, 1, 8, cast(SafetyPlannerConfig, "config"))
    with pytest.raises(ValueError):
        next_spawn(-1, 1, 1, 8, SafetyPlannerConfig())
    with pytest.raises(ValueError):
        defense_post(
            Coordinate(0, 0),
            (),
            frozenset(),
            UnitRole.WORKER,
            0,
        )
    with pytest.raises(ValueError):
        worker_dense_direction(-1)
    with pytest.raises(ValueError):
        retreat_direction(
            Coordinate(0, 0),
            (),
            frozenset(),
            Coordinate(5, 5),
            scoring="unknown",
        )
    with pytest.raises(ValueError):
        core_shelter_target(Coordinate(0, 0), frozenset(), frozenset(), search_radius=0)


def test_helpers_are_deterministic() -> None:
    obstacles = _obstacles([[0, -1]])
    enemies = _enemies([{"id": "e1", "kind": "UNIT", "position": [0, -5], "unitType": "VANGUARD"}])
    assert defense_post(Coordinate(0, 0), enemies, obstacles, UnitRole.VANGUARD, 0) == (
        defense_post(Coordinate(0, 0), enemies, obstacles, UnitRole.VANGUARD, 0)
    )
    assert home_cell(Coordinate(0, 0), obstacles) == home_cell(Coordinate(0, 0), obstacles)
    assert can_shoot(Coordinate(0, 0), Coordinate(1, 0), frozenset()) == can_shoot(
        Coordinate(0, 0), Coordinate(1, 0), frozenset()
    )


def test_massarmy_stage_targets_follow_four_stage_table() -> None:
    assert massarmy_stage_targets(0) == (8, 1, 1)
    assert massarmy_stage_targets(10) == (8, 1, 1)
    assert massarmy_stage_targets(11) == (12, 3, 4)
    assert massarmy_stage_targets(20) == (12, 3, 4)
    assert massarmy_stage_targets(21) == (18, 6, 8)
    assert massarmy_stage_targets(32) == (18, 6, 8)
    assert massarmy_stage_targets(33) == (18, 14, 16)
    assert massarmy_stage_targets(500) == (18, 14, 16)


def test_next_spawn_massarmy_fills_workers_then_military() -> None:
    # Stage 1 (pop <= 10): 8 workers, 1 vanguard, 1 ranger.
    assert next_spawn_massarmy(0, 0, 0, 1) is UnitRole.WORKER
    assert next_spawn_massarmy(7, 0, 0, 9) is UnitRole.WORKER
    assert next_spawn_massarmy(8, 0, 0, 9) is UnitRole.VANGUARD
    assert next_spawn_massarmy(8, 1, 0, 10) is UnitRole.RANGER
    # A completed stage overflows into Workers.
    assert next_spawn_massarmy(8, 1, 1, 10) is UnitRole.WORKER
    # Stage 2 (pop <= 20): 12 workers, 3 vanguards, 4 rangers.
    assert next_spawn_massarmy(12, 0, 0, 12) is UnitRole.VANGUARD
    assert next_spawn_massarmy(12, 3, 3, 18) is UnitRole.RANGER


def test_massarmy_helpers_reject_invalid_inputs() -> None:
    with pytest.raises(TypeError):
        massarmy_stage_targets(cast(int, "ten"))
    with pytest.raises(ValueError):
        massarmy_stage_targets(-1)
    with pytest.raises(ValueError):
        next_spawn_massarmy(-1, 0, 0, 0)
    with pytest.raises(TypeError):
        next_spawn_massarmy(1, 1, 1, cast(int, None))
