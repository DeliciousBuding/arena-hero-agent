"""In-process fenced leases for offline runtimes and deterministic tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from arena_hero_agent.domain import (
    DeadlineBudget,
    DecisionId,
    FencingToken,
    Generation,
    TenantId,
)
from arena_hero_agent.ports.clock import Clock
from arena_hero_agent.ports.leases import LeaseDisposition


@dataclass(slots=True)
class _LeaseSlot:
    next_fence: int = 1
    current: _MemoryLeaseHandle | None = None


class _MemoryLeaseHandle:
    def __init__(
        self,
        coordinator: MemoryLeaseCoordinator,
        *,
        kind: str,
        tenant_id: TenantId,
        fencing_token: FencingToken,
        expires_at_ns: int,
    ) -> None:
        self._coordinator = coordinator
        self._kind = kind
        self._tenant_id = tenant_id
        self._fencing_token = fencing_token
        self._expires_at_ns = expires_at_ns
        self._disposition = LeaseDisposition.ACTIVE

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    @property
    def fencing_token(self) -> FencingToken:
        return self._fencing_token

    @property
    def disposition(self) -> LeaseDisposition:
        return self._coordinator._disposition(self)

    async def renew(self, budget: DeadlineBudget) -> bool:
        return await self._coordinator._renew(self, budget)

    async def release(self) -> None:
        await self._coordinator._release(self)


class MemoryDecisionLeaseHandle(_MemoryLeaseHandle):
    """Decision handle issued by :class:`MemoryLeaseCoordinator`."""

    def __init__(
        self,
        coordinator: MemoryLeaseCoordinator,
        *,
        tenant_id: TenantId,
        generation: Generation,
        decision_id: DecisionId,
        fencing_token: FencingToken,
        expires_at_ns: int,
    ) -> None:
        super().__init__(
            coordinator,
            kind="decision",
            tenant_id=tenant_id,
            fencing_token=fencing_token,
            expires_at_ns=expires_at_ns,
        )
        self._generation = generation
        self._decision_id = decision_id

    @property
    def decision_id(self) -> DecisionId:
        return self._decision_id


class MemoryWriterLeaseHandle(_MemoryLeaseHandle):
    """Writer handle issued by :class:`MemoryLeaseCoordinator`."""

    def __init__(
        self,
        coordinator: MemoryLeaseCoordinator,
        *,
        tenant_id: TenantId,
        generation: Generation,
        fencing_token: FencingToken,
        expires_at_ns: int,
    ) -> None:
        super().__init__(
            coordinator,
            kind="writer",
            tenant_id=tenant_id,
            fencing_token=fencing_token,
            expires_at_ns=expires_at_ns,
        )
        self._generation = generation

    @property
    def generation(self) -> Generation:
        return self._generation


class MemoryLeaseCoordinator:
    """Tenant-partitioned decision and writer leases with explicit stale replacement.

    A normal acquisition never steals an expired lease. Replacement requires two
    independent facts: the current holder is past its monotonic expiry and the caller
    presents the exact observed fencing token. A successful replacement increments the
    fence and marks the previous handle as ``REPLACED``.
    """

    def __init__(self, clock: Clock, *, lease_duration_ns: int) -> None:
        if lease_duration_ns < 1:
            raise ValueError("lease_duration_ns must be positive")
        self._clock = clock
        self._lease_duration_ns = lease_duration_ns
        self._lock = asyncio.Lock()
        self._decision_slots: dict[TenantId, _LeaseSlot] = {}
        self._writer_slots: dict[TenantId, _LeaseSlot] = {}

    async def acquire_decision(
        self,
        tenant_id: TenantId,
        generation: Generation,
        decision_id: DecisionId,
        budget: DeadlineBudget,
    ) -> MemoryDecisionLeaseHandle | None:
        if budget.exhausted:
            return None
        async with self._lock:
            slot = self._decision_slots.setdefault(tenant_id, _LeaseSlot())
            if slot.current is not None:
                return None
            return self._issue_decision(slot, tenant_id, generation, decision_id)

    async def replace_decision(
        self,
        tenant_id: TenantId,
        generation: Generation,
        decision_id: DecisionId,
        *,
        expected_fencing_token: FencingToken,
        budget: DeadlineBudget,
    ) -> MemoryDecisionLeaseHandle | None:
        if budget.exhausted:
            return None
        async with self._lock:
            slot = self._decision_slots.setdefault(tenant_id, _LeaseSlot())
            current = slot.current
            if not self._replacement_is_authorized(current, expected_fencing_token):
                return None
            assert current is not None
            current._disposition = LeaseDisposition.REPLACED
            return self._issue_decision(slot, tenant_id, generation, decision_id)

    async def acquire_writer(
        self,
        tenant_id: TenantId,
        generation: Generation,
        budget: DeadlineBudget,
    ) -> MemoryWriterLeaseHandle | None:
        if budget.exhausted:
            return None
        async with self._lock:
            slot = self._writer_slots.setdefault(tenant_id, _LeaseSlot())
            if slot.current is not None:
                return None
            return self._issue_writer(slot, tenant_id, generation)

    async def replace_writer(
        self,
        tenant_id: TenantId,
        generation: Generation,
        *,
        expected_fencing_token: FencingToken,
        budget: DeadlineBudget,
    ) -> MemoryWriterLeaseHandle | None:
        if budget.exhausted:
            return None
        async with self._lock:
            slot = self._writer_slots.setdefault(tenant_id, _LeaseSlot())
            current = slot.current
            if not self._replacement_is_authorized(current, expected_fencing_token):
                return None
            assert current is not None
            current._disposition = LeaseDisposition.REPLACED
            return self._issue_writer(slot, tenant_id, generation)

    def _replacement_is_authorized(
        self,
        current: _MemoryLeaseHandle | None,
        expected_fencing_token: FencingToken,
    ) -> bool:
        return (
            current is not None
            and current.fencing_token == expected_fencing_token
            and self._clock.monotonic_ns() >= current._expires_at_ns
        )

    def _issue_decision(
        self,
        slot: _LeaseSlot,
        tenant_id: TenantId,
        generation: Generation,
        decision_id: DecisionId,
    ) -> MemoryDecisionLeaseHandle:
        handle = MemoryDecisionLeaseHandle(
            self,
            tenant_id=tenant_id,
            generation=generation,
            decision_id=decision_id,
            fencing_token=FencingToken(slot.next_fence),
            expires_at_ns=self._clock.monotonic_ns() + self._lease_duration_ns,
        )
        slot.next_fence += 1
        slot.current = handle
        return handle

    def _issue_writer(
        self,
        slot: _LeaseSlot,
        tenant_id: TenantId,
        generation: Generation,
    ) -> MemoryWriterLeaseHandle:
        handle = MemoryWriterLeaseHandle(
            self,
            tenant_id=tenant_id,
            generation=generation,
            fencing_token=FencingToken(slot.next_fence),
            expires_at_ns=self._clock.monotonic_ns() + self._lease_duration_ns,
        )
        slot.next_fence += 1
        slot.current = handle
        return handle

    def _slot_for(self, handle: _MemoryLeaseHandle) -> _LeaseSlot:
        slots = self._decision_slots if handle._kind == "decision" else self._writer_slots
        return slots[handle.tenant_id]

    def _disposition(self, handle: _MemoryLeaseHandle) -> LeaseDisposition:
        if handle._disposition is not LeaseDisposition.ACTIVE:
            return handle._disposition
        if self._clock.monotonic_ns() >= handle._expires_at_ns:
            return LeaseDisposition.EXPIRED
        return LeaseDisposition.ACTIVE

    async def _renew(self, handle: _MemoryLeaseHandle, budget: DeadlineBudget) -> bool:
        if budget.exhausted:
            return False
        async with self._lock:
            slot = self._slot_for(handle)
            if slot.current is not handle:
                handle._disposition = LeaseDisposition.REPLACED
                return False
            if self._clock.monotonic_ns() >= handle._expires_at_ns:
                return False
            handle._expires_at_ns = self._clock.monotonic_ns() + self._lease_duration_ns
            return True

    async def _release(self, handle: _MemoryLeaseHandle) -> None:
        async with self._lock:
            slot = self._slot_for(handle)
            if slot.current is handle:
                slot.current = None
                handle._disposition = LeaseDisposition.RELEASED
