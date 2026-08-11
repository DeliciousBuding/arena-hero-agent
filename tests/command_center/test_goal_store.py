"""Goal write governance semantics (legacy goal-store.ts port)."""

from __future__ import annotations

from typing import cast

import pytest

from arena_hero_agent.command_center import (
    DEFAULT_DEDUPE_WINDOW_MS,
    DEFAULT_GOAL_POLICY,
    CommandCenterError,
    GoalEntry,
    GoalKind,
    GoalMutationInput,
    GoalMutationPolicy,
    apply_goal_mutation,
    iso_utc,
)

NOW = 1_752_000_000_000
FIXED_SUFFIX = "abc123"


def _input(
    unit_id: str = "u1",
    kind: GoalKind = "mine",
    target: tuple[int, int] = (1, 2),
    note: str | None = None,
) -> GoalMutationInput:
    return GoalMutationInput(unit_id=unit_id, kind=kind, target=target, note=note)


def _goal(
    unit_id: str = "u1",
    goal_id: str = "g1",
    kind: GoalKind = "mine",
    target: tuple[int, int] = (1, 2),
    created_at: str = iso_utc(NOW),
) -> GoalEntry:
    return GoalEntry(
        id=goal_id,
        unit_id=unit_id,
        kind=kind,
        target=target,
        created_at=created_at,
    )


def test_apply_goal_mutation_creates_goal() -> None:
    result = apply_goal_mutation(
        {"goals": ()},
        _input(),
        NOW,
        id_factory=lambda _now: FIXED_SUFFIX,
    )
    assert result.outcome.applied is True
    assert result.outcome.replaced is False
    assert result.outcome.reason is None
    assert result.outcome.goal_id == f"goal-{NOW}-{FIXED_SUFFIX}"
    assert len(result.goals) == 1
    goal = result.goals[0]
    assert goal.unit_id == "u1"
    assert goal.kind == "mine"
    assert goal.target == (1, 2)
    assert goal.created_at == iso_utc(NOW)
    assert goal.note is None


def test_apply_goal_mutation_preserves_note() -> None:
    result = apply_goal_mutation(
        {"goals": ()}, _input(note="go mine"), NOW, id_factory=lambda _n: "x"
    )
    assert result.goals[0].note == "go mine"


def test_dedupe_within_window_returns_original_goals() -> None:
    goals = (_goal(),)
    result = apply_goal_mutation(
        {"goals": goals},
        _input(),
        NOW + 5_000,
        id_factory=lambda _n: "ignored",
    )
    assert result.outcome.applied is False
    assert result.outcome.reason == "deduped"
    assert result.outcome.goal_id == goals[0].id
    assert list(result.goals) == list(goals)


def test_same_goal_after_window_is_replaced() -> None:
    result = apply_goal_mutation(
        {"goals": (_goal(),)},
        _input(),
        NOW + DEFAULT_DEDUPE_WINDOW_MS + 1,
        id_factory=lambda _n: "newid",
    )
    assert result.outcome.applied is True
    assert result.outcome.replaced is True
    assert result.outcome.goal_id == f"goal-{NOW + DEFAULT_DEDUPE_WINDOW_MS + 1}-newid"
    assert len(result.goals) == 1
    assert result.goals[0].id != _goal().id


def test_different_target_replaces_immediately() -> None:
    result = apply_goal_mutation(
        {"goals": (_goal(),)},
        _input(target=(9, 9)),
        NOW + 1_000,
        id_factory=lambda _n: "b",
    )
    assert result.outcome.applied is True
    assert result.outcome.replaced is True
    assert result.goals[0].target == (9, 9)


def test_different_kind_replaces_immediately() -> None:
    result = apply_goal_mutation(
        {"goals": (_goal(),)},
        _input(kind="goto"),
        NOW + 1_000,
        id_factory=lambda _n: "c",
    )
    assert result.outcome.applied is True
    assert result.outcome.replaced is True
    assert result.goals[0].kind == "goto"


def test_multiple_units_are_independent() -> None:
    first = apply_goal_mutation(
        {"goals": ()},
        _input(unit_id="u1"),
        NOW,
        id_factory=lambda _n: "a",
    )
    second = apply_goal_mutation(
        {"goals": first.goals},
        _input(unit_id="u2"),
        NOW,
        id_factory=lambda _n: "b",
    )
    assert len(second.goals) == 2
    assert [g.unit_id for g in second.goals] == ["u1", "u2"]


def test_unparseable_created_at_never_dedupes() -> None:
    result = apply_goal_mutation(
        {"goals": (_goal(created_at="not-a-date"),)},
        _input(),
        NOW + 1_000,
        id_factory=lambda _n: "z",
    )
    assert result.outcome.applied is True


def test_custom_policy_window() -> None:
    policy = GoalMutationPolicy(dedupe_window_ms=1_000)
    within = apply_goal_mutation(
        {"goals": (_goal(),)},
        _input(),
        NOW + 500,
        policy,
        id_factory=lambda _n: "w",
    )
    assert within.outcome.applied is False
    outside = apply_goal_mutation(
        {"goals": (_goal(),)},
        _input(),
        NOW + 1_500,
        policy,
        id_factory=lambda _n: "o",
    )
    assert outside.outcome.applied is True


def test_goal_mutation_is_pure() -> None:
    goals = (_goal(),)
    result = apply_goal_mutation(
        {"goals": goals}, _input(target=(5, 5)), NOW, id_factory=lambda _n: "p"
    )
    assert result.goals != goals
    assert goals[0].target == (1, 2)


def test_default_policy_frozen() -> None:
    assert DEFAULT_GOAL_POLICY.dedupe_window_ms == DEFAULT_DEDUPE_WINDOW_MS
    assert DEFAULT_DEDUPE_WINDOW_MS == 30_000


@pytest.mark.parametrize("bad", [True, -1, 1.5])
def test_invalid_policy_window_raises(bad: object) -> None:
    with pytest.raises(CommandCenterError, match="dedupe_window_ms"):
        GoalMutationPolicy(dedupe_window_ms=cast(int, bad))


def test_goal_entry_rejects_non_object_store() -> None:
    with pytest.raises(CommandCenterError, match="goals as a sequence"):
        apply_goal_mutation({"goals": 42}, _input(), NOW, id_factory=lambda _n: "x")  # type: ignore[arg-type]
