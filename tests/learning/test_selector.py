"""P4-14 bounded runtime candidate selection: table-driven, fail-closed.

Acceptance surface (Wave 16 / Line A):

- production filtering: non-production candidates are rejected and never
  selected (fail-closed);
- bound truncation: at most ``bound`` candidates are selected and the rest of
  the eligible pool is recorded in ``truncated``;
- determinism: same input yields the same output, ties break by id asc;
- empty input: empty candidate set selects nothing;
- corrupted input: duplicate ids, wrong item types, bad bound, and bad
  candidate fields fail closed by raising;
- wiring: the strategies registry marker feeds the runtime candidate set and
  unmarked research variants never reach ``selected``.
"""

from __future__ import annotations

import pytest

from arena_hero_agent.learning import (
    REJECT_NON_PRODUCTION,
    SelectionResult,
    StrategyCandidate,
    select_candidates,
    select_runtime_candidates,
)
from arena_hero_agent.strategies import (
    AGGRESSIVE_SAFETY_CONFIG,
    DEFAULT_SAFETY_CONFIG,
    VARIANT_PRODUCTION,
    VARIANT_SAFETY_CONFIG,
    SafetyPlannerConfig,
    is_production_variant,
)

CONFIG = SafetyPlannerConfig()


def cand(
    *,
    id: str,
    production: bool = False,
    priority: int = 0,
    config: SafetyPlannerConfig | None = CONFIG,
) -> StrategyCandidate:
    return StrategyCandidate(id=id, production=production, priority=priority, config=config)


def ids(result: SelectionResult) -> tuple[str, ...]:
    return tuple(item.id for item in result.selected)


def rejected_ids(result: SelectionResult) -> tuple[str, ...]:
    return tuple(item.id for item in result.rejected)


def test_production_filtering_rejects_non_production() -> None:
    cases = [
        (
            [cand(id="exp-a"), cand(id="prod-b", production=True)],
            ("prod-b",),
            ("exp-a",),
        ),
        # Unmarked (default False) candidates are non-production: fail-closed.
        ([cand(id="unmarked")], (), ("unmarked",)),
        # Explicit False marker is non-production.
        ([cand(id="exp-c", production=False)], (), ("exp-c",)),
        # Mixed: only the explicitly-marked candidates are selected.
        (
            [cand(id="exp-x"), cand(id="prod-1", production=True), cand(id="exp-y")],
            ("prod-1",),
            ("exp-x", "exp-y"),
        ),
    ]
    for candidates, expected_selected, expected_rejected in cases:
        result = select_candidates(candidates, bound=len(candidates))
        assert ids(result) == expected_selected
        assert rejected_ids(result) == expected_rejected
        assert all(item.reason == REJECT_NON_PRODUCTION for item in result.rejected)
        assert result.truncated == ()


def test_non_production_never_selected_even_with_room() -> None:
    result = select_candidates(
        [cand(id="exp-a"), cand(id="prod-b", production=True)],
        bound=10,
    )
    assert ids(result) == ("prod-b",)
    assert rejected_ids(result) == ("exp-a",)


def test_bound_truncates_and_records_eligible_pool() -> None:
    cases = [
        (3, ("a", "b", "c"), ()),
        (2, ("a", "b"), ("c",)),
        (1, ("a",), ("b", "c")),
        (0, (), ("a", "b", "c")),
    ]
    candidates = [
        cand(id="a", production=True),
        cand(id="b", production=True),
        cand(id="c", production=True),
    ]
    for bound, expected_selected, expected_truncated in cases:
        result = select_candidates(candidates, bound=bound)
        assert ids(result) == expected_selected
        assert result.truncated == expected_truncated
        assert len(result.selected) == bound
        # selected + truncated == the whole eligible pool, in selection order.
        assert ids(result) + result.truncated == ("a", "b", "c")


def test_priority_then_id_is_deterministic_total_order() -> None:
    candidates = [
        cand(id="low-a", production=True, priority=0),
        cand(id="high-z", production=True, priority=10),
        cand(id="high-a", production=True, priority=10),
        cand(id="low-b", production=True, priority=0),
        cand(id="mid-m", production=True, priority=5),
    ]
    result = select_candidates(candidates, bound=len(candidates))
    # priority desc, then id asc
    assert ids(result) == ("high-a", "high-z", "mid-m", "low-a", "low-b")


def test_same_input_yields_identical_result() -> None:
    candidates = [
        cand(id="b", production=True, priority=1),
        cand(id="a", production=True),
        cand(id="exp", priority=2),
        cand(id="c", production=True, priority=1),
    ]
    first = select_candidates(candidates, bound=2)
    second = select_candidates(candidates, bound=2)
    assert first == second
    assert first is not second
    assert ids(first) == ("b", "c")
    assert first.truncated == ("a",)
    assert rejected_ids(first) == ("exp",)


def test_empty_input_selects_nothing() -> None:
    result = select_candidates([], bound=5)
    assert result.selected == ()
    assert result.rejected == ()
    assert result.truncated == ()
    assert result.bound == 5


def test_duplicate_candidate_id_fails_closed() -> None:
    with pytest.raises(ValueError, match="duplicate candidate id: dup"):
        select_candidates([cand(id="dup", production=True), cand(id="dup")], bound=10)


def test_corrupted_candidates_fail_closed() -> None:
    with pytest.raises(TypeError, match="candidates must contain only StrategyCandidate"):
        select_candidates([cand(id="a"), "not-a-candidate"], bound=10)  # type: ignore
    with pytest.raises(TypeError, match="candidates must be a sequence"):
        select_candidates("a", bound=10)  # type: ignore


def test_bad_bound_fails_closed() -> None:
    with pytest.raises(TypeError, match="selection bound must be an integer"):
        select_candidates([cand(id="a", production=True)], bound=True)
    with pytest.raises(ValueError, match="selection bound must be >= 0"):
        select_candidates([cand(id="a", production=True)], bound=-1)


def test_bad_candidate_fields_fail_closed() -> None:
    with pytest.raises(ValueError, match="candidate id must be a non-empty string"):
        StrategyCandidate(id="")
    with pytest.raises(TypeError, match="candidate production must be a boolean"):
        StrategyCandidate(id="a", production=1)  # type: ignore
    with pytest.raises(TypeError, match="candidate priority must be an integer"):
        StrategyCandidate(id="a", priority=True)
    with pytest.raises(ValueError, match="candidate priority must be >= 0"):
        StrategyCandidate(id="a", priority=-1)
    with pytest.raises(TypeError, match="candidate config must be a SafetyPlannerConfig or None"):
        StrategyCandidate(id="a", config={"population_ceiling": 30})  # type: ignore


def test_marker_default_is_conservative_non_production() -> None:
    # Every registered strategy variant is unmarked => non-production (P4-14).
    assert frozenset() == VARIANT_PRODUCTION
    assert VARIANT_SAFETY_CONFIG
    for variant_id in VARIANT_SAFETY_CONFIG:
        assert is_production_variant(variant_id) is False
    assert is_production_variant("no-such-variant") is False
    with pytest.raises(TypeError, match="variant id must be a string"):
        is_production_variant(1)  # type: ignore


def test_runtime_wiring_selects_only_production_baseline() -> None:
    result = select_runtime_candidates(bound=10)
    # Registered variants are research candidates: rejected fail-closed.
    assert rejected_ids(result) == tuple(sorted(VARIANT_SAFETY_CONFIG))
    # Production baseline configs are selected, bounded and deterministic.
    assert ids(result) == ("aggressive-v1", "default-v1")
    assert result.selected[0].config == AGGRESSIVE_SAFETY_CONFIG
    assert result.selected[1].config == DEFAULT_SAFETY_CONFIG
    assert result.truncated == ()
    assert all(item.reason == REJECT_NON_PRODUCTION for item in result.rejected)


def test_runtime_wiring_respects_bound() -> None:
    result = select_runtime_candidates(bound=1)
    assert ids(result) == ("aggressive-v1",)
    assert result.truncated == ("default-v1",)
    assert rejected_ids(result) == tuple(sorted(VARIANT_SAFETY_CONFIG))
