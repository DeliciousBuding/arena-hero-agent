"""Versioned director command protocol: envelope, dispositions, and audit events.

P4-16 defines the command bus contract between the director and tenant
runtimes (architecture "State ownership"). A command is an immutable envelope
carrying a schema version, identifier, target tenant, issuer, issue time,
expiry, expected generation, idempotency key, and directive action. The
director publishes commands and never writes tenant state; the receiving
tenant validates each command fail-closed and applies or rejects it.

Validation is deterministic and order-sensitive: unsupported versions and
cross-tenant commands are rejected, agent issuers are unauthorized, expired
commands are expired, stale-generation commands are rejected, and only then a
command may be accepted for application. Wall-clock values never enter the
envelope; issue/expiry use tenant ticks.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from arena_hero_agent.domain import (
    CommandId,
    Coordinate,
    EntityId,
    Generation,
    TenantId,
)

COMMAND_SCHEMA_VERSION = 1


class CommandBusError(RuntimeError):
    """Fail-closed failure in the director command bus."""


class CommandIssuer(StrEnum):
    """Command source; subordinate agents cannot issue top-level commands."""

    __canonical_name__ = "arena-hero.command-issuer.v1"

    HUMAN = "human"
    DIRECTOR = "director"
    AGENT = "agent"


class CommandDisposition(StrEnum):
    """Lifecycle outcome recorded for every processed command."""

    __canonical_name__ = "arena-hero.command-disposition.v1"

    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DUPLICATE = "duplicate"
    UNAUTHORIZED = "unauthorized"


@dataclass(frozen=True, slots=True)
class CommandAction:
    """Typed directive body in the shape of the TS intent spec (minimal)."""

    __canonical_name__ = "arena-hero.command-action.v1"

    kind: str
    target: Coordinate | None = None
    unit_ids: tuple[EntityId, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("command action kind must be a non-empty string")
        if self.target is not None and not isinstance(self.target, Coordinate):
            raise TypeError("command action target must be a Coordinate or None")
        if not isinstance(self.unit_ids, Sequence) or isinstance(self.unit_ids, (str, bytes)):
            raise TypeError("command action unit_ids must be a sequence of EntityId")
        normalized = tuple(self.unit_ids)
        if not all(isinstance(unit, EntityId) for unit in normalized):
            raise TypeError("command action unit_ids must contain only EntityId")
        object.__setattr__(self, "unit_ids", normalized)

    def to_json_object(self) -> dict[str, object]:
        target: object = None
        if self.target is not None:
            target = [self.target.x, self.target.y]
        return {
            "kind": self.kind,
            "target": target,
            "unitIds": [unit.value for unit in self.unit_ids],
        }

    @classmethod
    def from_json_object(cls, payload: object) -> Self:
        if not isinstance(payload, dict):
            raise ValueError("command action must be a JSON object")
        try:
            kind = payload["kind"]
            target = payload.get("target")
            unit_ids = payload.get("unitIds", [])
        except KeyError as error:
            raise ValueError(f"command action is missing required key {error.args[0]!r}") from error
        if not isinstance(kind, str) or not kind:
            raise ValueError("command action kind must be a non-empty string")
        if target is not None and (
            not isinstance(target, list)
            or len(target) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in target)
        ):
            raise ValueError("command action target must be a [x, y] integer pair or null")
        if not isinstance(unit_ids, list) or not all(isinstance(item, str) for item in unit_ids):
            raise ValueError("command action unitIds must be a list of strings")
        try:
            return cls(
                kind=kind,
                target=None if target is None else Coordinate(target[0], target[1]),
                unit_ids=tuple(EntityId(unit) for unit in unit_ids),
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"malformed command action: {error}") from error


@dataclass(frozen=True, slots=True)
class DirectorCommand:
    """Immutable versioned command envelope issued by the director."""

    __canonical_name__ = "arena-hero.director-command.v1"

    schema_version: int
    command_id: CommandId
    tenant_id: TenantId
    issuer: CommandIssuer
    issued_at_tick: int
    expires_at_tick: int
    expected_generation: Generation
    idempotency_key: str
    action: CommandAction

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise TypeError("command schema version must be an integer")
        if not isinstance(self.command_id, CommandId):
            raise TypeError("command id must be a CommandId")
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("command tenant id must be a TenantId")
        if not isinstance(self.issuer, CommandIssuer):
            raise TypeError("command issuer must be a CommandIssuer")
        if not isinstance(self.issued_at_tick, int) or isinstance(self.issued_at_tick, bool):
            raise TypeError("command issue tick must be an integer")
        if self.issued_at_tick < 0:
            raise ValueError("command issue tick cannot be negative")
        if not isinstance(self.expires_at_tick, int) or isinstance(self.expires_at_tick, bool):
            raise TypeError("command expiry tick must be an integer")
        if self.expires_at_tick <= self.issued_at_tick:
            raise ValueError("command expiry tick must be after the issue tick")
        if not isinstance(self.expected_generation, Generation):
            raise TypeError("command expected generation must be a Generation")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValueError("command idempotency key must be a non-empty string")
        if not isinstance(self.action, CommandAction):
            raise TypeError("command action must be a CommandAction")

    def to_json_object(self) -> dict[str, object]:
        target: object = None
        if self.action.target is not None:
            target = [self.action.target.x, self.action.target.y]
        return {
            "schemaVersion": self.schema_version,
            "commandId": self.command_id.value,
            "tenantId": self.tenant_id.value,
            "issuer": self.issuer.value,
            "issuedAtTick": self.issued_at_tick,
            "expiresAtTick": self.expires_at_tick,
            "expectedGeneration": self.expected_generation.value,
            "idempotencyKey": self.idempotency_key,
            "action": {
                "kind": self.action.kind,
                "target": target,
                "unitIds": [unit.value for unit in self.action.unit_ids],
            },
        }

    @classmethod
    def from_json_object(cls, payload: object) -> Self:
        """Strictly decode a canonical command envelope; malformed input raises."""
        if not isinstance(payload, dict):
            raise ValueError("command must be a JSON object")
        try:
            schema_version = payload["schemaVersion"]
            command_id = payload["commandId"]
            tenant_id = payload["tenantId"]
            issuer = payload["issuer"]
            issued_at_tick = payload["issuedAtTick"]
            expires_at_tick = payload["expiresAtTick"]
            expected_generation = payload["expectedGeneration"]
            idempotency_key = payload["idempotencyKey"]
            action = payload["action"]
        except KeyError as error:
            raise ValueError(f"command is missing required key {error.args[0]!r}") from error
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ValueError("command schema version must be an integer")
        if schema_version != COMMAND_SCHEMA_VERSION:
            raise ValueError("unsupported command schema version")
        if not isinstance(command_id, str):
            raise ValueError("command id must be a string")
        if not isinstance(tenant_id, str):
            raise ValueError("command tenant id must be a string")
        if not isinstance(issuer, str):
            raise ValueError("command issuer must be a string")
        if isinstance(issued_at_tick, bool) or not isinstance(issued_at_tick, int):
            raise ValueError("command issue tick must be an integer")
        if isinstance(expires_at_tick, bool) or not isinstance(expires_at_tick, int):
            raise ValueError("command expiry tick must be an integer")
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int):
            raise ValueError("command expected generation must be an integer")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("command idempotency key must be a non-empty string")
        decoded_action = CommandAction.from_json_object(action)
        try:
            return cls(
                schema_version=schema_version,
                command_id=CommandId(command_id),
                tenant_id=TenantId(tenant_id),
                issuer=CommandIssuer(issuer),
                issued_at_tick=issued_at_tick,
                expires_at_tick=expires_at_tick,
                expected_generation=Generation(expected_generation),
                idempotency_key=idempotency_key,
                action=decoded_action,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"malformed command envelope: {error}") from error


@dataclass(frozen=True, slots=True)
class CommandAuditEvent:
    """One append-only lifecycle event for a director command."""

    __canonical_name__ = "arena-hero.command-audit-event.v1"

    command_id: CommandId
    tenant_id: TenantId
    disposition: CommandDisposition
    reason: str | None
    generation: Generation
    tick: int

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, CommandId):
            raise TypeError("audit command id must be a CommandId")
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("audit tenant id must be a TenantId")
        if not isinstance(self.disposition, CommandDisposition):
            raise TypeError("audit disposition must be a CommandDisposition")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("audit reason must be a string or None")
        if not isinstance(self.generation, Generation):
            raise TypeError("audit generation must be a Generation")
        if isinstance(self.tick, bool) or not isinstance(self.tick, int):
            raise TypeError("audit tick must be an integer")
        if self.tick < 0:
            raise ValueError("audit tick cannot be negative")

    def to_json_object(self) -> dict[str, object]:
        return {
            "commandId": self.command_id.value,
            "tenantId": self.tenant_id.value,
            "disposition": self.disposition.value,
            "reason": self.reason,
            "generation": self.generation.value,
            "tick": self.tick,
        }

    @classmethod
    def from_json_object(cls, payload: object) -> Self:
        if not isinstance(payload, dict):
            raise ValueError("audit event must be a JSON object")
        try:
            command_id = payload["commandId"]
            tenant_id = payload["tenantId"]
            disposition = payload["disposition"]
            reason = payload["reason"]
            generation = payload["generation"]
            tick = payload["tick"]
        except KeyError as error:
            raise ValueError(f"audit event is missing required key {error.args[0]!r}") from error
        if not isinstance(command_id, str):
            raise ValueError("audit command id must be a string")
        if not isinstance(tenant_id, str):
            raise ValueError("audit tenant id must be a string")
        if not isinstance(disposition, str):
            raise ValueError("audit disposition must be a string")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("audit reason must be a string or null")
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError("audit generation must be an integer")
        if isinstance(tick, bool) or not isinstance(tick, int):
            raise ValueError("audit tick must be an integer")
        try:
            return cls(
                command_id=CommandId(command_id),
                tenant_id=TenantId(tenant_id),
                disposition=CommandDisposition(disposition),
                reason=reason,
                generation=Generation(generation),
                tick=tick,
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"malformed audit event: {error}") from error


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of processing one pending command."""

    __canonical_name__ = "arena-hero.command-result.v1"

    command: DirectorCommand
    disposition: CommandDisposition
    reason: str | None = None


def validate_issuance(command: DirectorCommand) -> str | None:
    """Director-side envelope checks; returns a fail-closed reason or None."""
    if command.schema_version != COMMAND_SCHEMA_VERSION:
        return "unsupported command schema version"
    if command.issuer is CommandIssuer.AGENT:
        return "agent issuers are not authorized for director commands"
    if command.expires_at_tick <= command.issued_at_tick:
        return "command expiry must be after its issue time"
    return None


def validate_command(
    command: DirectorCommand,
    *,
    tenant_id: TenantId,
    current_generation: Generation,
    now: int,
) -> tuple[CommandDisposition, str | None]:
    """Fail-closed tenant-side validation; returns ``(disposition, reason)``."""
    if command.schema_version != COMMAND_SCHEMA_VERSION:
        return CommandDisposition.REJECTED, "unsupported command schema version"
    if command.tenant_id != tenant_id:
        return CommandDisposition.REJECTED, "command targets another tenant"
    if command.issuer is CommandIssuer.AGENT:
        return (
            CommandDisposition.UNAUTHORIZED,
            "agent issuers are not authorized for director commands",
        )
    if now >= command.expires_at_tick:
        return CommandDisposition.EXPIRED, "command has expired"
    if command.expected_generation != current_generation:
        return CommandDisposition.REJECTED, "command expected generation is stale"
    return CommandDisposition.ACCEPTED, None


__all__ = [
    "COMMAND_SCHEMA_VERSION",
    "CommandAction",
    "CommandAuditEvent",
    "CommandBusError",
    "CommandDisposition",
    "CommandIssuer",
    "CommandResult",
    "DirectorCommand",
    "validate_command",
    "validate_issuance",
]
