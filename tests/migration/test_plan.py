"""Migration plan schema parse tests (migration-system-v1 §6.1).

Strict fail-closed parsing: any missing, mistyped, or unknown field value must
reject the whole plan; round-trips must be lossless.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from arena_hero_agent.migration.plan import (
    MigrationAssist,
    MigrationClearRequest,
    MigrationPlanV1,
    MigrationReplenish,
    parse_migration_plan,
)
from arena_hero_agent.migration.state_machine import MigrationState

RawMutation = Callable[[dict[str, object]], dict[str, object]]


def valid_raw(make_plan: Callable[..., MigrationPlanV1]) -> dict[str, object]:
    return make_plan().to_json_object()


def test_round_trip_preserves_the_plan(make_plan: Callable[..., MigrationPlanV1]) -> None:
    plan = make_plan()
    result = parse_migration_plan(plan.to_json_object())
    assert result.ok
    assert result.plan is not None
    assert result.plan == plan


def test_round_trip_with_optional_m6_m8_sections(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    plan = make_plan()
    plan_with_extras = replace(
        plan,
        clear_requests=(MigrationClearRequest(x=-20, y=41, reason="mine"),),
        assist=MigrationAssist(clear_ahead_cells=3, clear_ahead_reason="initial"),
        replenish=MigrationReplenish(gap=1, missing_role="SC", since_tick=120),
    )
    result = parse_migration_plan(plan_with_extras.to_json_object())
    assert result.ok
    assert result.plan == plan_with_extras


def test_json_shape_uses_schema_camel_case_keys(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    payload = make_plan().to_json_object()
    assert payload["schema"] == "migration-plan-v1"
    for key in (
        "operationId",
        "revision",
        "conductorEpoch",
        "tenant",
        "mode",
        "state",
        "core",
        "lease",
        "target",
        "path",
        "legs",
        "legProgress",
        "pace",
        "roles",
        "conductor",
        "updatedAt",
    ):
        assert key in payload
    assert payload["state"] == "PLAN"
    assert payload["legProgress"] == {"legIndex": 0, "cellsThisLeg": 0}


@pytest.mark.parametrize(
    ("mutate", "reason_fragment"),
    [
        (lambda raw: {**raw, "schema": "migration-plan-v2"}, "schema"),
        (lambda raw: {**raw, "operationId": ""}, "operationId"),
        (lambda raw: {**raw, "operationId": 7}, "operationId"),
        (lambda raw: {**raw, "revision": 0}, "revision"),
        (lambda raw: {**raw, "revision": True}, "revision"),
        (lambda raw: {**raw, "conductorEpoch": -1}, "conductorEpoch"),
        (lambda raw: {**raw, "tenant": ""}, "tenant"),
        (lambda raw: {**raw, "mode": "bogus"}, "mode"),
        (lambda raw: {**raw, "state": "BOGUS"}, "state"),
        (lambda raw: {**raw, "state": 7}, "state"),
        (
            lambda raw: {
                **raw,
                "core": {"originCoreId": 5, "currentCoreId": None, "generation": 1},
            },
            "originCoreId",
        ),
        (
            lambda raw: {
                **raw,
                "core": {"originCoreId": "uuid-A", "currentCoreId": "uuid-A", "generation": -1},
            },
            "generation",
        ),
        (
            lambda raw: {
                **raw,
                "lease": {"untilTick": "soon", "heartbeatAt": "2026-08-08T21:30:00.000Z"},
            },
            "untilTick",
        ),
        (
            lambda raw: {
                **raw,
                "lease": {"untilTick": 74_123, "heartbeatAt": "not-a-date"},
            },
            "heartbeatAt",
        ),
        (lambda raw: {**raw, "target": {"x": -20, "y": 40}}, "target"),
        (
            lambda raw: {**raw, "path": {"cells": "nope", "corridorWidth": 8, "lookahead": 30}},
            "path.cells",
        ),
        (
            lambda raw: {
                **raw,
                "path": {"cells": [[-583, "x"]], "corridorWidth": 8, "lookahead": 30},
            },
            "path.cells",
        ),
        (
            lambda raw: {**raw, "path": {"cells": [], "corridorWidth": -1, "lookahead": 30}},
            "corridorWidth",
        ),
        (
            lambda raw: {
                **raw,
                "legs": [
                    {
                        "index": 0,
                        "from": {"x": 0, "y": 0},
                        "to": {"x": 1, "y": 1},
                        "audit": {"ok": "yes"},
                    }
                ],
            },
            "audit",
        ),
        (lambda raw: {**raw, "legProgress": {"legIndex": "first"}}, "legProgress"),
        (lambda raw: {**raw, "pace": {"policy": "wild", "burstCells": 8}}, "pace.policy"),
        (lambda raw: {**raw, "pace": {"policy": "adaptive", "burstCells": 0}}, "pace.burstCells"),
        (
            lambda raw: {
                **raw,
                "roles": {
                    "quotas": {"escort": -1, "sweep": 30, "scout": 15, "rear": 15},
                    "seed": 1,
                },
            },
            "escort",
        ),
        (lambda raw: {**raw, "conductor": {"pid": "abc"}}, "conductor.pid"),
        (lambda raw: {**raw, "updatedAt": "yesterday"}, "updatedAt"),
        (
            lambda raw: {
                **raw,
                "clearRequests": [
                    {"x": 1, "y": 2},
                    {"x": 1, "y": 2},
                    {"x": 1, "y": 2},
                    {"x": 1, "y": 2},
                ],
            },
            "clearRequests",
        ),
        (
            lambda raw: {**raw, "assist": {"clearAheadCells": 0, "clearAheadReason": "initial"}},
            "clearAheadCells",
        ),
        (
            lambda raw: {**raw, "assist": {"clearAheadCells": 2, "clearAheadReason": "sometimes"}},
            "clearAheadReason",
        ),
        (
            lambda raw: {**raw, "replenish": {"gap": 0, "missingRole": "SC", "sinceTick": 1}},
            "gap",
        ),
        (
            lambda raw: {**raw, "replenish": {"gap": 1, "missingRole": "TANK", "sinceTick": 1}},
            "missingRole",
        ),
    ],
)
def test_parse_rejects_malformed_plans(
    make_plan: Callable[..., MigrationPlanV1],
    mutate: RawMutation,
    reason_fragment: str,
) -> None:
    result = parse_migration_plan(mutate(valid_raw(make_plan)))
    assert not result.ok
    assert result.reason is not None
    assert reason_fragment in result.reason


@pytest.mark.parametrize("bad", [None, 7, "plan", [1, 2]])
def test_parse_rejects_non_object_inputs(bad: object) -> None:
    result = parse_migration_plan(bad)
    assert not result.ok


def test_parse_rejects_unknown_state_even_with_valid_shape(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    raw = valid_raw(make_plan)
    raw["state"] = "LEG_MARCH"
    result = parse_migration_plan(raw)
    assert not result.ok
    assert "state" in (result.reason or "")


def test_parse_accepts_all_known_states(make_plan: Callable[..., MigrationPlanV1]) -> None:
    for state in MigrationState:
        raw = make_plan(state=state).to_json_object()
        result = parse_migration_plan(raw)
        assert result.ok, state
        assert result.plan is not None
        assert result.plan.state == state
