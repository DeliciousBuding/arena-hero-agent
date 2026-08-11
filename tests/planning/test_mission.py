"""Differential and boundary tests for mission value functions."""

from __future__ import annotations

from typing import Any, cast

import pytest

from arena_hero_agent.domain import Coordinate, EntityId, UnitRole
from arena_hero_agent.planning import (
    MissionConfig,
    PlanningUnit,
    is_collectable,
    refill_bonus_of,
    surveyor_ids,
    target_confidence,
)
from tests.strategies.fixture_loader import load_oracle_fixture


def _coordinate(value: list[int]) -> Coordinate:
    return Coordinate(value[0], value[1])


def _worker(record: dict[str, Any]) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(record["id"]),
        unit_role=UnitRole.WORKER,
        position=_coordinate(record["position"]),
        health=record.get("hp", 2),
        cargo=record.get("cargo", 0),
    )


def _confidence_cell(record: dict[str, Any]) -> dict[str, object]:
    cell: dict[str, object] = {}
    if "visible" in record:
        cell["visible"] = record["visible"]
    if "seeded" in record:
        cell["seeded"] = record["seeded"]
    if "lastSeenTick" in record:
        cell["last_seen_tick"] = record["lastSeenTick"]
    return cell


def test_target_confidence_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    mission = fixture["mission_value"]
    for case in mission["target_confidence"]:
        config = MissionConfig(
            visible_bonus=case["config"]["visibleBonus"],
            seed_age_decay=case["config"]["seedAgeDecay"],
        )
        got = target_confidence(_confidence_cell(case["cell"]), case["tick"], config)
        assert got == case["expected"], case["name"]


def test_is_collectable_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    mission = fixture["mission_value"]
    for case in mission["is_collectable"]:
        config = MissionConfig(
            collection_value_floor=case["config"]["collectionValueFloor"],
            max_collection_distance=case["config"]["maxCollectionDistance"],
            dead_mine_overdue_ticks=case["config"]["deadMineOverdueTicks"],
        )
        refill = None if "refill" not in case else dict(case["refill"])
        got = is_collectable(
            case["score"],
            _worker(case["worker"]),
            case["cell"][0],
            case["cell"][1],
            config,
            refill,
            visible=case.get("visible", False),
        )
        assert got == case["expected"], case["name"]


def test_refill_bonus_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    mission = fixture["mission_value"]
    for case in mission["refill_bonus"]:
        config = MissionConfig(
            refill_lookahead=case["config"]["refillLookahead"],
            dead_mine_overdue_ticks=case["config"]["deadMineOverdueTicks"],
            refill_bonus=case["config"]["refillBonus"],
        )
        got = refill_bonus_of(case["key"], dict(case["refill"]), config)
        assert got == case["expected"], case["name"]


def test_surveyor_ids_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    mission = fixture["mission_value"]
    for case in mission["surveyor_ids"]:
        config = MissionConfig(
            survey_worker_cap=case["config"]["surveyWorkerCap"],
            survey_worker_floor=case["config"]["surveyWorkerFloor"],
        )
        units = tuple(_worker(unit) for unit in case["unassigned"])
        got = sorted(surveyor_ids(units, config, survey_burst_active=case.get("burst", False)))
        assert got == sorted(case["expected"]), case["name"]


def test_is_collectable_boundaries() -> None:
    worker = _worker({"id": "w1", "unitType": "WORKER", "position": [0, 0], "hp": 2, "cargo": 0})
    config = MissionConfig(
        collection_value_floor=4,
        max_collection_distance=10,
        dead_mine_overdue_ticks=5,
    )
    assert is_collectable(4.0, worker, 3, 4, config)
    assert not is_collectable(3.99, worker, 3, 4, config)
    # exact max distance boundary
    assert is_collectable(5.0, worker, 6, 4, config)
    assert not is_collectable(5.0, worker, 6, 5, config)
    # visible cells skip the floor
    assert is_collectable(0.0, worker, 1, 0, config, visible=True)


def test_is_collectable_rejects_invalid_inputs() -> None:
    worker = _worker({"id": "w1", "unitType": "WORKER", "position": [0, 0], "hp": 2, "cargo": 0})
    with pytest.raises(TypeError):
        is_collectable(5.0, worker, 1, 0, cast(MissionConfig, "config"))
    with pytest.raises(TypeError):
        is_collectable(5.0, cast(PlanningUnit, "worker"), 1, 0)


def test_refill_bonus_rejects_bad_config() -> None:
    with pytest.raises(TypeError):
        refill_bonus_of("3,4", {}, cast(MissionConfig, "config"))


def test_surveyor_ids_rejects_invalid_config() -> None:
    with pytest.raises(TypeError):
        surveyor_ids((), cast(MissionConfig, "config"))
