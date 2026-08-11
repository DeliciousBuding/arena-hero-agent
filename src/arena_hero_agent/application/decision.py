"""Deterministic tenant decision arbitration over fenced leases and durable journal state."""

from __future__ import annotations

from dataclasses import dataclass, field

from arena_hero_agent.domain import (
    DeadlineBudget,
    DecisionId,
    Generation,
    StateDigest,
    TenantId,
    TenantState,
    TurnInput,
)
from arena_hero_agent.ports import (
    DecisionLease,
    DecisionLeaseHandle,
    EventJournal,
    LeaseDisposition,
    TenantStateStore,
    WriterLease,
    WriterLeaseHandle,
)

_INITIAL_GENERATION = Generation(0)


class DecisionArbitrationError(RuntimeError):
    """Base class for fail-closed decision arbitration failures."""


class DecisionLeaseUnavailable(DecisionArbitrationError):
    """Raised when either required purpose-specific lease cannot be acquired."""


class DuplicateDecisionError(DecisionArbitrationError):
    """Raised when a committed decision identity is submitted again."""


class StateRecoveryError(DecisionArbitrationError):
    """Raised when snapshot and journal evidence cannot be replayed safely."""


class StaleDecisionContextError(DecisionArbitrationError):
    """Raised when a ticket no longer describes the current tenant generation."""


class StateCommitConflict(DecisionArbitrationError):
    """Raised when the state snapshot CAS rejects a journaled decision."""


@dataclass(frozen=True, slots=True)
class DecisionContext:
    """Stable deterministic input seam consumed by later decision policy layers."""

    __canonical_name__ = "arena-hero.decision-context.v1"

    tenant_id: TenantId
    generation: Generation
    state_digest: StateDigest
    state: TenantState
    turn: TurnInput
    decision_id: DecisionId

    def __post_init__(self) -> None:
        self.state.require_owner(self.tenant_id)
        if self.state.state_digest != self.state_digest:
            raise ValueError("state_digest does not match state")
        self.state.observe(self.turn.projection, actor=self.tenant_id)


@dataclass(frozen=True, slots=True)
class DecisionJournalEntry:
    """Replayable evidence for one committed tenant decision."""

    __canonical_name__ = "arena-hero.decision-journal-entry.v1"

    tenant_id: TenantId
    previous_generation: Generation
    generation: Generation
    previous_state_digest: StateDigest
    state_digest: StateDigest
    turn: TurnInput
    decision_id: DecisionId
    state: TenantState

    def __post_init__(self) -> None:
        if self.generation != self.previous_generation.next():
            raise ValueError("journal generation must advance exactly once")
        self.state.require_owner(self.tenant_id)
        if self.state.state_digest != self.state_digest:
            raise ValueError("journal state_digest does not match state")
        if self.state.last_decision_id != self.decision_id:
            raise ValueError("journal state does not end with decision_id")


@dataclass(frozen=True, slots=True)
class CommittedDecision:
    """Durable result returned after journal append and snapshot CAS."""

    generation: Generation
    state_digest: StateDigest
    state: TenantState
    journal_position: int


@dataclass(slots=True)
class DecisionTicket:
    """Acquired decision authority; callers must commit or close it."""

    context: DecisionContext
    decision_lease: DecisionLeaseHandle
    writer_lease: WriterLeaseHandle
    _initial_state: TenantState = field(repr=False)
    _initial_generation: Generation = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.decision_lease.release()
        finally:
            await self.writer_lease.release()


@dataclass(frozen=True, slots=True)
class _RecoveredTenant:
    generation: Generation
    state_digest: StateDigest
    state: TenantState
    committed_decision_ids: frozenset[DecisionId]


class DecisionArbiter:
    """Serialize tenant decisions and replay authoritative state after restart."""

    def __init__(
        self,
        *,
        state_store: TenantStateStore[TenantState],
        journal: EventJournal[DecisionJournalEntry],
        decision_leases: DecisionLease,
        writer_leases: WriterLease,
    ) -> None:
        self._state_store = state_store
        self._journal = journal
        self._decision_leases = decision_leases
        self._writer_leases = writer_leases

    async def acquire(
        self,
        *,
        initial_state: TenantState,
        turn: TurnInput,
        decision_id: DecisionId,
        budget: DeadlineBudget,
        initial_generation: Generation = _INITIAL_GENERATION,
    ) -> DecisionTicket:
        """Acquire one deterministic decision ticket for the recovered tenant state."""

        tenant_id = initial_state.tenant_id
        recovered = await self._recover(initial_state, initial_generation)
        self._reject_duplicate(recovered, decision_id)
        recovered.state.observe(turn.projection, actor=tenant_id)

        writer = await self._writer_leases.acquire_writer(
            tenant_id,
            recovered.generation,
            budget,
        )
        if writer is None:
            raise DecisionLeaseUnavailable(f"writer lease unavailable for tenant {tenant_id.value}")

        decision = await self._decision_leases.acquire_decision(
            tenant_id,
            recovered.generation,
            decision_id,
            budget,
        )
        if decision is None:
            await writer.release()
            raise DecisionLeaseUnavailable(
                f"decision lease unavailable for tenant {tenant_id.value}"
            )

        context = DecisionContext(
            tenant_id=tenant_id,
            generation=recovered.generation,
            state_digest=recovered.state_digest,
            state=recovered.state,
            turn=turn,
            decision_id=decision_id,
        )
        ticket = DecisionTicket(
            context=context,
            decision_lease=decision,
            writer_lease=writer,
            _initial_state=initial_state,
            _initial_generation=initial_generation,
        )
        try:
            current = await self._recover(initial_state, initial_generation)
            if not self._same_revision(recovered, current):
                raise StaleDecisionContextError(
                    f"tenant {tenant_id.value} advanced while leases were acquired"
                )
            self._reject_duplicate(current, decision_id)
            if not await self._ensure_snapshot(current, writer):
                raise StaleDecisionContextError(
                    f"tenant {tenant_id.value} snapshot could not be restored"
                )
            return ticket
        except BaseException:
            await ticket.close()
            raise

    async def commit(
        self,
        ticket: DecisionTicket,
        *,
        budget: DeadlineBudget,
    ) -> CommittedDecision:
        """Journal and CAS one decision; every stale or duplicate path fails closed."""

        if ticket.closed:
            raise StaleDecisionContextError("decision ticket is already closed")
        context = ticket.context
        try:
            if (
                ticket.writer_lease.disposition is not LeaseDisposition.ACTIVE
                or ticket.decision_lease.disposition is not LeaseDisposition.ACTIVE
                or not await ticket.writer_lease.renew(budget)
                or not await ticket.decision_lease.renew(budget)
            ):
                raise StaleDecisionContextError(
                    f"lease is no longer current for tenant {context.tenant_id.value}"
                )

            current = await self._recover(ticket._initial_state, ticket._initial_generation)
            if (
                current.generation != context.generation
                or current.state_digest != context.state_digest
                or current.state != context.state
            ):
                raise StaleDecisionContextError(
                    f"tenant {context.tenant_id.value} advanced before commit"
                )
            self._reject_duplicate(current, context.decision_id)

            try:
                next_state = context.state.reduce_turn(
                    context.turn,
                    context.decision_id,
                    actor=context.tenant_id,
                )
            except ValueError as error:
                if "duplicate decision commit" in str(error):
                    raise DuplicateDecisionError(str(error)) from error
                raise

            next_generation = context.generation.next()
            entry = DecisionJournalEntry(
                tenant_id=context.tenant_id,
                previous_generation=context.generation,
                generation=next_generation,
                previous_state_digest=context.state_digest,
                state_digest=next_state.state_digest,
                turn=context.turn,
                decision_id=context.decision_id,
                state=next_state,
            )
            position = await self._journal.append(
                context.tenant_id,
                generation=context.generation,
                events=(entry,),
                lease=ticket.writer_lease,
            )
            stored = await self._state_store.compare_and_set(
                context.tenant_id,
                expected_generation=context.generation,
                next_generation=next_generation,
                state_digest=next_state.state_digest,
                state=next_state,
                lease=ticket.writer_lease,
            )
            if not stored:
                raise StateCommitConflict(
                    f"state CAS rejected decision {context.decision_id.value}"
                )
            return CommittedDecision(
                generation=next_generation,
                state_digest=next_state.state_digest,
                state=next_state,
                journal_position=position,
            )
        finally:
            await ticket.close()

    async def recover(
        self,
        initial_state: TenantState,
        initial_generation: Generation = _INITIAL_GENERATION,
    ) -> tuple[Generation, StateDigest, TenantState]:
        """Return the validated journal-derived tenant revision."""

        recovered = await self._recover(initial_state, initial_generation)
        return recovered.generation, recovered.state_digest, recovered.state

    async def _recover(
        self,
        initial_state: TenantState,
        initial_generation: Generation,
    ) -> _RecoveredTenant:
        tenant_id = initial_state.tenant_id
        generation = initial_generation
        state = initial_state
        state_digest = state.state_digest
        seen: set[DecisionId] = set()

        async for entry in self._journal.read_from(tenant_id, 0):
            if entry.tenant_id != tenant_id:
                raise StateRecoveryError("journal entry crossed tenant partition")
            if entry.previous_generation != generation or entry.generation != generation.next():
                raise StateRecoveryError("journal generation sequence is not contiguous")
            if entry.previous_state_digest != state_digest:
                raise StateRecoveryError(
                    "journal previous_state_digest does not match replay state"
                )
            if entry.decision_id in seen:
                raise StateRecoveryError("journal contains a duplicate decision id")
            try:
                replayed = state.reduce_turn(entry.turn, entry.decision_id, actor=tenant_id)
            except (TypeError, ValueError) as error:
                raise StateRecoveryError("journal reducer replay failed") from error
            if replayed != entry.state or replayed.state_digest != entry.state_digest:
                raise StateRecoveryError(
                    "journal entry does not match deterministic reducer replay"
                )
            generation = entry.generation
            state = replayed
            state_digest = replayed.state_digest
            seen.add(entry.decision_id)

        stored = await self._state_store.load(tenant_id)
        if stored is not None:
            stored_generation, stored_digest, stored_state = stored
            try:
                stored_state.require_owner(tenant_id)
            except (TypeError, ValueError) as error:
                raise StateRecoveryError("stored state crossed tenant partition") from error
            if stored_state.state_digest != stored_digest:
                raise StateRecoveryError("stored state digest does not match state")
            if stored_generation > generation:
                raise StateRecoveryError("state snapshot is ahead of the authoritative journal")
            if stored_generation == generation and (
                stored_digest != state_digest or stored_state != state
            ):
                raise StateRecoveryError("state snapshot conflicts with the journal revision")

        return _RecoveredTenant(
            generation=generation,
            state_digest=state_digest,
            state=state,
            committed_decision_ids=frozenset(seen),
        )

    async def _ensure_snapshot(
        self,
        recovered: _RecoveredTenant,
        lease: WriterLeaseHandle,
    ) -> bool:
        stored = await self._state_store.load(recovered.state.tenant_id)
        if stored is not None and stored[0] == recovered.generation:
            return stored[1] == recovered.state_digest and stored[2] == recovered.state
        return await self._state_store.restore(
            recovered.state.tenant_id,
            generation=recovered.generation,
            state_digest=recovered.state_digest,
            state=recovered.state,
            lease=lease,
        )

    @staticmethod
    def _same_revision(first: _RecoveredTenant, second: _RecoveredTenant) -> bool:
        return (
            first.generation == second.generation
            and first.state_digest == second.state_digest
            and first.state == second.state
        )

    @staticmethod
    def _reject_duplicate(recovered: _RecoveredTenant, decision_id: DecisionId) -> None:
        if decision_id not in recovered.committed_decision_ids:
            return
        if recovered.state.last_decision_id == decision_id:
            try:
                recovered.state.record_decision(decision_id, actor=recovered.state.tenant_id)
            except ValueError as error:
                raise DuplicateDecisionError(str(error)) from error
        raise DuplicateDecisionError(f"duplicate decision commit for {decision_id.value}")
