from __future__ import annotations

import json
from pathlib import Path

import pytest

from arena_hero_agent.adapters.runtime import FileCommandBus, FileWriterLeaseCoordinator
from arena_hero_agent.alliance.applier import CommandContext, TenantCommandApplier
from arena_hero_agent.alliance.commands import (
    COMMAND_SCHEMA_VERSION,
    CommandAction,
    CommandAuditEvent,
    CommandBusError,
    CommandDisposition,
    CommandIssuer,
    DirectorCommand,
)
from arena_hero_agent.domain import (
    CommandId,
    Coordinate,
    DeadlineBudget,
    FencingToken,
    Generation,
    TenantId,
)
from arena_hero_agent.ports import CommandAudit, CommandBus, CommandLedger, LeaseDisposition
from arena_hero_agent.ports.leases import WriterLeaseHandle

TENANT = TenantId("t1")
GENERATION = Generation(3)
BUDGET = DeadlineBudget.from_milliseconds(1000)


class ManualTick:
    def __init__(self, tick: int = 0) -> None:
        self.tick = tick

    def __call__(self) -> int:
        return self.tick


def command(command_id: str = "cmd:a", idempotency_key: str | None = None) -> DirectorCommand:
    return DirectorCommand(
        schema_version=COMMAND_SCHEMA_VERSION,
        command_id=CommandId(command_id),
        tenant_id=TENANT,
        issuer=CommandIssuer.DIRECTOR,
        issued_at_tick=10,
        expires_at_tick=40,
        expected_generation=GENERATION,
        idempotency_key=idempotency_key or f"key-{command_id}",
        action=CommandAction(kind="squad_attack", target=Coordinate(4, 5)),
    )


class _RestartAwareLeases:
    """WriterLease facade: plain acquire first, exact-fence takeover after restart."""

    def __init__(
        self,
        coordinator: FileWriterLeaseCoordinator,
        *,
        observed_fence: FencingToken | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._fence = observed_fence

    async def acquire_writer(
        self,
        tenant_id: TenantId,
        generation: Generation,
        budget: DeadlineBudget,
    ) -> WriterLeaseHandle | None:
        if self._fence is not None:
            handle = await self._coordinator.replace_writer(
                tenant_id,
                generation,
                expected_fencing_token=self._fence,
                budget=budget,
            )
            if handle is not None:
                self._fence = None
            return handle
        return await self._coordinator.acquire_writer(tenant_id, generation, budget)

    async def replace_writer(
        self,
        tenant_id: TenantId,
        generation: Generation,
        *,
        expected_fencing_token: FencingToken,
        budget: DeadlineBudget,
    ) -> WriterLeaseHandle | None:
        return await self._coordinator.replace_writer(
            tenant_id,
            generation,
            expected_fencing_token=expected_fencing_token,
            budget=budget,
        )


# Static conformance exercised by `ty check`.
def _bus(root: Path) -> FileCommandBus:
    return FileCommandBus(root)


async def test_file_bus_satisfies_control_protocols(tmp_path: Path) -> None:
    bus = _bus(tmp_path)
    command_bus: CommandBus[DirectorCommand] = bus
    ledger: CommandLedger = bus
    audit: CommandAudit = bus
    assert isinstance(command_bus, CommandBus)
    assert isinstance(ledger, CommandLedger)
    assert isinstance(audit, CommandAudit)


async def test_publish_and_pending_round_trip_in_issue_order(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    first = command("cmd:a")
    second = command("cmd:b")
    await bus.publish(first)
    await bus.publish(second)

    pending = [item async for item in bus.pending(TENANT)]
    assert pending == [first, second]
    received = [item async for item in bus.receive(TENANT)]
    assert received == [first, second]


async def test_pending_filters_applied_markers_across_bus_instances(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    first = command("cmd:a")
    await bus.publish(first)
    await bus.publish(command("cmd:b"))
    coordinator = FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=60_000_000_000)
    lease = await coordinator.acquire_writer(TENANT, GENERATION, BUDGET)
    assert lease is not None
    assert await bus.mark_applied(
        TENANT,
        command=first,
        generation=GENERATION,
        applied_at_tick=21,
        lease=lease,
    )
    await lease.release()

    fresh = FileCommandBus(tmp_path)
    pending = [item async for item in fresh.pending(TENANT)]
    assert [item.command_id for item in pending] == [CommandId("cmd:b")]
    assert await fresh.is_applied(TENANT, command=first)


async def test_is_applied_matches_command_id_or_idempotency_key(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    first = command("cmd:first", idempotency_key="shared-key")
    await bus.publish(first)
    coordinator = FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=60_000_000_000)
    lease = await coordinator.acquire_writer(TENANT, GENERATION, BUDGET)
    assert lease is not None
    assert await bus.mark_applied(
        TENANT, command=first, generation=GENERATION, applied_at_tick=21, lease=lease
    )
    await lease.release()

    same_command = command("cmd:first")
    same_key = command("cmd:other", idempotency_key="shared-key")
    unrelated = command("cmd:unrelated")
    assert await bus.is_applied(TENANT, command=same_command)
    assert await bus.is_applied(TENANT, command=same_key)
    assert not await bus.is_applied(TENANT, command=unrelated)


async def test_mark_applied_is_idempotent_under_the_same_lease(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    cmd = command("cmd:dup")
    await bus.publish(cmd)
    coordinator = FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=60_000_000_000)
    lease = await coordinator.acquire_writer(TENANT, GENERATION, BUDGET)
    assert lease is not None

    assert await bus.mark_applied(
        TENANT, command=cmd, generation=GENERATION, applied_at_tick=21, lease=lease
    )
    assert not await bus.mark_applied(
        TENANT, command=cmd, generation=GENERATION, applied_at_tick=22, lease=lease
    )
    await lease.release()

    record = json.loads((tmp_path / TENANT.value / "writer-lease.json").read_text(encoding="utf-8"))
    assert record["schemaVersion"] == 1


async def test_mark_applied_fails_closed_on_stale_or_wrong_lease(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    cmd = command("cmd:stale")
    coordinator = FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=100)
    lease = await coordinator.acquire_writer(TENANT, GENERATION, BUDGET)
    assert lease is not None
    await lease.release()
    assert lease.disposition is LeaseDisposition.RELEASED

    with pytest.raises(CommandBusError):
        await bus.mark_applied(
            TENANT, command=cmd, generation=GENERATION, applied_at_tick=21, lease=lease
        )

    other = await coordinator.acquire_writer(TenantId("t2"), GENERATION, BUDGET)
    assert other is not None
    try:
        with pytest.raises(CommandBusError):
            await bus.mark_applied(
                TENANT, command=cmd, generation=GENERATION, applied_at_tick=21, lease=other
            )
    finally:
        await other.release()


async def test_audit_records_and_reads_back_oldest_first(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    first = command("cmd:a")
    second = command("cmd:b")
    await bus.record(
        TENANT,
        event=CommandAuditEvent(
            command_id=first.command_id,
            tenant_id=TENANT,
            disposition=CommandDisposition.ACCEPTED,
            reason=None,
            generation=GENERATION,
            tick=10,
        ),
    )
    await bus.record(
        TENANT,
        event=CommandAuditEvent(
            command_id=second.command_id,
            tenant_id=TENANT,
            disposition=CommandDisposition.APPLIED,
            reason=None,
            generation=GENERATION,
            tick=21,
        ),
    )

    events = bus.read_audit(TENANT)
    assert [event.command_id for event in events] == [first.command_id, second.command_id]
    assert [event.disposition for event in events] == [
        CommandDisposition.ACCEPTED,
        CommandDisposition.APPLIED,
    ]
    assert bus.read_audit(TENANT, limit=1) == [events[-1]]

    with pytest.raises(CommandBusError):
        await bus.record(
            TenantId("t2"),
            event=events[0],
        )


async def test_audit_failure_never_blocks_command_processing(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    # Replace the audit target with a directory so the append fails closed at
    # the writer level; the audit must stay fail-open (TypeScript parity).
    audit_path = tmp_path / "command-bus" / TENANT.value / "audit.jsonl"
    audit_path.mkdir(parents=True)

    cmd = command("cmd:a")
    await bus.publish(cmd)
    assert not await bus.is_applied(TENANT, command=cmd)
    events = bus.read_audit(TENANT)
    assert events == []

    coordinator = FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=60_000_000_000)
    lease = await coordinator.acquire_writer(TENANT, GENERATION, BUDGET)
    assert lease is not None
    assert await bus.mark_applied(
        TENANT, command=cmd, generation=GENERATION, applied_at_tick=21, lease=lease
    )
    await lease.release()


async def test_torn_tail_is_skipped_and_malformed_line_fails_closed(tmp_path: Path) -> None:
    bus = FileCommandBus(tmp_path)
    cmd = command("cmd:a")
    await bus.publish(cmd)
    commands_path = tmp_path / "command-bus" / TENANT.value / "commands.jsonl"
    with commands_path.open("ab") as handle:
        handle.write(b'{"schemaVersion": 1, "commandId": "cmd:torn"')

    pending = [item async for item in bus.pending(TENANT)]
    assert pending == [cmd]

    with commands_path.open("ab") as handle:
        handle.write(b'{"schemaVersion": 1, "commandId": "cmd:bad"}\n')
    with pytest.raises(CommandBusError):
        _ = [item async for item in bus.pending(TENANT)]


async def test_full_stack_restart_with_file_writer_lease(tmp_path: Path) -> None:
    bus_a = FileCommandBus(tmp_path)
    first = command("cmd:first")
    second = command("cmd:second")
    await bus_a.publish(first)
    await bus_a.publish(second)
    leases_a = _RestartAwareLeases(
        FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=60_000_000_000, holder_id="a")
    )
    tick_a = ManualTick(20)
    applier_a = TenantCommandApplier(ledger=bus_a, audit=bus_a, writer_leases=leases_a, now=tick_a)

    results_a = await applier_a.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)
    assert [result.disposition for result in results_a] == [
        CommandDisposition.APPLIED,
        CommandDisposition.APPLIED,
    ]

    record = json.loads((tmp_path / TENANT.value / "writer-lease.json").read_text(encoding="utf-8"))
    observed_fence = FencingToken(record["fencingToken"])

    third = command("cmd:third")
    await FileCommandBus(tmp_path).publish(third)

    bus_b = FileCommandBus(tmp_path)
    leases_b = _RestartAwareLeases(
        FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=60_000_000_000, holder_id="b"),
        observed_fence=observed_fence,
    )
    tick_b = ManualTick(21)
    applier_b = TenantCommandApplier(ledger=bus_b, audit=bus_b, writer_leases=leases_b, now=tick_b)

    results_b = await applier_b.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)
    assert [result.disposition for result in results_b] == [CommandDisposition.APPLIED]
    assert results_b[0].command == third
    assert await bus_b.is_applied(TENANT, command=first)
    audit = bus_b.read_audit(TENANT)
    assert [event.disposition for event in audit] == [
        CommandDisposition.APPLIED,
        CommandDisposition.APPLIED,
        CommandDisposition.APPLIED,
    ]
