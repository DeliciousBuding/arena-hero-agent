"""Persistence and audit boundaries owned by the application."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

from arena_hero_agent.domain import DecisionId, Generation, StateDigest, TenantId
from arena_hero_agent.ports.leases import DecisionLeaseHandle, WriterLeaseHandle

if TYPE_CHECKING:
    from arena_hero import Accepted, CommandPlan

StateT = TypeVar("StateT")
EventT = TypeVar("EventT")


@runtime_checkable
class TenantStateStore(Protocol[StateT]):
    """Compare-and-set storage for one tenant partition."""

    async def load(
        self,
        tenant_id: TenantId,
    ) -> tuple[Generation, StateDigest, StateT] | None: ...

    async def compare_and_set(
        self,
        tenant_id: TenantId,
        *,
        expected_generation: Generation,
        next_generation: Generation,
        state_digest: StateDigest,
        state: StateT,
        lease: WriterLeaseHandle,
    ) -> bool:
        """Persist only when generation and writer fence are still current."""
        ...

    async def restore(
        self,
        tenant_id: TenantId,
        *,
        generation: Generation,
        state_digest: StateDigest,
        state: StateT,
        lease: WriterLeaseHandle,
    ) -> bool:
        """Restore a missing or older snapshot from validated journal evidence."""
        ...


@runtime_checkable
class EventJournal(Protocol[EventT]):
    """Append-only tenant event journal guarded by a writer lease."""

    async def append(
        self,
        tenant_id: TenantId,
        *,
        generation: Generation,
        events: Sequence[EventT],
        lease: WriterLeaseHandle,
    ) -> int:
        """Append events and return the last durable zero-based position."""
        ...

    def read_from(self, tenant_id: TenantId, position: int) -> AsyncIterator[EventT]:
        """Read durable events in journal order starting at a position."""
        ...


@runtime_checkable
class DecisionRecorder(Protocol):
    """Record deterministic inputs, SDK-owned output, and acknowledgement evidence."""

    async def record(
        self,
        tenant_id: TenantId,
        *,
        decision_id: DecisionId,
        generation: Generation,
        state_digest: StateDigest,
        plan: CommandPlan,
        accepted: Accepted | None,
        lease: DecisionLeaseHandle,
    ) -> None: ...
