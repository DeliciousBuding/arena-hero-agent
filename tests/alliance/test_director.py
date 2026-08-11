from __future__ import annotations

from pathlib import Path

from arena_hero_agent.adapters.runtime import FileCommandBus
from arena_hero_agent.alliance.commands import (
    COMMAND_SCHEMA_VERSION,
    CommandAction,
    CommandDisposition,
    CommandIssuer,
    DirectorCommand,
)
from arena_hero_agent.alliance.director import Director
from arena_hero_agent.domain import CommandId, Coordinate, Generation, TenantId

TENANT = TenantId("t1")
GENERATION = Generation(3)


def command(
    *,
    command_id: str = "cmd:attack-1",
    issuer: CommandIssuer = CommandIssuer.DIRECTOR,
    schema_version: int = COMMAND_SCHEMA_VERSION,
) -> DirectorCommand:
    return DirectorCommand(
        schema_version=schema_version,
        command_id=CommandId(command_id),
        tenant_id=TENANT,
        issuer=issuer,
        issued_at_tick=10,
        expires_at_tick=40,
        expected_generation=GENERATION,
        idempotency_key=f"key-{command_id}",
        action=CommandAction(kind="squad_attack", target=Coordinate(4, 5)),
    )


def director(root: Path) -> Director:
    bus = FileCommandBus(root)
    return Director(ledger=bus, audit=bus)


async def test_issue_publishes_and_audits_accepted(tmp_path: Path) -> None:
    d = director(tmp_path)
    cmd = command()

    assert await d.issue(cmd) is CommandDisposition.ACCEPTED

    bus = FileCommandBus(tmp_path)
    pending = [item async for item in bus.pending(TENANT)]
    assert pending == [cmd]
    audit = bus.read_audit(TENANT)
    assert len(audit) == 1
    assert audit[0].command_id == cmd.command_id
    assert audit[0].disposition is CommandDisposition.ACCEPTED
    assert audit[0].reason is None
    assert audit[0].generation == GENERATION
    assert audit[0].tick == cmd.issued_at_tick


async def test_issue_rejects_unsupported_version_without_publishing(tmp_path: Path) -> None:
    d = director(tmp_path)
    cmd = command(schema_version=2)

    assert await d.issue(cmd) is CommandDisposition.REJECTED

    bus = FileCommandBus(tmp_path)
    assert [item async for item in bus.pending(TENANT)] == []
    audit = bus.read_audit(TENANT)
    assert len(audit) == 1
    assert audit[0].disposition is CommandDisposition.REJECTED
    assert audit[0].reason == "unsupported command schema version"


async def test_issue_rejects_agent_issuer_as_unauthorized(tmp_path: Path) -> None:
    d = director(tmp_path)
    cmd = command(issuer=CommandIssuer.AGENT)

    assert await d.issue(cmd) is CommandDisposition.UNAUTHORIZED

    bus = FileCommandBus(tmp_path)
    assert [item async for item in bus.pending(TENANT)] == []
    audit = bus.read_audit(TENANT)
    assert len(audit) == 1
    assert audit[0].disposition is CommandDisposition.UNAUTHORIZED
    assert audit[0].reason == "agent issuers are not authorized for director commands"


async def test_issue_audits_every_outcome_per_command(tmp_path: Path) -> None:
    d = director(tmp_path)
    good = command(command_id="cmd:ok")
    bad = command(command_id="cmd:bad", schema_version=2)

    assert await d.issue(good) is CommandDisposition.ACCEPTED
    assert await d.issue(bad) is CommandDisposition.REJECTED

    audit = FileCommandBus(tmp_path).read_audit(TENANT)
    assert [event.disposition for event in audit] == [
        CommandDisposition.ACCEPTED,
        CommandDisposition.REJECTED,
    ]
