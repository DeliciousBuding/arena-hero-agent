"""Differential and boundary tests for tactical squad formation and rally points."""

from __future__ import annotations

from typing import Any, cast

import pytest

from arena_hero_agent.domain import Coordinate, EntityId, UnitRole
from arena_hero_agent.planning import PlanningUnit
from arena_hero_agent.strategies import (
    EMPTY_SQUAD_MEMBERSHIP,
    SquadMembership,
    TacticalSquad,
    rally_member_slot,
    rally_point_at_member_slot,
    rally_point_at_slot,
    rally_slot_for_squad,
    reconcile_tactical_squads,
)
from tests.strategies.fixture_loader import load_oracle_fixture

ROLE = {
    "WORKER": UnitRole.WORKER,
    "VANGUARD": UnitRole.VANGUARD,
    "RANGER": UnitRole.RANGER,
}


def _coordinate(value: list[int]) -> Coordinate:
    return Coordinate(value[0], value[1])


def _obstacles(records: list[list[int]]) -> frozenset[str]:
    return frozenset(f"{x},{y}" for x, y in records)


def _unit(record: tuple[Any, ...]) -> PlanningUnit:
    return PlanningUnit(
        id=EntityId(record[0]),
        unit_role=ROLE[record[1]],
        position=_coordinate(record[2]),
        health=record[3],
        cargo=record[4],
    )


def _squads_payload(membership: SquadMembership) -> dict[str, Any]:
    return {
        "squads": [
            {
                "id": squad.id,
                "role": squad.role,
                "index": squad.index,
                "vanguardIds": list(squad.vanguard_ids),
                "rangerIds": list(squad.ranger_ids),
            }
            for squad in membership.squads
        ],
        "squadByUnit": dict(membership.squad_by_unit),
    }


# Reconstructed inputs mirroring the TypeScript fixture generator.
_SQUAD_CASES: dict[str, tuple[list[tuple[Any, ...]], dict[str, Any] | None, dict[str, Any]]] = {
    "empty": ([], None, {}),
    "one_vanguard": ([("v1", "VANGUARD", [1, 1], 2, 0)], None, {}),
    "home_2v1r": (
        [
            ("v1", "VANGUARD", [1, 1], 2, 0),
            ("v2", "VANGUARD", [2, 2], 2, 0),
            ("r1", "RANGER", [3, 3], 2, 0),
        ],
        None,
        {},
    ),
    "two_squads": (
        [
            ("v1", "VANGUARD", [1, 1], 2, 0),
            ("v2", "VANGUARD", [2, 2], 2, 0),
            ("r1", "RANGER", [3, 3], 2, 0),
            ("v3", "VANGUARD", [4, 4], 2, 0),
            ("v4", "VANGUARD", [5, 5], 2, 0),
            ("r2", "RANGER", [6, 6], 2, 0),
        ],
        None,
        {},
    ),
    "sticky_previous": (
        [
            ("v1", "VANGUARD", [1, 1], 2, 0),
            ("v2", "VANGUARD", [2, 2], 2, 0),
            ("r1", "RANGER", [3, 3], 2, 0),
            ("v3", "VANGUARD", [4, 4], 2, 0),
            ("r2", "RANGER", [6, 6], 2, 0),
        ],
        {
            "v1": "local:strike:0",
            "v2": "local:strike:0",
            "r1": "local:strike:0",
            "v3": "local:home:0",
            "r2": "local:home:0",
        },
        {},
    ),
    "home_anchor_selection": (
        [
            ("v1", "VANGUARD", [9, 9], 2, 0),
            ("v2", "VANGUARD", [1, 0], 2, 0),
            ("r1", "RANGER", [2, 0], 2, 0),
        ],
        None,
        {"homeAnchor": [0, 0]},
    ),
    "mobile_remainder": (
        [
            ("v1", "VANGUARD", [1, 1], 2, 0),
            ("v2", "VANGUARD", [2, 2], 2, 0),
            ("r1", "RANGER", [3, 3], 2, 0),
            ("v3", "VANGUARD", [4, 4], 2, 0),
            ("v4", "VANGUARD", [5, 5], 2, 0),
            ("r2", "RANGER", [6, 6], 2, 0),
            ("v5", "VANGUARD", [7, 7], 2, 0),
            ("v6", "VANGUARD", [8, 8], 2, 0),
            ("r3", "RANGER", [9, 9], 2, 0),
        ],
        None,
        {},
    ),
}


def test_tactical_squads_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["tactical_squads"]:
        units, previous, opts = _SQUAD_CASES[case["name"]]
        anchor = None if "homeAnchor" not in opts else _coordinate(opts["homeAnchor"])
        membership = reconcile_tactical_squads(
            tuple(_unit(unit) for unit in units),
            previous,
            case["tenantId"],
            home_anchor=anchor,
        )
        assert _squads_payload(membership) == case["expected"], case["name"]


def test_rally_slots_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["rally_slots"]:
        assert rally_slot_for_squad(case["squadIndex"]) == case["expected"], case["name"]


def test_rally_points_at_slot_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["rally_points_at_slot"]:
        got = rally_point_at_slot(
            _coordinate(case["target"]),
            _coordinate(case["home"]),
            _obstacles(case["obstacles"]),
            _obstacles(case["resourceCells"]),
            case["slot"],
        )
        assert got == _coordinate(case["expected"]), case["name"]


def test_rally_member_slots_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["rally_member_slots"]:
        got = rally_member_slot(case["squadIndex"], case["memberIndex"])
        assert got == case["expected"], case["name"]


def test_rally_points_at_member_slot_match_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["rally_points_at_member_slot"]:
        got = rally_point_at_member_slot(
            _coordinate(case["target"]),
            _coordinate(case["home"]),
            _obstacles(case["obstacles"]),
            _obstacles(case["resourceCells"]),
            case["slot"],
        )
        assert got == _coordinate(case["expected"]), case["name"]


def test_empty_squads_membership() -> None:
    assert reconcile_tactical_squads((), None, "local") is EMPTY_SQUAD_MEMBERSHIP
    assert EMPTY_SQUAD_MEMBERSHIP.squads == ()


def test_squad_membership_validation() -> None:
    with pytest.raises(TypeError):
        reconcile_tactical_squads(cast(tuple[PlanningUnit, ...], []), None, "local")
    with pytest.raises(ValueError):
        reconcile_tactical_squads((), None, "")
    with pytest.raises(ValueError):
        TacticalSquad(id="x", role="UNKNOWN", index=0, vanguard_ids=(), ranger_ids=())
    with pytest.raises(ValueError):
        TacticalSquad(id="x", role="STRIKE", index=-1, vanguard_ids=(), ranger_ids=())


def test_rally_helpers_reject_invalid_inputs() -> None:
    with pytest.raises(TypeError):
        rally_slot_for_squad(cast(int, "0"))
    with pytest.raises(TypeError):
        rally_point_at_slot(
            Coordinate(10, 10),
            Coordinate(0, 0),
            cast(frozenset[str], set()),
            frozenset(),
            0,
        )
    with pytest.raises(TypeError):
        rally_member_slot(0, cast(int, "1"))
