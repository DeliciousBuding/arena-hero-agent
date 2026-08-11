from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arena_hero_agent.adapters.runtime import FileCommandBus, MemoryLeaseCoordinator
from arena_hero_agent.alliance.applier import (
    CommandContext,
    CommandLeaseUnavailable,
    TenantCommandApplier,
)
from arena_hero_agent.alliance.commands import (
    COMMAND_SCHEMA_VERSION,
    CommandAction,
    CommandDisposition,
    CommandIssuer,
    DirectorCommand,
)
from arena_hero_agent.domain import CommandId, Coordinate, DeadlineBudget, Generation, TenantId

TENANT = TenantId("t1")
GENERATION = Generation(3)
BUDGET = DeadlineBudget.from_milliseconds(1000)


class ManualTick:
    def __init__(self, tick: int = 0) -> None:
        self.tick = tick

    def __call__(self) -> int:
        return self.tick

    def advance(self, delta: int) -> None:
        self.tick += delta


class MonotonicClock:
    def __init__(self) -> None:
        self.now = 0

    def monotonic_ns(self) -> int:
        return self.now


class SequenceTick:
    def __init__(self, values: list[int]) -> None:
        self._values = iter(values)

    def __call__(self) -> int:
        return next(self._values)


def command(
    *,
    command_id: str = "cmd:attack-1",
    issuer: CommandIssuer = CommandIssuer.DIRECTOR,
    issued_at_tick: int = 10,
    expires_at_tick: int = 40,
    expected_generation: Generation = GENERATION,
    idempotency_key: str | None = None,
) -> DirectorCommand:
    return DirectorCommand(
        schema_version=COMMAND_SCHEMA_VERSION,
        command_id=CommandId(command_id),
        tenant_id=TENANT,
        issuer=issuer,
        issued_at_tick=issued_at_tick,
        expires_at_tick=expires_at_tick,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key or f"key-{command_id}",
        action=CommandAction(kind="squad_attack", target=Coordinate(4, 5)),
    )


async def test_drain_applies_valid_commands_in_issue_order(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    first = command(command_id="cmd:a")
    second = command(command_id="cmd:b")
    await bus.publish(first)
    await bus.publish(second)
    tick = ManualTick(20)
    leases = MemoryLeaseCoordinator(MonotonicClock(), lease_duration_ns=100)
    apply = TenantCommandApplier(ledger=bus, audit=bus, writer_leases=leases, now=tick)

    results = await apply.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)

    assert [result.disposition for result in results] == [
        CommandDisposition.APPLIED,
        CommandDisposition.APPLIED,
    ]
    assert results[0].command == first
    assert results[1].command == second
    assert await bus.is_applied(TENANT, command=first)
    assert await bus.is_applied(TENANT, command=second)
    audit = bus.read_audit(TENANT)
    assert [event.disposition for event in audit] == [
        CommandDisposition.APPLIED,
        CommandDisposition.APPLIED,
    ]
    assert audit[0].tick == 20
    assert audit[0].generation == GENERATION


async def test_drain_applies_only_valid_commands_fail_closed(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    good = command(command_id="cmd:good", expires_at_tick=100)
    stale = command(
        command_id="cmd:stale",
        expected_generation=Generation(99),
        expires_at_tick=100,
    )
    expired = command(command_id="cmd:expired", expires_at_tick=40)
    unauthorized = command(command_id="cmd:unauthorized", issuer=CommandIssuer.AGENT)
    for item in (good, stale, expired, unauthorized):
        await bus.publish(item)
    tick = ManualTick(50)
    leases = MemoryLeaseCoordinator(MonotonicClock(), lease_duration_ns=100)
    apply = TenantCommandApplier(ledger=bus, audit=bus, writer_leases=leases, now=tick)

    results = await apply.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)

    assert [result.disposition for result in results] == [
        CommandDisposition.APPLIED,
        CommandDisposition.REJECTED,
        CommandDisposition.EXPIRED,
        CommandDisposition.UNAUTHORIZED,
    ]
    assert await bus.is_applied(TENANT, command=good)
    assert not await bus.is_applied(TENANT, command=stale)
    assert not await bus.is_applied(TENANT, command=expired)
    assert not await bus.is_applied(TENANT, command=unauthorized)
    audit = bus.read_audit(TENANT)
    assert [event.disposition for event in audit] == [
        CommandDisposition.APPLIED,
        CommandDisposition.REJECTED,
        CommandDisposition.EXPIRED,
        CommandDisposition.UNAUTHORIZED,
    ]


async def test_duplicate_delivery_applies_once_and_audits_duplicate(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    cmd = command(command_id="cmd:dup")
    await bus.publish(cmd)
    await bus.publish(cmd)
    tick = ManualTick(20)
    leases = MemoryLeaseCoordinator(MonotonicClock(), lease_duration_ns=100)
    apply = TenantCommandApplier(ledger=bus, audit=bus, writer_leases=leases, now=tick)

    results = await apply.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)

    assert [result.disposition for result in results] == [
        CommandDisposition.APPLIED,
        CommandDisposition.DUPLICATE,
    ]
    assert await bus.is_applied(TENANT, command=cmd)
    audit = bus.read_audit(TENANT)
    assert [event.disposition for event in audit] == [
        CommandDisposition.APPLIED,
        CommandDisposition.DUPLICATE,
    ]


async def test_expiry_race_fails_closed_under_lease(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    cmd = command(command_id="cmd:race", expires_at_tick=40)
    await bus.publish(cmd)
    leases = MemoryLeaseCoordinator(MonotonicClock(), lease_duration_ns=100)
    # The pre-check observes tick 20 (valid); the authoritative re-check under
    # the writer lease observes tick 60 (already expired).
    apply = TenantCommandApplier(
        ledger=bus,
        audit=bus,
        writer_leases=leases,
        now=SequenceTick([20, 60]),
    )

    results = await apply.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)

    assert len(results) == 1
    assert results[0].disposition is CommandDisposition.EXPIRED
    assert not await bus.is_applied(TENANT, command=cmd)
    audit = bus.read_audit(TENANT)
    assert len(audit) == 1
    assert audit[0].disposition is CommandDisposition.EXPIRED
    assert audit[0].tick == 60


async def test_drain_fails_closed_when_writer_lease_is_unavailable(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    await bus.publish(command(command_id="cmd:contended"))
    leases = MemoryLeaseCoordinator(MonotonicClock(), lease_duration_ns=100)
    holder = await leases.acquire_writer(TENANT, GENERATION, BUDGET)
    assert holder is not None
    tick = ManualTick(20)
    apply = TenantCommandApplier(ledger=bus, audit=bus, writer_leases=leases, now=tick)

    with pytest.raises(CommandLeaseUnavailable):
        await apply.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)

    assert not await bus.is_applied(TENANT, command=command(command_id="cmd:contended"))
    assert bus.read_audit(TENANT) == []
    await holder.release()

    results = await apply.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)
    assert [result.disposition for result in results] == [CommandDisposition.APPLIED]


async def test_concurrent_drains_never_double_apply(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    cmd = command(command_id="cmd:concurrent")
    await bus.publish(cmd)
    leases = MemoryLeaseCoordinator(MonotonicClock(), lease_duration_ns=100)
    tick = ManualTick(20)
    first = TenantCommandApplier(ledger=bus, audit=bus, writer_leases=leases, now=tick)
    second = TenantCommandApplier(ledger=bus, audit=bus, writer_leases=leases, now=tick)

    outcomes = await asyncio.gather(
        first.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET),
        second.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET),
        return_exceptions=True,
    )

    # Exactly one writer applies the command. The loser fails closed in one of
    # three equivalent ways depending on interleaving: it raised because the
    # writer lease was taken, it observed the applied marker (DUPLICATE), or it
    # read an empty pending set after the winner finished. No double apply.
    applied = [
        result
        for outcome in outcomes
        if isinstance(outcome, tuple)
        for result in outcome
        if result.disposition is CommandDisposition.APPLIED
    ]
    assert len(applied) == 1
    duplicates = [
        result
        for outcome in outcomes
        if isinstance(outcome, tuple)
        for result in outcome
        if result.disposition is CommandDisposition.DUPLICATE
    ]
    assert len(duplicates) <= 1
    exceptions = [outcome for outcome in outcomes if isinstance(outcome, CommandLeaseUnavailable)]
    assert len(exceptions) <= 1
    assert await bus.is_applied(TENANT, command=cmd)
    audit = bus.read_audit(TENANT)
    assert [event.disposition for event in audit] == [CommandDisposition.APPLIED]


async def test_restart_recovery_catches_up_without_double_apply(tmp_path: Path) -> None:
    bus_a = FileCommandBus(tmp_path)
    first = command(command_id="cmd:first")
    second = command(command_id="cmd:second")
    await bus_a.publish(first)
    await bus_a.publish(second)
    tick_a = ManualTick(20)
    leases_a = MemoryLeaseCoordinator(MonotonicClock(), lease_duration_ns=100)
    applier_a = TenantCommandApplier(ledger=bus_a, audit=bus_a, writer_leases=leases_a, now=tick_a)

    results_a = await applier_a.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)
    assert [result.disposition for result in results_a] == [
        CommandDisposition.APPLIED,
        CommandDisposition.APPLIED,
    ]

    # A third command arrives after the first instance stopped.
    third = command(command_id="cmd:third")
    await FileCommandBus(tmp_path).publish(third)

    # Restart: fresh bus, fresh lease coordinator, fresh applier over the same root.
    bus_b = FileCommandBus(tmp_path)
    leases_b = MemoryLeaseCoordinator(MonotonicClock(), lease_duration_ns=100)
    tick_b = ManualTick(21)
    applier_b = TenantCommandApplier(ledger=bus_b, audit=bus_b, writer_leases=leases_b, now=tick_b)
    results_b = await applier_b.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)

    assert [result.disposition for result in results_b] == [CommandDisposition.APPLIED]
    assert results_b[0].command == third
    assert await bus_b.is_applied(TENANT, command=first)
    assert await bus_b.is_applied(TENANT, command=second)
    assert await bus_b.is_applied(TENANT, command=third)
    audit = bus_b.read_audit(TENANT)
    assert [event.disposition for event in audit] == [
        CommandDisposition.APPLIED,
        CommandDisposition.APPLIED,
        CommandDisposition.APPLIED,
    ]


async def test_empty_drain_does_not_touch_the_writer_lease(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    leases = MemoryLeaseCoordinator(MonotonicClock(), lease_duration_ns=100)
    apply = TenantCommandApplier(ledger=bus, audit=bus, writer_leases=leases, now=ManualTick(20))

    results = await apply.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)

    assert results == ()
    assert bus.read_audit(TENANT) == []
