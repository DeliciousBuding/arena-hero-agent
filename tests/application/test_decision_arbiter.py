from __future__ import annotations

import asyncio

import pytest

from arena_hero_agent.adapters.runtime import (
    MemoryDecisionJournal,
    MemoryLeaseCoordinator,
    MemoryTenantStateStore,
)
from arena_hero_agent.application.decision import (
    DecisionArbiter,
    DecisionContext,
    DecisionLeaseUnavailable,
    DuplicateDecisionError,
    StaleDecisionContextError,
)
from arena_hero_agent.domain import (
    DeadlineBudget,
    DecisionId,
    Generation,
    RulesVersion,
    TenantId,
    TenantState,
    TurnInput,
    WorldProjection,
    canonical_sha256,
)


class ManualClock:
    def __init__(self) -> None:
        self.now = 0

    def monotonic_ns(self) -> int:
        return self.now

    def advance(self, nanoseconds: int) -> None:
        self.now += nanoseconds


def _world(tick: int) -> WorldProjection:
    return WorldProjection(tick=tick, rules_version=RulesVersion.V0_14)


def _initial(tenant: str) -> TenantState:
    return TenantState(tenant_id=TenantId(tenant), world=_world(0))


def _turn(tick: int) -> TurnInput:
    return TurnInput(tick=tick, projection=_world(tick))


def _arbiter(
    clock: ManualClock,
    store: MemoryTenantStateStore,
    journal: MemoryDecisionJournal,
) -> DecisionArbiter:
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=1_000)
    return DecisionArbiter(
        state_store=store,
        journal=journal,
        decision_leases=leases,
        writer_leases=leases,
    )


async def test_two_arbiters_compete_and_only_one_gets_a_ticket() -> None:
    clock = ManualClock()
    store = MemoryTenantStateStore()
    journal = MemoryDecisionJournal()
    arbiter = _arbiter(clock, store, journal)
    initial = _initial("sample")
    budget = DeadlineBudget(1)

    results = await asyncio.gather(
        arbiter.acquire(
            initial_state=initial,
            turn=_turn(1),
            decision_id=DecisionId("decision:first"),
            budget=budget,
        ),
        arbiter.acquire(
            initial_state=initial,
            turn=_turn(1),
            decision_id=DecisionId("decision:second"),
            budget=budget,
        ),
        return_exceptions=True,
    )

    tickets = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, DecisionLeaseUnavailable)]
    assert len(tickets) == 1
    assert len(failures) == 1
    await tickets[0].close()


async def test_restart_recovers_state_from_journal_without_snapshot() -> None:
    clock = ManualClock()
    journal = MemoryDecisionJournal()
    first_store = MemoryTenantStateStore()
    first = _arbiter(clock, first_store, journal)
    initial = _initial("sample")
    ticket = await first.acquire(
        initial_state=initial,
        turn=_turn(1),
        decision_id=DecisionId("decision:one"),
        budget=DeadlineBudget(1),
    )
    committed = await first.commit(ticket, budget=DeadlineBudget(1))
    assert committed.generation == Generation(1)

    restarted = _arbiter(clock, MemoryTenantStateStore(), journal)
    generation, digest, state = await restarted.recover(initial)

    assert generation == Generation(1)
    assert digest == committed.state_digest
    assert state == committed.state
    assert state.decision_count == 1

    next_ticket = await restarted.acquire(
        initial_state=initial,
        turn=_turn(2),
        decision_id=DecisionId("decision:two"),
        budget=DeadlineBudget(1),
    )
    next_commit = await restarted.commit(next_ticket, budget=DeadlineBudget(1))
    assert next_commit.generation == Generation(2)
    assert next_commit.state.decision_count == 2


async def test_committed_decision_id_is_rejected_fail_closed_after_restart() -> None:
    clock = ManualClock()
    store = MemoryTenantStateStore()
    journal = MemoryDecisionJournal()
    initial = _initial("sample")
    decision_id = DecisionId("decision:duplicate")
    first = _arbiter(clock, store, journal)
    ticket = await first.acquire(
        initial_state=initial,
        turn=_turn(1),
        decision_id=decision_id,
        budget=DeadlineBudget(1),
    )
    await first.commit(ticket, budget=DeadlineBudget(1))

    restarted = _arbiter(clock, store, journal)
    with pytest.raises(DuplicateDecisionError, match="duplicate decision commit"):
        await restarted.acquire(
            initial_state=initial,
            turn=_turn(1),
            decision_id=decision_id,
            budget=DeadlineBudget(1),
        )


async def test_different_tenants_progress_independently() -> None:
    clock = ManualClock()
    store = MemoryTenantStateStore()
    journal = MemoryDecisionJournal()
    arbiter = _arbiter(clock, store, journal)
    budget = DeadlineBudget(1)

    first, second = await asyncio.gather(
        arbiter.acquire(
            initial_state=_initial("t1"),
            turn=_turn(1),
            decision_id=DecisionId("decision:t1"),
            budget=budget,
        ),
        arbiter.acquire(
            initial_state=_initial("t2"),
            turn=_turn(1),
            decision_id=DecisionId("decision:t2"),
            budget=budget,
        ),
    )
    committed_first, committed_second = await asyncio.gather(
        arbiter.commit(first, budget=budget),
        arbiter.commit(second, budget=budget),
    )

    assert committed_first.state.tenant_id == TenantId("t1")
    assert committed_second.state.tenant_id == TenantId("t2")
    assert committed_first.generation == committed_second.generation == Generation(1)


async def test_expired_ticket_cannot_commit() -> None:
    clock = ManualClock()
    store = MemoryTenantStateStore()
    journal = MemoryDecisionJournal()
    arbiter = _arbiter(clock, store, journal)
    ticket = await arbiter.acquire(
        initial_state=_initial("sample"),
        turn=_turn(1),
        decision_id=DecisionId("decision:expired"),
        budget=DeadlineBudget(1),
    )

    clock.advance(1_000)
    with pytest.raises(StaleDecisionContextError, match="no longer current"):
        await arbiter.commit(ticket, budget=DeadlineBudget(1))
    assert ticket.closed


async def test_decision_context_is_reproducible_policy_input() -> None:
    clock = ManualClock()
    store = MemoryTenantStateStore()
    journal = MemoryDecisionJournal()
    arbiter = _arbiter(clock, store, journal)
    initial = _initial("sample")
    turn = _turn(1)
    decision_id = DecisionId("decision:stable")

    first = await arbiter.acquire(
        initial_state=initial,
        turn=turn,
        decision_id=decision_id,
        budget=DeadlineBudget(1),
    )
    first_hash = canonical_sha256(first.context)
    await first.close()
    second = await arbiter.acquire(
        initial_state=initial,
        turn=turn,
        decision_id=decision_id,
        budget=DeadlineBudget(1),
    )

    assert isinstance(second.context, DecisionContext)
    assert canonical_sha256(second.context) == first_hash
    await second.close()
