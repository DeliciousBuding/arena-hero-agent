from __future__ import annotations

import asyncio

from arena_hero_agent.adapters.runtime import MemoryLeaseCoordinator
from arena_hero_agent.domain import DeadlineBudget, DecisionId, FencingToken, Generation, TenantId
from arena_hero_agent.ports import LeaseDisposition


class ManualClock:
    def __init__(self) -> None:
        self.now = 0

    def monotonic_ns(self) -> int:
        return self.now

    def advance(self, nanoseconds: int) -> None:
        self.now += nanoseconds


async def test_two_writer_contenders_get_exactly_one_holder() -> None:
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    tenant = TenantId("sample")
    generation = Generation(3)
    budget = DeadlineBudget(1)

    first, second = await asyncio.gather(
        leases.acquire_writer(tenant, generation, budget),
        leases.acquire_writer(tenant, generation, budget),
    )

    assert (first is None) != (second is None)
    winner = first if first is not None else second
    assert winner is not None
    assert winner.disposition is LeaseDisposition.ACTIVE


async def test_different_generation_cannot_bypass_active_writer() -> None:
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    tenant = TenantId("sample")
    budget = DeadlineBudget(1)

    current = await leases.acquire_writer(tenant, Generation(4), budget)
    contender = await leases.acquire_writer(tenant, Generation(5), budget)

    assert current is not None
    assert contender is None
    assert current.disposition is LeaseDisposition.ACTIVE


async def test_stale_replacement_requires_expiry_and_exact_fence_evidence() -> None:
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    tenant = TenantId("sample")
    budget = DeadlineBudget(1)
    old = await leases.acquire_writer(tenant, Generation(4), budget)
    assert old is not None

    too_early = await leases.replace_writer(
        tenant,
        Generation(5),
        expected_fencing_token=old.fencing_token,
        budget=budget,
    )
    assert too_early is None

    clock.advance(100)
    wrong_fence = await leases.replace_writer(
        tenant,
        Generation(5),
        expected_fencing_token=FencingToken(old.fencing_token.value + 1),
        budget=budget,
    )
    assert wrong_fence is None
    assert old.disposition is LeaseDisposition.EXPIRED

    replacement = await leases.replace_writer(
        tenant,
        Generation(5),
        expected_fencing_token=old.fencing_token,
        budget=budget,
    )
    assert replacement is not None
    assert replacement.fencing_token.supersedes(old.fencing_token)
    assert replacement.generation == Generation(5)
    assert old.disposition is LeaseDisposition.REPLACED
    assert not await old.renew(budget)


async def test_decision_replacement_marks_old_holder_and_preserves_identity() -> None:
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=10)
    tenant = TenantId("sample")
    budget = DeadlineBudget(1)
    old = await leases.acquire_decision(
        tenant,
        Generation(2),
        DecisionId("decision:old"),
        budget,
    )
    assert old is not None

    clock.advance(10)
    replacement = await leases.replace_decision(
        tenant,
        Generation(3),
        DecisionId("decision:new"),
        expected_fencing_token=old.fencing_token,
        budget=budget,
    )

    assert replacement is not None
    assert old.disposition is LeaseDisposition.REPLACED
    assert replacement.decision_id == DecisionId("decision:new")


async def test_tenant_partitions_acquire_independently() -> None:
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    budget = DeadlineBudget(1)

    first, second = await asyncio.gather(
        leases.acquire_writer(TenantId("t1"), Generation(1), budget),
        leases.acquire_writer(TenantId("t2"), Generation(1), budget),
    )

    assert first is not None
    assert second is not None
    assert first.tenant_id != second.tenant_id
