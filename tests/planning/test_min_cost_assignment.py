"""Differential and boundary tests for the deterministic Hungarian solver."""

from __future__ import annotations

from typing import cast

import pytest

from arena_hero_agent.planning import minimum_cost_assignment
from tests.strategies.fixture_loader import load_oracle_fixture


def test_min_cost_assignment_matches_ts_oracle() -> None:
    fixture = load_oracle_fixture()
    for case in fixture["min_cost_assignment"]:
        got = minimum_cost_assignment(case["matrix"])
        assert got == case["expected"], case["name"]


def test_empty_matrix_returns_empty() -> None:
    assert minimum_cost_assignment([]) == []


def test_single_row_single_column() -> None:
    assert minimum_cost_assignment([[5.0]]) == [0]


def test_rejects_non_rectangular() -> None:
    with pytest.raises(ValueError, match="rectangular"):
        minimum_cost_assignment([[1.0, 2.0], [3.0]])


def test_rejects_rows_exceeding_columns() -> None:
    with pytest.raises(ValueError, match="rows <= columns"):
        minimum_cost_assignment([[1.0], [2.0]])


def test_rejects_non_finite_costs() -> None:
    with pytest.raises(ValueError, match="finite"):
        minimum_cost_assignment([[float("inf"), 1.0]])


def test_rejects_non_numeric_costs() -> None:
    with pytest.raises(TypeError, match="numbers"):
        minimum_cost_assignment(cast(list[list[float]], [["x"]]))


def test_rejects_boolean_costs() -> None:
    with pytest.raises(TypeError, match="numbers"):
        minimum_cost_assignment(cast(list[list[float]], [[True]]))
