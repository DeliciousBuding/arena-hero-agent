"""P4-16 command bus fault injection: half-write replay, disconnect replay.

Scenarios: a torn ledger tail from an interrupted append is skipped (never
committed) and applies only once the full line exists; a restart/reconnect
replays without double-applying (applier idempotency via durable markers); a
half-written applied.json tmp is never promoted; malformed ledger lines fail
closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arena_hero_agent.adapters.runtime import FileCommandBus, FileWriterLeaseCoordinator
from arena_hero_agent.alliance.applier import CommandContext, TenantCommandApplier
from arena_hero_agent.alliance.commands import (
    COMMAND_SCHEMA_VERSION,
    CommandAction,
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
from arena_hero_agent.ports.leases import WriterLeaseHandle

TENANT = TenantId("t1")
GENERATION = Generation(3)
BUDGET = DeadlineBudget.from_milliseconds(1000)
LEASE_NS = 60_000_000_000


def command(command_id: str = "cmd:a") -> DirectorCommand:
    return DirectorCommand(
        schema_version=COMMAND_SCHEMA_VERSION,
        command_id=CommandId(command_id),
        tenant_id=TENANT,
        issuer=CommandIssuer.DIRECTOR,
        issued_at_tick=10,
        expires_at_tick=40,
        expected_generation=GENERATION,
        idempotency_key=f"key-{command_id}",
        action=CommandAction(kind="squad_attack", target=Coordinate(4, 5)),
    )


class ManualTick:
    def __init__(self, tick: int = 0) -> None:
        self.tick = tick

    def __call__(self) -> int:
        return self.tick


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


def _commands_path(tmp_path: Path) -> Path:
    return tmp_path / "command-bus" / TENANT.value / "commands.jsonl"


def _lease_record_fence(tmp_path: Path) -> FencingToken:
    record = json.loads((tmp_path / TENANT.value / "writer-lease.json").read_text(encoding="utf-8"))
    return FencingToken(record["fencingToken"])


async def test_half_written_ledger_tail_skipped_then_applies_after_completion(
    tmp_path: Path,
) -> None:
    """A torn append is never committed; the full line applies on replay."""
    bus = FileCommandBus(tmp_path)
    cmd_a = command("cmd:a")
    cmd_b = command("cmd:b")
    await bus.publish(cmd_a)
    # Crash mid-append of cmd_b: the ledger ends without a newline.
    with _commands_path(tmp_path).open("ab") as handle:
        handle.write(json.dumps(cmd_b.to_json_object(), sort_keys=True).encode("utf-8"))

    coordinator = FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=LEASE_NS)
    applier = TenantCommandApplier(ledger=bus, audit=bus, writer_leases=coordinator, now=lambda: 20)
    results = await applier.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)
    assert [result.command.command_id for result in results] == [CommandId("cmd:a")]
    assert [result.disposition for result in results] == [CommandDisposition.APPLIED]

    # The torn line was never committed: pending is empty (cmd_a applied).
    fresh = FileCommandBus(tmp_path)
    assert [item async for item in fresh.pending(TENANT)] == []

    # Complete the interrupted write and replay: only cmd_b applies, cmd_a is
    # not re-applied (idempotent).
    with _commands_path(tmp_path).open("ab") as handle:
        handle.write(b"\n")
        handle.write(json.dumps(cmd_b.to_json_object(), sort_keys=True).encode("utf-8"))
        handle.write(b"\n")
    leases = _RestartAwareLeases(
        FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=LEASE_NS, holder_id="b"),
        observed_fence=_lease_record_fence(tmp_path),
    )
    applier_b = TenantCommandApplier(
        ledger=fresh, audit=fresh, writer_leases=leases, now=ManualTick(21)
    )
    results_b = await applier_b.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)
    # The completed line applies once; the identical duplicate line is detected
    # by the durable marker and replayed as DUPLICATE (no double apply).
    assert [result.command.command_id for result in results_b] == [
        CommandId("cmd:b"),
        CommandId("cmd:b"),
    ]
    assert [result.disposition for result in results_b] == [
        CommandDisposition.APPLIED,
        CommandDisposition.DUPLICATE,
    ]

    assert await fresh.is_applied(TENANT, command=cmd_a)
    assert await fresh.is_applied(TENANT, command=cmd_b)
    audit = fresh.read_audit(TENANT)
    applied = [event for event in audit if event.disposition is CommandDisposition.APPLIED]
    assert [event.command_id for event in applied] == [
        cmd_a.command_id,
        cmd_b.command_id,
    ]
    # A further replay with nothing new is a no-op.
    assert await applier_b.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET) == ()


async def test_disconnect_replay_never_double_applies(tmp_path: Path) -> None:
    """A reconnect with the same pending set applies nothing a second time."""
    bus_a = FileCommandBus(tmp_path)
    first = command("cmd:a")
    second = command("cmd:b")
    await bus_a.publish(first)
    await bus_a.publish(second)
    applier_a = TenantCommandApplier(
        ledger=bus_a,
        audit=bus_a,
        writer_leases=FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=LEASE_NS),
        now=ManualTick(20),
    )
    results = await applier_a.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)
    assert [result.disposition for result in results] == [
        CommandDisposition.APPLIED,
        CommandDisposition.APPLIED,
    ]

    # "Disconnect": a brand-new bus and applier instance (new holder) replays.
    bus_b = FileCommandBus(tmp_path)
    leases_b = _RestartAwareLeases(
        FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=LEASE_NS, holder_id="b"),
        observed_fence=_lease_record_fence(tmp_path),
    )
    applier_b = TenantCommandApplier(
        ledger=bus_b, audit=bus_b, writer_leases=leases_b, now=ManualTick(21)
    )
    assert await applier_b.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET) == ()

    # A new command after reconnect applies exactly once.
    third = command("cmd:c")
    await bus_b.publish(third)
    results_c = await applier_b.drain(context=CommandContext(TENANT, GENERATION), budget=BUDGET)
    assert [result.command.command_id for result in results_c] == [CommandId("cmd:c")]
    audit = bus_b.read_audit(TENANT)
    applied_ids = [
        event.command_id for event in audit if event.disposition is CommandDisposition.APPLIED
    ]
    assert applied_ids == [first.command_id, second.command_id, third.command_id]


async def test_half_written_applied_marker_tmp_is_never_promoted(tmp_path: Path) -> None:
    """A crash mid applied.json rewrite leaves a tmp that is ignored."""
    bus = FileCommandBus(tmp_path)
    cmd = command("cmd:a")
    await bus.publish(cmd)
    coordinator = FileWriterLeaseCoordinator(tmp_path, lease_duration_ns=LEASE_NS)
    lease = await coordinator.acquire_writer(TENANT, GENERATION, BUDGET)
    assert lease is not None
    assert await bus.mark_applied(
        TENANT, command=cmd, generation=GENERATION, applied_at_tick=21, lease=lease
    )
    await lease.release()

    applied_path = tmp_path / "command-bus" / TENANT.value / "applied.json"
    partial = applied_path.with_name("applied.json.tmp")
    partial.write_text('{"schemaVersion":1,"tenantId":"t1","applied":[', encoding="utf-8")

    fresh = FileCommandBus(tmp_path)
    assert await fresh.is_applied(TENANT, command=cmd)
    assert [item async for item in fresh.pending(TENANT)] == []
    # The half-written tmp is not promoted and does not break any read.
    assert partial.exists()


@pytest.mark.parametrize(
    "bad_line",
    [
        "this is not json\n",
        '{"schemaVersion":1,"commandId":"cmd:x"}\n',
        '["not","an","object"]\n',
    ],
)
async def test_malformed_ledger_line_fails_closed(tmp_path: Path, bad_line: str) -> None:
    """A committed-but-malformed command line must fail closed, never skip."""
    bus = FileCommandBus(tmp_path)
    await bus.publish(command("cmd:a"))
    with _commands_path(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write(bad_line)

    with pytest.raises(CommandBusError):
        _ = [item async for item in bus.pending(TENANT)]
