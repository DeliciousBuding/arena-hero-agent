"""Authoritative tenant domain state identity and deterministic reducers.

P4-8 establishes the single semantic identity for one tenant partition on top of
the existing immutable ``WorldProjection`` (world) and ``DecisionId`` (decision)
boundaries. P4-10 embeds ``EconomyState`` in that identity instead of creating a
parallel state model. ``TenantState`` remains the value type stored through the
``TenantStateStore`` port and consumed by the P4-9 lease/arbiter seam.

Ownership rules:

- ``tenant_id`` is the owner of the state partition; it never changes across
  reducer transitions.
- Every external state advance must be authorized by an actor whose
  ``TenantId`` matches the state owner. ``require_owner`` and ``reduce_turn``
  fail closed on cross-tenant or non-owner writes; ``observe`` and
  ``record_decision`` enforce the same rule when an actor is declared.
- ``world`` is tenant-runtime-owned observed state. Reducers replace it with a
  newer projection and never mutate the existing one.
- ``economy`` is advanced from the same ``TurnInput`` as ``world``. Tick, rules
  identity, and controlled population must agree before either projection is
  accepted.
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

- ``observe`` folds in one newer world/economy observation. A tick regression
  or a different observation for the already-seen tick is rejected; an
  identical observation is a no-op.
- ``record_decision`` commits one decision. Re-committing the current last
  decision is rejected so the P4-9 arbiter can treat it as a duplicate signal.
- ``reduce_turn`` applies one round input (``TurnInput``) plus a decision in a
  single pure step and requires an explicit owning actor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from .economy import EconomyDecisionInput, EconomyState, EconomyTurnInput
from .rules import assert_current_rules_version
from .value_objects import DecisionId, StateDigest, TenantId, _require_int
from .world import WorldProjection

_MAX_SAFE_INTEGER = 2**53 - 1


class StateOwnershipError(ValueError):
    """Raised when a non-owner attempts to authorize or advance tenant state."""


@dataclass(frozen=True, slots=True)
class TurnInput:
    """Deterministic input for one decision round (one turn).

    Mirrors the arena.agent.io.v1 ``DecideMessage`` semantics without depending
    on the SDK: a round identity (``tick``), world observation (``projection``),
    and authoritative economic observation (``economy``). Tick, rules version,
    and controlled population must agree across both projections.
    """

    __canonical_name__ = "arena-hero.turn-input.v2"

    tick: int
    projection: WorldProjection
    economy: EconomyTurnInput

    def __post_init__(self) -> None:
        tick = _require_int("turn tick", self.tick)
        if tick < 0:
            raise ValueError("turn tick cannot be negative")
        if tick > _MAX_SAFE_INTEGER:
            raise ValueError("turn tick exceeds the cross-language safe-integer range")
        if not isinstance(self.projection, WorldProjection):
            raise TypeError("projection must be a WorldProjection")
        if not isinstance(self.economy, EconomyTurnInput):
            raise TypeError("economy must be an EconomyTurnInput")
        if self.projection.tick != tick:
            raise ValueError(
                f"turn tick {tick} does not match projection tick {self.projection.tick}"
            )
        if self.economy.tick != tick:
            raise ValueError(f"turn tick {tick} does not match economy tick {self.economy.tick}")
        if self.economy.rules_version is not self.projection.rules_version:
            raise ValueError("economy rules_version does not match projection rules_version")
        if self.economy.population != len(self.projection.units):
            raise ValueError("economy population does not match controlled unit count")


@dataclass(frozen=True, slots=True)
class TenantState:
    """Immutable authoritative domain state for one tenant partition."""

    __canonical_name__ = "arena-hero.tenant-state.v2"

    tenant_id: TenantId
    world: WorldProjection
    economy: EconomyState
    decision_count: int = 0
    last_decision_id: DecisionId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant_id must be a TenantId")
        if not isinstance(self.world, WorldProjection):
            raise TypeError("world must be a WorldProjection")
        if not isinstance(self.economy, EconomyState):
            raise TypeError("economy must be an EconomyState")
        if self.economy.tick != self.world.tick:
            raise ValueError("economy tick must match world tick")
        if self.economy.rules_version is not self.world.rules_version:
            raise ValueError("economy rules_version must match world rules_version")
        if self.economy.population != len(self.world.units):
            raise ValueError("economy population must match controlled unit count")
        decision_count = _require_int("decision_count", self.decision_count)
        if decision_count < 0:
            raise ValueError("decision_count cannot be negative")
        if decision_count > _MAX_SAFE_INTEGER:
            raise ValueError("decision_count exceeds the cross-language safe-integer range")
        if self.last_decision_id is not None and not isinstance(self.last_decision_id, DecisionId):
            raise TypeError("last_decision_id must be a DecisionId or None")

    @property
    def owner(self) -> TenantId:
        """Return the tenant partition that owns this state."""

        return self.tenant_id

    @property
    def state_digest(self) -> StateDigest:
        """Return the canonical semantic identity for this tenant state."""

        return StateDigest.from_state(self)

    @property
    def economy_input(self) -> EconomyDecisionInput:
        """Return the stable economic policy input for the current state."""

        return self.economy.decision_input(self.tenant_id)

    def require_owner(self, actor: TenantId) -> Self:
        """Fail closed unless the actor owns this tenant state.

        Returns the same state when authorized so callers can chain the guard
        with a reducer step; a non-owner actor raises ``StateOwnershipError``.
        """

        if not isinstance(actor, TenantId):
            raise TypeError("actor must be a TenantId")
        if actor != self.tenant_id:
            raise StateOwnershipError(
                f"actor {actor.value} does not own state for tenant {self.tenant_id.value}"
            )
        return self

    def observe(
        self,
        world: WorldProjection,
        economy: EconomyTurnInput,
        *,
        actor: TenantId | None = None,
    ) -> Self:
        """Fold newer world and economy observations without mutating state.

        The tenant partition and decision journal are preserved. The projection
        must use the current rules version and must not regress or contradict an
        observation already seen for its tick. When ``actor`` is declared it
        must own the state; otherwise the advance is treated as owner-authorized
        by construction. World and economy advance atomically.
        """

        if actor is not None:
            self.require_owner(actor)
        if not isinstance(world, WorldProjection):
            raise TypeError("world must be a WorldProjection")
        if not isinstance(economy, EconomyTurnInput):
            raise TypeError("economy must be an EconomyTurnInput")
        assert_current_rules_version(world.rules_version)
        if economy.tick != world.tick:
            raise ValueError("economy tick must match world tick")
        if economy.rules_version is not world.rules_version:
            raise ValueError("economy rules_version must match world rules_version")
        if world.tick < self.world.tick:
            raise ValueError(
                f"world tick {world.tick} regresses below current tick {self.world.tick}"
            )
        if world.tick == self.world.tick and world != self.world:
            raise ValueError(f"conflicting world observation for tick {world.tick}")
        next_economy = self.economy.advance(economy)
        if world == self.world and next_economy == self.economy:
            return self
        return type(self)(
            tenant_id=self.tenant_id,
            world=world,
            economy=next_economy,
            decision_count=self.decision_count,
            last_decision_id=self.last_decision_id,
        )

    def observe_turn(
        self,
        turn: TurnInput,
        *,
        actor: TenantId,
    ) -> Self:
        """Fold one complete world and economy observation without a decision commit."""

        if not isinstance(turn, TurnInput):
            raise TypeError("turn must be a TurnInput")
        self.require_owner(actor)
        return self.observe(turn.projection, turn.economy, actor=actor)

    def record_decision(
        self,
        decision_id: DecisionId,
        *,
        actor: TenantId | None = None,
    ) -> Self:
        """Commit one decision into the journal identity.

        Re-committing the current last decision is rejected; the arbiter treats
        it as a duplicate signal rather than silently extending the journal.
        When ``actor`` is declared it must own the state.
        """

        if actor is not None:
            self.require_owner(actor)
        if not isinstance(decision_id, DecisionId):
            raise TypeError("decision_id must be a DecisionId")
        if decision_id == self.last_decision_id:
            raise ValueError(f"duplicate decision commit for {decision_id}")
        return type(self)(
            tenant_id=self.tenant_id,
            world=self.world,
            economy=self.economy,
            decision_count=self.decision_count + 1,
            last_decision_id=decision_id,
        )

    def reduce_turn(
        self,
        turn: TurnInput,
        decision: DecisionId,
        *,
        actor: TenantId,
    ) -> Self:
        """Apply one round input plus a decision as a single pure state advance.

        The owning actor is mandatory and fails closed on cross-tenant or
        non-owner writes. The round input must carry the current rules version
        and world/economy projections that neither regress nor conflict with the
        state; the decision must not duplicate the committed journal tail.
        """

        if not isinstance(turn, TurnInput):
            raise TypeError("turn must be a TurnInput")
        if not isinstance(decision, DecisionId):
            raise TypeError("decision must be a DecisionId")
        self.require_owner(actor)
        observed = self.observe_turn(turn, actor=actor)
        return observed.record_decision(decision, actor=actor)
