"""Authoritative tenant domain state identity and deterministic reducers.

P4-8 establishes the single semantic identity for one tenant partition on top of
the existing immutable ``WorldProjection`` (world) and ``DecisionId`` (decision)
boundaries. ``TenantState`` is the value type stored through the
``TenantStateStore`` port and consumed by the P4-9 lease/arbiter seam.

Ownership rules:

- ``tenant_id`` identifies the partition that owns the state; it never changes
  across reducer transitions.
- ``world`` is tenant-runtime-owned observed state. Reducers replace it with a
  newer projection and never mutate the existing one.
- ``decision_count`` and ``last_decision_id`` form the decision journal identity
  owned by decision commits. Full journal replay belongs to the event journal
  port; the state keeps only the deterministic identity the arbiter needs to
  detect duplicates.
- Generation and fencing tokens are durable-envelope concerns owned by the
  writer lease and the ``TenantStateStore`` compare-and-set protocol; they are
  intentionally not part of the semantic digest. Two revisions with identical
  semantic content therefore share the same identity, while any semantic change
  changes the digest.

Reducers are pure, fail-closed state advances:

- ``observe`` folds in a newer world projection. A tick regression or a
  different observation for the already-seen tick is rejected; an identical
  projection is a no-op.
- ``record_decision`` commits one decision. Re-committing the current last
  decision is rejected so the P4-9 arbiter can treat it as a duplicate signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from .rules import assert_current_rules_version
from .value_objects import DecisionId, StateDigest, TenantId, _require_int
from .world import WorldProjection

_MAX_SAFE_INTEGER = 2**53 - 1


@dataclass(frozen=True, slots=True)
class TenantState:
    """Immutable authoritative domain state for one tenant partition."""

    __canonical_name__ = "arena-hero.tenant-state.v1"

    tenant_id: TenantId
    world: WorldProjection
    decision_count: int = 0
    last_decision_id: DecisionId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be a TenantId")
        if not isinstance(self.world, WorldProjection):
            raise TypeError("world must be a WorldProjection")
        decision_count = _require_int("decision_count", self.decision_count)
        if decision_count < 0:
            raise ValueError("decision_count cannot be negative")
        if decision_count > _MAX_SAFE_INTEGER:
            raise ValueError("decision_count exceeds the cross-language safe-integer range")
        if self.last_decision_id is not None and not isinstance(self.last_decision_id, DecisionId):
            raise TypeError("last_decision_id must be a DecisionId or None")

    @property
    def state_digest(self) -> StateDigest:
        """Return the canonical semantic identity for this tenant state."""

        return StateDigest.from_state(self)

    def observe(self, world: WorldProjection) -> Self:
        """Fold a newer world projection into the state without mutating it.

        The tenant partition and decision journal are preserved. The projection
        must use the current rules version and must not regress or contradict an
        observation already seen for its tick.
        """

        if not isinstance(world, WorldProjection):
            raise TypeError("world must be a WorldProjection")
        assert_current_rules_version(world.rules_version)
        if world.tick < self.world.tick:
            raise ValueError(
                f"world tick {world.tick} regresses below current tick {self.world.tick}"
            )
        if world.tick == self.world.tick and world != self.world:
            raise ValueError(f"conflicting world observation for tick {world.tick}")
        if world == self.world:
            return self
        return type(self)(
            tenant_id=self.tenant_id,
            world=world,
            decision_count=self.decision_count,
            last_decision_id=self.last_decision_id,
        )

    def record_decision(self, decision_id: DecisionId) -> Self:
        """Commit one decision into the journal identity.

        Re-committing the current last decision is rejected; the arbiter treats
        it as a duplicate signal rather than silently extending the journal.
        """

        if not isinstance(decision_id, DecisionId):
            raise TypeError("decision_id must be a DecisionId")
        if decision_id == self.last_decision_id:
            raise ValueError(f"duplicate decision commit for {decision_id}")
        return type(self)(
            tenant_id=self.tenant_id,
            world=self.world,
            decision_count=self.decision_count + 1,
            last_decision_id=decision_id,
        )
