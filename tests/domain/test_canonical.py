from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arena_hero_agent.domain import (
    Coordinate,
    Direction,
    StateDigest,
    canonical_json_bytes,
    canonical_sha256,
)


@given(st.dictionaries(st.text(min_size=1), st.integers(), max_size=30))
def test_mapping_digest_is_independent_of_insertion_order(values: dict[str, int]) -> None:
    reversed_values = dict(reversed(list(values.items())))

    assert canonical_sha256(values) == canonical_sha256(reversed_values)


@given(st.sets(st.integers(), max_size=30))
def test_set_digest_is_independent_of_iteration_order(values: set[int]) -> None:
    forward = set(values)
    reverse = set(reversed(sorted(values)))

    assert canonical_json_bytes(forward) == canonical_json_bytes(reverse)


def test_sequence_order_remains_semantic() -> None:
    assert canonical_sha256([1, 2]) != canonical_sha256([2, 1])
    assert canonical_sha256([1, 2]) != canonical_sha256({1, 2})


def test_value_object_type_and_fields_enter_digest() -> None:
    assert StateDigest.from_state(Coordinate(2, 3)) != StateDigest.from_state({"x": 2, "y": 3})
    assert canonical_sha256(Direction.NORTH) != canonical_sha256("north")


@pytest.mark.parametrize("value", [1.0, float("nan"), datetime.now(UTC)])
def test_nondeterministic_or_platform_sensitive_values_are_rejected(value: object) -> None:
    with pytest.raises(TypeError):
        canonical_json_bytes(value)


def test_mapping_keys_must_be_strings() -> None:
    with pytest.raises(TypeError, match="string keys"):
        canonical_json_bytes({1: "value"})


def test_unicode_uses_stable_utf8_encoding() -> None:
    assert canonical_json_bytes({"é": "值"}) == canonical_json_bytes({"é": "值"})
    assert b"\\u" not in canonical_json_bytes({"é": "值"})
