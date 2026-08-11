"""In-memory snapshot and journal adapters for offline decision runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from arena_hero_agent.application.decision import DecisionJournalEntry
from arena_hero_agent.domain import Generation, StateDigest, TenantId, TenantState
from arena_hero_agent.ports import LeaseDisposition, WriterLeaseHandle


class MemoryTenantStateStore:
    """Tenant-partitioned compare-and-set state snapshots."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: dict[TenantId, tuple[Generation, StateDigest, TenantState]] = {}

    async def load(
        self,
        tenant_id: TenantId,
    ) -> tuple[Generation, StateDigest, TenantState] | None:
        async with self._lock:
            return self._states.get(tenant_id)

    async def compare_and_set(
        self,
        tenant_id: TenantId,
        *,
        expected_generation: Generation,
        next_generation: Generation,
        state_digest: StateDigest,
        state: TenantState,
        lease: WriterLeaseHandle,
    ) -> bool:
        if (
            lease.tenant_id != tenant_id
            or lease.generation != expected_generation
            or lease.disposition is not LeaseDisposition.ACTIVE
            or next_generation != expected_generation.next()
            or state.tenant_id != tenant_id
            or state.state_digest != state_digest
        ):
            return False
        async with self._lock:
            current = self._states.get(tenant_id)
            if current is not None and current[0] != expected_generation:
                return False
            if current is None and expected_generation != Generation(0):
                return False
            self._states[tenant_id] = (next_generation, state_digest, state)
            return True

    async def restore(
        self,
        tenant_id: TenantId,
        *,
        generation: Generation,
        state_digest: StateDigest,
        state: TenantState,
        lease: WriterLeaseHandle,
    ) -> bool:
        if (
            lease.tenant_id != tenant_id
            or lease.generation != generation
            or lease.disposition is not LeaseDisposition.ACTIVE
            or state.tenant_id != tenant_id
            or state.state_digest != state_digest
        ):
            return False
        async with self._lock:
            current = self._states.get(tenant_id)
            if current is not None and current[0] > generation:
                return False
            if current is not None and current[0] == generation:
                return current[1] == state_digest and current[2] == state
            self._states[tenant_id] = (generation, state_digest, state)
            return True


class MemoryDecisionJournal:
    """Append-only replay evidence guarded by the current writer lease."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[TenantId, list[DecisionJournalEntry]] = {}

    async def append(
        self,
        tenant_id: TenantId,
        *,
        generation: Generation,
        events: Sequence[DecisionJournalEntry],
        lease: WriterLeaseHandle,
    ) -> int:
        if not events:
            raise ValueError("journal append requires at least one event")
        if (
            lease.tenant_id != tenant_id
            or lease.generation != generation
            or lease.disposition is not LeaseDisposition.ACTIVE
        ):
            raise ValueError("writer lease is not current for journal append")
        async with self._lock:
            entries = self._entries.setdefault(tenant_id, [])
            current_generation = entries[-1].generation if entries else generation
            seen = {entry.decision_id for entry in entries}
            for event in events:
                if event.tenant_id != tenant_id:
                    raise ValueError("journal event crossed tenant partition")
                if event.previous_generation != current_generation:
                    raise ValueError("journal event generation is not contiguous")
                if event.decision_id in seen:
                    raise ValueError("journal contains a duplicate decision id")
                entries.append(event)
                current_generation = event.generation
                seen.add(event.decision_id)
            return len(entries) - 1

    async def _read_snapshot(
        self,
        tenant_id: TenantId,
        position: int,
    ) -> tuple[DecisionJournalEntry, ...]:
        if position < 0:
            raise ValueError("journal position cannot be negative")
        async with self._lock:
            return tuple(self._entries.get(tenant_id, ()))[position:]

    async def _iterate(
        self,
        tenant_id: TenantId,
        position: int,
    ) -> AsyncIterator[DecisionJournalEntry]:
        for entry in await self._read_snapshot(tenant_id, position):
            yield entry

    def read_from(
        self,
        tenant_id: TenantId,
        position: int,
    ) -> AsyncIterator[DecisionJournalEntry]:
        return self._iterate(tenant_id, position)
