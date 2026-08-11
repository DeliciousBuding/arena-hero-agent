from __future__ import annotations

from typing import cast

import pytest

from arena_hero_agent.alliance.commands import (
    COMMAND_SCHEMA_VERSION,
    CommandAction,
    CommandAuditEvent,
    CommandDisposition,
    CommandIssuer,
    DirectorCommand,
    validate_command,
    validate_issuance,
)
from arena_hero_agent.domain import (
    CommandId,
    Coordinate,
    EntityId,
    Generation,
    TenantId,
    canonical_sha256,
)

TENANT = TenantId("t1")
GENERATION = Generation(3)
ACTION = CommandAction(kind="squad_attack", target=Coordinate(4, 5), unit_ids=(EntityId("u1"),))


def command(
    *,
    command_id: str = "cmd:attack-1",
    tenant_id: TenantId = TENANT,
    issuer: CommandIssuer = CommandIssuer.DIRECTOR,
    issued_at_tick: int = 10,
    expires_at_tick: int = 40,
    expected_generation: Generation = GENERATION,
    idempotency_key: str = "key-1",
    action: CommandAction = ACTION,
    schema_version: int = COMMAND_SCHEMA_VERSION,
) -> DirectorCommand:
    return DirectorCommand(
        schema_version=schema_version,
        command_id=CommandId(command_id),
        tenant_id=tenant_id,
        issuer=issuer,
        issued_at_tick=issued_at_tick,
        expires_at_tick=expires_at_tick,
        expected_generation=expected_generation,
        idempotency_key=idempotency_key,
        action=action,
    )


def test_command_id_rejects_non_canonical_values() -> None:
    with pytest.raises(ValueError):
        CommandId("")
    with pytest.raises(ValueError):
        CommandId("has space")
    assert CommandId("cmd:attack-1").value == "cmd:attack-1"


def test_command_action_validates_and_normalizes_unit_ids() -> None:
    with pytest.raises(ValueError):
        CommandAction(kind="")
    with pytest.raises(TypeError):
        CommandAction(kind="attack", target=cast(Coordinate, (4, 5)))
    normalized = CommandAction(
        kind="attack",
        unit_ids=cast(tuple[EntityId, ...], [EntityId("u1"), EntityId("u2")]),
    )
    assert normalized.unit_ids == (EntityId("u1"), EntityId("u2"))
    with pytest.raises(TypeError):
        CommandAction(kind="attack", unit_ids=cast(tuple[EntityId, ...], ["u1", "u2"]))


def test_command_envelope_enforces_expiry_after_issue() -> None:
    with pytest.raises(ValueError):
        command(issued_at_tick=40, expires_at_tick=40)
    with pytest.raises(ValueError):
        command(issued_at_tick=50, expires_at_tick=40)
    with pytest.raises(ValueError):
        command(idempotency_key="")
    with pytest.raises(TypeError):
        command(schema_version=True)


def test_command_json_round_trip_preserves_every_field() -> None:
    original = command(
        command_id="cmd:attack-1",
        idempotency_key="key-1",
        action=CommandAction(
            kind="worker_relocate",
            target=Coordinate(-11, -1),
            unit_ids=(EntityId("u1"), EntityId("u2")),
        ),
    )
    restored = DirectorCommand.from_json_object(original.to_json_object())
    assert restored == original
    assert canonical_sha256(restored) == canonical_sha256(original)


def test_command_json_round_trip_without_optional_action_fields() -> None:
    original = command(action=CommandAction(kind="core_guard"))
    restored = DirectorCommand.from_json_object(original.to_json_object())
    assert restored == original
    assert restored.action.target is None
    assert restored.action.unit_ids == ()


def test_command_decode_fails_closed_on_malformed_payloads() -> None:
    valid = command().to_json_object()
    missing = dict(valid)
    del missing["commandId"]
    with pytest.raises(ValueError):
        DirectorCommand.from_json_object(missing)

    unsupported = dict(valid, schemaVersion=2)
    with pytest.raises(ValueError):
        DirectorCommand.from_json_object(unsupported)

    wrong_type = dict(valid, issuedAtTick=True)
    with pytest.raises(ValueError):
        DirectorCommand.from_json_object(wrong_type)

    bad_issuer = dict(valid, issuer="goblin")
    with pytest.raises(ValueError):
        DirectorCommand.from_json_object(bad_issuer)

    bad_target = dict(valid, action={"kind": "attack", "target": [1], "unitIds": []})
    with pytest.raises(ValueError):
        DirectorCommand.from_json_object(bad_target)

    with pytest.raises(ValueError):
        DirectorCommand.from_json_object(["not", "an", "object"])


def test_validate_issuance_rejects_unsupported_version_and_agent_issuer() -> None:
    assert validate_issuance(command()) is None
    assert validate_issuance(command(issuer=CommandIssuer.HUMAN)) is None
    unsupported = validate_issuance(command(schema_version=2))
    assert unsupported is not None
    assert "unsupported command schema version" in unsupported
    unauthorized = validate_issuance(command(issuer=CommandIssuer.AGENT))
    assert unauthorized is not None
    assert "not authorized" in unauthorized


def test_validate_command_is_fail_closed_and_deterministic() -> None:
    now = 20
    assert validate_command(
        command(), tenant_id=TENANT, current_generation=GENERATION, now=now
    ) == (
        CommandDisposition.ACCEPTED,
        None,
    )
    assert (
        validate_command(
            command(schema_version=2),
            tenant_id=TENANT,
            current_generation=GENERATION,
            now=now,
        )[0]
        is CommandDisposition.REJECTED
    )
    assert (
        validate_command(
            command(tenant_id=TenantId("t2")),
            tenant_id=TENANT,
            current_generation=GENERATION,
            now=now,
        )[0]
        is CommandDisposition.REJECTED
    )
    assert (
        validate_command(
            command(issuer=CommandIssuer.AGENT),
            tenant_id=TENANT,
            current_generation=GENERATION,
            now=now,
        )[0]
        is CommandDisposition.UNAUTHORIZED
    )
    assert (
        validate_command(
            command(),
            tenant_id=TENANT,
            current_generation=GENERATION,
            now=40,
        )[0]
        is CommandDisposition.EXPIRED
    )
    assert (
        validate_command(
            command(expected_generation=Generation(99)),
            tenant_id=TENANT,
            current_generation=GENERATION,
            now=now,
        )[0]
        is CommandDisposition.REJECTED
    )


def test_audit_event_round_trip_and_validation() -> None:
    event = CommandAuditEvent(
        command_id=CommandId("cmd:attack-1"),
        tenant_id=TENANT,
        disposition=CommandDisposition.APPLIED,
        reason=None,
        generation=GENERATION,
        tick=21,
    )
    restored = CommandAuditEvent.from_json_object(event.to_json_object())
    assert restored == event
    with pytest.raises(ValueError):
        CommandAuditEvent.from_json_object({"commandId": "cmd:attack-1"})
