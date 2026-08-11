"""Versioned offline known-answer fixture for the Turn-to-plan adaptation chain."""

from __future__ import annotations

import dataclasses
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from arena_hero import Accepted, AsyncTurn, CommandPlan, PlayerState

from arena_hero_agent.adapters.sdk import (
    adapt_async_turn,
    build_command_plan,
    command_plan_payload,
)
from arena_hero_agent.application import (
    CoreAction,
    CoreIntent,
    Decision,
    UnitAction,
    UnitIntent,
)
from arena_hero_agent.domain import (
    Coordinate,
    Direction,
    EntityId,
    UnitRole,
    canonical_sha256,
)

FIXTURE = Path(__file__).parent / "fixtures" / "turn_to_plan_known_answers_v1.json"


async def _noop_submit(plan: CommandPlan, idempotency_key: str | None = None) -> Accepted:
    raise AssertionError("offline fixture tests never submit")


def _payload(value: Any) -> Any:
    """Convert application values into an environment-neutral JSON tree."""

    if isinstance(value, EntityId):
        return value.value
    if isinstance(value, Coordinate):
        return [value.x, value.y]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple | list):
        return [_payload(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _payload(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {key: _payload(item) for key, item in sorted(value.items())}
    return value


def _decision_from_payload(payload: dict[str, Any]) -> Decision:
    unit_intents = []
    for item in payload["unit_intents"]:
        unit_intents.append(
            UnitIntent(
                unit_id=EntityId(item["unit_id"]),
                action=UnitAction(item["action"]),
                direction=None if item["direction"] is None else Direction(item["direction"]),
                target_id=None if item["target_id"] is None else EntityId(item["target_id"]),
                expected_cell=(
                    None if item["expected_cell"] is None else Coordinate(*item["expected_cell"])
                ),
            )
        )
    core = payload["core_intent"]
    core_intent = None
    if core is not None:
        core_intent = CoreIntent(
            action=CoreAction(core["action"]),
            direction=None if core["direction"] is None else Direction(core["direction"]),
            unit_role=None if core["unit_role"] is None else UnitRole(core["unit_role"]),
        )
    return Decision(tick=payload["tick"], unit_intents=tuple(unit_intents), core_intent=core_intent)


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_round_trip_pins_turn_to_plan_chain() -> None:
    fixture = _load_fixture()
    assert fixture["version"] == 1
    assert fixture["sdk"]["name"] == "arena-hero"
    assert fixture["sdk"]["version"] == "0.2.9"

    state = PlayerState.model_validate(fixture["turn"]["state"])
    turn = AsyncTurn(tick=fixture["turn"]["tick"], state=state, submitter=_noop_submit)
    observation = adapt_async_turn(turn)
    assert _payload(observation) == fixture["observation"]

    decision = _decision_from_payload(fixture["decision"])
    plan = build_command_plan(decision, observation)
    payload = command_plan_payload(plan)
    assert payload == fixture["plan"]

    digest = canonical_sha256(payload)
    assert digest == fixture["plan_sha256"]
    assert len(digest) == 64


def test_fixture_digest_is_reproducible_without_reloading() -> None:
    fixture = _load_fixture()
    state = PlayerState.model_validate(fixture["turn"]["state"])
    turn = AsyncTurn(tick=fixture["turn"]["tick"], state=state, submitter=_noop_submit)
    observation = adapt_async_turn(turn)
    decision = _decision_from_payload(fixture["decision"])
    first = canonical_sha256(command_plan_payload(build_command_plan(decision, observation)))
    second = canonical_sha256(command_plan_payload(build_command_plan(decision, observation)))
    assert first == second == fixture["plan_sha256"]


def test_fixture_is_environment_neutral() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    for forbidden in ("D:\\", "C:\\", "/mnt/", "/home/", "Users\\Ding", "Users/Ding"):
        assert forbidden not in text
