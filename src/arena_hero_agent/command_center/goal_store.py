"""Human goal write governance (port of legacy ``goal-store.ts``).

The oracle's dedupe window collapses repeated writes of the same goal
(same unit, same kind, same target) within a window into a single entry so
the core does not receive a constant stream of duplicate ``START_MOVE``
commands. This module is a pure function layer: it never writes files or
audits; the caller decides whether to persist and audit.

Registered difference from the TS oracle: goal ids are opaque. The oracle
uses ``goal-<ms>-<6 base36 chars>``; this port uses ``goal-<ms>-<6 hex chars>``
via an injectable id factory (defaults to ``secrets``). Values are never
compared across languages.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .errors import CommandCenterError

GoalKind = Literal["mine", "goto"]
DEDUPE_REASON = "deduped"

DEFAULT_DEDUPE_WINDOW_MS = 30_000


def iso_utc(now_ms: int) -> str:
    """Format an epoch-millis timestamp as ``YYYY-MM-DDTHH:MM:SS.mmmZ``.

    Matches the TypeScript ``new Date(ms).toISOString()`` output shape used
    for ``createdAt`` and store ``updatedAt`` fields.
    """
    if isinstance(now_ms, bool) or not isinstance(now_ms, int):
        raise CommandCenterError(f"now_ms must be an integer; actual={now_ms!r}")
    dt = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_epoch_ms(value: str) -> int | None:
    """Parse an ISO-8601 UTC timestamp into epoch millis (None when invalid)."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class GoalEntry:
    """One human goal targeting a unit at a position."""

    id: str
    unit_id: str
    kind: GoalKind
    target: tuple[int, int]
    created_at: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class GoalMutationInput:
    """Proposed goal write from a Command Center write route."""

    unit_id: str
    kind: GoalKind
    target: tuple[int, int]
    note: str | None = None


@dataclass(frozen=True, slots=True)
class GoalMutationPolicy:
    """Dedupe window for repeated goal writes."""

    dedupe_window_ms: int = DEFAULT_DEDUPE_WINDOW_MS

    def __post_init__(self) -> None:
        if isinstance(self.dedupe_window_ms, bool) or not isinstance(self.dedupe_window_ms, int):
            raise CommandCenterError(
                f"dedupe_window_ms must be an integer; actual={self.dedupe_window_ms!r}"
            )
        if self.dedupe_window_ms < 0:
            raise CommandCenterError(
                f"dedupe_window_ms cannot be negative; actual={self.dedupe_window_ms}"
            )


DEFAULT_GOAL_POLICY = GoalMutationPolicy()


@dataclass(frozen=True, slots=True)
class GoalMutationOutcome:
    """Result of one goal write attempt."""

    applied: bool
    goal_id: str
    replaced: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class GoalMutationResult:
    """Outcome plus the resulting goals array (unchanged on dedupe)."""

    outcome: GoalMutationOutcome
    goals: tuple[GoalEntry, ...]


@dataclass(frozen=True, slots=True)
class _GoalStoreView:
    goals: tuple[GoalEntry, ...]


def apply_goal_mutation(
    store: object,
    input: GoalMutationInput,
    now_ms: int,
    policy: GoalMutationPolicy = DEFAULT_GOAL_POLICY,
    *,
    id_factory: Callable[[int], str] | None = None,
) -> GoalMutationResult:
    """Apply a goal write: dedupe within the window, else replace per unit.

    Pure function: the input store is never mutated; a replaced goal returns a
    new goals tuple, a deduped goal returns the original tuple (caller should
    skip write and audit).
    """
    goals = _store_goals(store)
    existing = next((goal for goal in goals if goal.unit_id == input.unit_id), None)
    if existing is not None and _same_goal(existing, input):
        age_ms = _goal_age_ms(existing, now_ms)
        if age_ms is not None and 0 <= age_ms < policy.dedupe_window_ms:
            return GoalMutationResult(
                outcome=GoalMutationOutcome(
                    applied=False, goal_id=existing.id, reason=DEDUPE_REASON
                ),
                goals=goals,
            )
    suffix = id_factory(now_ms) if id_factory is not None else secrets.token_hex(3)
    goal = GoalEntry(
        id=f"goal-{now_ms}-{suffix}",
        unit_id=input.unit_id,
        kind=input.kind,
        target=(input.target[0], input.target[1]),
        note=input.note,
        created_at=iso_utc(now_ms),
    )
    next_goals = tuple(g for g in goals if g.unit_id != input.unit_id) + (goal,)
    return GoalMutationResult(
        outcome=GoalMutationOutcome(applied=True, goal_id=goal.id, replaced=existing is not None),
        goals=next_goals,
    )


def _store_goals(store: object) -> tuple[GoalEntry, ...]:
    goals = store.get("goals") if isinstance(store, dict) else getattr(store, "goals", None)
    if goals is None:
        return ()
    if isinstance(goals, tuple):
        return goals
    if isinstance(goals, list):
        return tuple(goals)
    raise CommandCenterError(
        f"goal store must expose goals as a sequence; actual={type(goals).__name__}"
    )


def _same_goal(existing: GoalEntry, input: GoalMutationInput) -> bool:
    return (
        existing.kind == input.kind
        and existing.target[0] == input.target[0]
        and existing.target[1] == input.target[1]
    )


def _goal_age_ms(goal: GoalEntry, now_ms: int) -> int | None:
    parsed = _parse_epoch_ms(goal.created_at)
    if parsed is None:
        return None
    return now_ms - parsed


__all__ = [
    "DEDUPE_REASON",
    "DEFAULT_DEDUPE_WINDOW_MS",
    "DEFAULT_GOAL_POLICY",
    "GoalEntry",
    "GoalMutationInput",
    "GoalMutationOutcome",
    "GoalMutationPolicy",
    "GoalMutationResult",
    "apply_goal_mutation",
    "iso_utc",
]
