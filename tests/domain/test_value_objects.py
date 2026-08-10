from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arena_hero_agent.domain import (
    Coordinate,
    DeadlineBudget,
    DecisionId,
    Direction,
    EntityId,
    FencingToken,
    Generation,
    StateDigest,
    TenantId,
)


@pytest.mark.parametrize("value", ["", " tenant", "tenant ", "Tenant", "tenant/one", "a" * 65])
def test_tenant_id_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError):
        TenantId(value)


@pytest.mark.parametrize("value", ["", " entity", "entity ", "entity/one", "a" * 129])
def test_entity_id_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError):
        EntityId(value)


@pytest.mark.parametrize("factory", [TenantId, EntityId, DecisionId])
def test_identifiers_reject_non_strings(
    factory: Callable[[str], TenantId | EntityId | DecisionId],
) -> None:
    with pytest.raises(TypeError):
        factory(cast(str, 123))


def _set_attribute(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


def test_value_objects_are_immutable() -> None:
    tenant_id = TenantId("sample")

    with pytest.raises(FrozenInstanceError):
        _set_attribute(tenant_id, "value", "changed")


def test_decision_id_is_derived_without_wall_clock() -> None:
    first = DecisionId.from_deterministic_input({"tenant": "alpha", "sequence": 3})
    second = DecisionId.from_deterministic_input({"sequence": 3, "tenant": "alpha"})

    assert first == second
    assert first.value.startswith("decision:")

    with pytest.raises(TypeError, match="wall-clock"):
        DecisionId.from_deterministic_input(datetime.now(UTC))


@pytest.mark.parametrize("coordinate", [(True, 0), (0, False), (2**31, 0), (0, -(2**31) - 1)])
def test_coordinate_rejects_invalid_components(coordinate: tuple[object, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        Coordinate(cast(int, coordinate[0]), cast(int, coordinate[1]))


def test_coordinate_has_stable_order_and_direction_steps() -> None:
    origin = Coordinate(0, 0)

    assert sorted([Coordinate(1, 0), origin, Coordinate(0, 1)]) == [
        origin,
        Coordinate(0, 1),
        Coordinate(1, 0),
    ]
    assert origin.step(Direction.NORTH, 2) == Coordinate(0, -2)
    assert Direction.NORTH.opposite is Direction.SOUTH


@pytest.mark.parametrize("value", [-1, -10])
def test_deadline_budget_rejects_negative_values(value: int) -> None:
    with pytest.raises(ValueError):
        DeadlineBudget(value)
    with pytest.raises(ValueError):
        DeadlineBudget.from_milliseconds(value)


def test_deadline_budget_consumption_is_immutable_and_saturating() -> None:
    original = DeadlineBudget.from_milliseconds(5)

    assert original.consume(2_000_000) == DeadlineBudget(3_000_000)
    assert original.consume(10_000_000).exhausted
    assert original == DeadlineBudget(5_000_000)


@given(st.integers(min_value=0, max_value=10**12))
def test_generation_successor_is_strictly_monotonic(value: int) -> None:
    current = Generation(value)
    successor = current.next()

    assert successor.supersedes(current)
    assert not current.supersedes(successor)
    assert successor.value == value + 1


@given(st.integers(min_value=1, max_value=10**12))
def test_fencing_token_successor_is_strictly_monotonic(value: int) -> None:
    current = FencingToken(value)
    successor = current.next()

    assert successor.supersedes(current)
    assert not current.supersedes(successor)
    assert successor.value == value + 1


@pytest.mark.parametrize("factory,value", [(Generation, -1), (FencingToken, 0), (FencingToken, -1)])
def test_monotonic_values_reject_invalid_ranges(
    factory: Callable[[int], Generation | FencingToken], value: int
) -> None:
    with pytest.raises(ValueError):
        factory(value)


def test_state_digest_requires_lowercase_sha256() -> None:
    valid = StateDigest.from_state({"generation": Generation(2)})

    assert len(valid.value) == 64
    with pytest.raises(ValueError):
        StateDigest(valid.value.upper())
