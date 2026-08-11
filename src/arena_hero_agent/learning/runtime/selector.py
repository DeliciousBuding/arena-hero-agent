"""Bounded runtime candidate selection (P4-14).

The runtime keeps only production candidates: research/experimental strategy
variants must never be selected by the live agent, they belong to
``arena-hero-lab``. This module is the bounded selection core:

- production filtering is fail-closed: a candidate is selectable only when its
  ``production`` marker is explicitly true, anything else (unmarked or false)
  is rejected with a stable reason and never appears in ``selected``;
- the result is bounded: at most ``bound`` candidates are selected, the rest of
  the eligible (production) pool is recorded in ``truncated``;
- the selection is fully deterministic: no randomness, a stable total order
  (priority desc, then candidate id asc) so ties always break the same way;
- corrupted input (duplicate ids, wrong types, bad bound) fails closed by
  raising, because determinism cannot be guaranteed over corrupt input.

The candidates carry their ``SafetyPlannerConfig`` as the SafetyPlanner
constraint surface (P4-11): the selector treats the config as opaque candidate
data (validated by the dataclass), it does not mutate or weaken it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ...strategies.safety_planner_config import (
    AGGRESSIVE_SAFETY_CONFIG,
    DEFAULT_SAFETY_CONFIG,
    SafetyPlannerConfig,
)
from ...strategies.variant_registry import (
    VARIANT_SAFETY_CONFIG,
    apply_variant_overrides,
    is_production_variant,
)

# Stable rejection reasons; part of the deterministic contract.
REJECT_NON_PRODUCTION = "non-production"
# Stable canonical ids of the production baseline strategy configs. The
# runtime candidate set wires exactly these ids to their baseline configs.
PRODUCTION_BASELINE_IDS: Final[tuple[str, ...]] = ("default-v1", "aggressive-v1")
_PRODUCTION_BASELINE_CONFIGS: Final[dict[str, SafetyPlannerConfig]] = {
    "default-v1": DEFAULT_SAFETY_CONFIG,
    "aggressive-v1": AGGRESSIVE_SAFETY_CONFIG,
}


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    """One strategy option offered to the runtime selector.

    ``production`` is the runtime eligibility marker (P4-14): unmarked/false
    candidates are research variants and are rejected fail-closed. ``config``
    is the candidate's SafetyPlanner constraint surface; ``priority`` ranks
    eligible candidates (higher first), with ``id`` as the deterministic tie
    breaker.
    """

    __canonical_name__ = "arena-hero.strategy-candidate.v1"

    id: str
    production: bool = False
    priority: int = 0
    config: SafetyPlannerConfig | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("candidate id must be a non-empty string")
        if not isinstance(self.production, bool):
            raise TypeError("candidate production must be a boolean")
        _require_int("candidate priority", self.priority, minimum=0)
        if self.config is not None and not isinstance(self.config, SafetyPlannerConfig):
            raise TypeError("candidate config must be a SafetyPlannerConfig or None")


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """A candidate explicitly rejected with a stable reason."""

    __canonical_name__ = "arena-hero.rejected-candidate.v1"

    id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("rejected id must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("rejected reason must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Bounded selection outcome; every list is in deterministic order.

    ``selected`` is ordered by the selection rank (priority desc, id asc) and
    holds at most ``bound`` candidates. ``rejected`` lists non-production
    candidates sorted by id asc. ``truncated`` lists the eligible candidates
    that exceeded ``bound``, in selection order.
    """

    __canonical_name__ = "arena-hero.selection-result.v1"

    selected: tuple[StrategyCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]
    truncated: tuple[str, ...]
    bound: int

    def __post_init__(self) -> None:
        if not isinstance(self.selected, tuple) or not all(
            isinstance(item, StrategyCandidate) for item in self.selected
        ):
            raise TypeError("selected must be a tuple of StrategyCandidate")
        if not isinstance(self.rejected, tuple) or not all(
            isinstance(item, RejectedCandidate) for item in self.rejected
        ):
            raise TypeError("rejected must be a tuple of RejectedCandidate")
        if not isinstance(self.truncated, tuple) or not all(
            isinstance(item, str) and item for item in self.truncated
        ):
            raise TypeError("truncated must be a tuple of non-empty strings")
        _require_int("selection bound", self.bound, minimum=0)
        if len(self.selected) > self.bound:
            raise ValueError("selected exceeds bound")
        for item in self.selected:
            if item.id in self.truncated:
                raise ValueError("a candidate cannot be both selected and truncated")


def _require_int(label: str, value: object, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")


def select_candidates(
    candidates: Sequence[StrategyCandidate],
    bound: int,
) -> SelectionResult:
    """Select at most ``bound`` production candidates, deterministically.

    Non-production candidates are rejected (fail-closed), eligible candidates
    are ranked by (priority desc, id asc), and anything past ``bound`` is
    recorded as truncated. Duplicate candidate ids are corrupt input and fail
    closed by raising.
    """

    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise TypeError("candidates must be a sequence of StrategyCandidate")
    if not all(isinstance(item, StrategyCandidate) for item in candidates):
        raise TypeError("candidates must contain only StrategyCandidate items")
    _require_int("selection bound", bound, minimum=0)

    seen: set[str] = set()
    for item in candidates:
        if item.id in seen:
            raise ValueError(f"duplicate candidate id: {item.id}")
        seen.add(item.id)

    rejected = tuple(
        RejectedCandidate(id=item.id, reason=REJECT_NON_PRODUCTION)
        for item in candidates
        if not item.production
    )
    eligible = [item for item in candidates if item.production]
    eligible.sort(key=lambda item: (-item.priority, item.id))
    selected = tuple(eligible[:bound])
    truncated = tuple(item.id for item in eligible[bound:])
    return SelectionResult(
        selected=selected,
        rejected=rejected,
        truncated=truncated,
        bound=bound,
    )


def select_runtime_candidates(bound: int) -> SelectionResult:
    """Wire the strategies surface into the runtime selector (P4-14).

    The runtime candidate set is built from the strategies registry: the
    production baseline configs are always eligible, and every registered
    strategy variant is eligible only when its id is declared in the registry's
    production marker (``VARIANT_PRODUCTION``). Unmarked variants are rejected
    fail-closed, so research variants never reach the selected set.
    """

    candidates = [
        StrategyCandidate(
            id=candidate_id,
            production=True,
            priority=0,
            config=_PRODUCTION_BASELINE_CONFIGS[candidate_id],
        )
        for candidate_id in PRODUCTION_BASELINE_IDS
    ]
    for variant_id in sorted(VARIANT_SAFETY_CONFIG):
        candidates.append(
            StrategyCandidate(
                id=variant_id,
                production=is_production_variant(variant_id),
                priority=1,
                config=apply_variant_overrides(DEFAULT_SAFETY_CONFIG, [variant_id]),
            )
        )
    return select_candidates(candidates, bound)
