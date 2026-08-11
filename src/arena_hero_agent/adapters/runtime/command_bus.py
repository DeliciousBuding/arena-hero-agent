"""Cross-process director command bus: durable ledger, applied markers, and audit.

P4-16 builds the director command bus on the P4-15 fenced writer lease pattern.
The director appends versioned commands to a per-tenant append-only ledger; the
tenant replays pending commands, validates each envelope fail-closed, and
records applied markers under a fenced writer lease. The director never touches
tenant command state (applied markers), and the tenant never accepts a command
that is unsupported, unauthorized, expired, stale, or already applied.

Layout under a validated data root::

    command-bus/<tenant>/commands.jsonl   director appends (one envelope per line)
    command-bus/<tenant>/applied.json     tenant state, versioned, atomic rewrite
    command-bus/<tenant>/audit.jsonl      append-only lifecycle audit

Failure semantics follow the telemetry/oracle precedent: the command and
applied-marker paths fail closed (a malformed or unsupported record raises), a
torn tail left by an interrupted append is treated as a non-committed line and
skipped on read, and the audit trail is fail-open so a broken audit sink never
blocks command processing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from arena_hero_agent.adapters.recorder._common import validate_data_root
from arena_hero_agent.alliance.commands import (
    CommandAuditEvent,
    CommandBusError,
    DirectorCommand,
)
from arena_hero_agent.domain import Generation, TenantId
from arena_hero_agent.ports.leases import LeaseDisposition, WriterLeaseHandle
from arena_hero_agent.telemetry import JsonlRotationOptions, JsonlWriterError, append_jsonl_line

_APPLIED_SCHEMA_VERSION = 1
_COMMANDS_FILENAME = "commands.jsonl"
_APPLIED_FILENAME = "applied.json"
_AUDIT_FILENAME = "audit.jsonl"
# Commands are rare coordination events; 1 GiB without backups means rotation
# never engages in practice while keeping the append primitive's path hardening.
_NO_ROTATION = JsonlRotationOptions(max_bytes=1 << 30, max_backups=0)


@dataclass(frozen=True, slots=True)
class _AppliedEntry:
    command_id: str
    idempotency_key: str
    generation: int
    applied_at_tick: int

    def to_json_object(self) -> dict[str, object]:
        return {
            "commandId": self.command_id,
            "idempotencyKey": self.idempotency_key,
            "generation": self.generation,
            "appliedAtTick": self.applied_at_tick,
        }


@dataclass(frozen=True, slots=True)
class _AppliedRecord:
    schema_version: int
    tenant_id: str
    entries: tuple[_AppliedEntry, ...]

    def to_json_object(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "tenantId": self.tenant_id,
            "applied": [entry.to_json_object() for entry in self.entries],
        }


def _read_applied(path: Path) -> _AppliedRecord:
    """Read the versioned applied record; a missing file is a fresh tenant.

    A present-but-unreadable or unsupported record fails closed instead of
    guessing which commands the tenant already applied.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _AppliedRecord(schema_version=_APPLIED_SCHEMA_VERSION, tenant_id="", entries=())
    except OSError as error:
        raise CommandBusError(f"applied command record is unreadable: {path}") from error
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise CommandBusError(f"applied command record is malformed: {path}") from error
    if not isinstance(payload, dict):
        raise CommandBusError(f"applied command record is malformed: {path}")
    try:
        if payload.get("schemaVersion") != _APPLIED_SCHEMA_VERSION:
            raise CommandBusError(f"applied command record has an unsupported schema: {path}")
        tenant_id = payload["tenantId"]
        raw_entries = payload["applied"]
        if not isinstance(tenant_id, str) or not isinstance(raw_entries, list):
            raise CommandBusError(f"applied command record is malformed: {path}")
        entries: list[_AppliedEntry] = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise CommandBusError(f"applied command record is malformed: {path}")
            command_id = raw["commandId"]
            idempotency_key = raw["idempotencyKey"]
            generation = raw["generation"]
            applied_at_tick = raw["appliedAtTick"]
            if (
                not isinstance(command_id, str)
                or not isinstance(idempotency_key, str)
                or isinstance(generation, bool)
                or not isinstance(generation, int)
                or isinstance(applied_at_tick, bool)
                or not isinstance(applied_at_tick, int)
            ):
                raise CommandBusError(f"applied command record is malformed: {path}")
            entries.append(
                _AppliedEntry(
                    command_id=command_id,
                    idempotency_key=idempotency_key,
                    generation=generation,
                    applied_at_tick=applied_at_tick,
                )
            )
    except KeyError as error:
        raise CommandBusError(f"applied command record is malformed: {path}") from error
    return _AppliedRecord(
        schema_version=_APPLIED_SCHEMA_VERSION,
        tenant_id=tenant_id,
        entries=tuple(entries),
    )


def _write_applied(path: Path, record: _AppliedRecord) -> None:
    """Atomically persist the applied record: temp file, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(record.to_json_object(), sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _read_audit_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


class FileCommandBus:
    """Durable per-tenant command ledger, applied markers, and lifecycle audit."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = validate_data_root(root)
        self._lock = asyncio.Lock()

    def _tenant_dir(self, tenant_id: TenantId) -> Path:
        return self._root / "command-bus" / tenant_id.value

    def _commands_path(self, tenant_id: TenantId) -> Path:
        return self._tenant_dir(tenant_id) / _COMMANDS_FILENAME

    def _applied_path(self, tenant_id: TenantId) -> Path:
        return self._tenant_dir(tenant_id) / _APPLIED_FILENAME

    def _audit_path(self, tenant_id: TenantId) -> Path:
        return self._tenant_dir(tenant_id) / _AUDIT_FILENAME

    # ------------------------------------------------------------------
    # Command ledger (director writes, tenant replays)
    # ------------------------------------------------------------------

    async def publish(self, command: DirectorCommand) -> None:
        self._tenant_dir(command.tenant_id).mkdir(parents=True, exist_ok=True)
        try:
            append_jsonl_line(
                self._commands_path(command.tenant_id),
                json.dumps(command.to_json_object(), sort_keys=True),
                rotation=_NO_ROTATION,
            )
        except JsonlWriterError as error:
            raise CommandBusError("command publish failed") from error

    def pending(self, tenant_id: TenantId) -> AsyncIterator[DirectorCommand]:
        return self._pending(tenant_id)

    async def _pending(self, tenant_id: TenantId) -> AsyncIterator[DirectorCommand]:
        applied = _read_applied(self._applied_path(tenant_id))
        if applied.tenant_id != "" and applied.tenant_id != tenant_id.value:
            raise CommandBusError("applied command record crossed tenant partition")
        applied_ids = {entry.command_id for entry in applied.entries}
        applied_keys = {entry.idempotency_key for entry in applied.entries}
        for line in self._read_command_lines(tenant_id):
            command = self._decode_command(line)
            if command.command_id.value in applied_ids or command.idempotency_key in applied_keys:
                continue
            yield command

    def _read_command_lines(self, tenant_id: TenantId) -> list[str]:
        path = self._commands_path(tenant_id)
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise CommandBusError(f"command ledger is unreadable: {path}") from error
        lines = text.splitlines()
        # A file not ending in a newline has a torn tail from an interrupted
        # append; that line was never durably committed and is skipped.
        if text and not text.endswith("\n"):
            lines = lines[:-1]
        return [line for line in lines if line.strip()]

    @staticmethod
    def _decode_command(line: str) -> DirectorCommand:
        try:
            payload = json.loads(line)
        except ValueError as error:
            raise CommandBusError("command ledger line is not valid JSON") from error
        try:
            return DirectorCommand.from_json_object(payload)
        except (TypeError, ValueError) as error:
            raise CommandBusError("command ledger line is malformed") from error

    # ------------------------------------------------------------------
    # Applied markers (tenant writes under a fenced writer lease)
    # ------------------------------------------------------------------

    async def is_applied(self, tenant_id: TenantId, *, command: DirectorCommand) -> bool:
        record = _read_applied(self._applied_path(tenant_id))
        self._require_record_tenant(record, tenant_id)
        return command.command_id.value in {
            entry.command_id for entry in record.entries
        } or command.idempotency_key in {entry.idempotency_key for entry in record.entries}

    async def mark_applied(
        self,
        tenant_id: TenantId,
        *,
        command: DirectorCommand,
        generation: Generation,
        applied_at_tick: int,
        lease: WriterLeaseHandle,
    ) -> bool:
        """Record one command as applied; ``False`` when it was already applied.

        The write is fenced: a non-``ACTIVE`` lease or a lease for another
        tenant fails closed, and the read-modify-write of the versioned record
        is serialized per process.
        """
        if lease.disposition is not LeaseDisposition.ACTIVE:
            raise CommandBusError("applied marker requires an active writer lease")
        if lease.tenant_id != tenant_id:
            raise CommandBusError("applied marker lease targets another tenant")
        if not isinstance(generation, Generation):
            raise TypeError("generation must be a Generation")
        if isinstance(applied_at_tick, bool) or not isinstance(applied_at_tick, int):
            raise TypeError("applied_at_tick must be an integer")
        if applied_at_tick < 0:
            raise ValueError("applied_at_tick cannot be negative")
        async with self._lock:
            record = _read_applied(self._applied_path(tenant_id))
            self._require_record_tenant(record, tenant_id)
            applied_ids = {entry.command_id for entry in record.entries}
            applied_keys = {entry.idempotency_key for entry in record.entries}
            if command.command_id.value in applied_ids or command.idempotency_key in applied_keys:
                return False
            entry = _AppliedEntry(
                command_id=command.command_id.value,
                idempotency_key=command.idempotency_key,
                generation=generation.value,
                applied_at_tick=applied_at_tick,
            )
            updated = _AppliedRecord(
                schema_version=_APPLIED_SCHEMA_VERSION,
                tenant_id=tenant_id.value,
                entries=record.entries + (entry,),
            )
            _write_applied(self._applied_path(tenant_id), updated)
            return True

    @staticmethod
    def _require_record_tenant(record: _AppliedRecord, tenant_id: TenantId) -> None:
        if record.tenant_id != "" and record.tenant_id != tenant_id.value:
            raise CommandBusError("applied command record crossed tenant partition")

    # ------------------------------------------------------------------
    # Lifecycle audit (append-only, fail-open)
    # ------------------------------------------------------------------

    async def record(self, tenant_id: TenantId, *, event: CommandAuditEvent) -> None:
        if event.tenant_id != tenant_id:
            raise CommandBusError("audit event targets another tenant")
        self._tenant_dir(tenant_id).mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(JsonlWriterError):
            # Audit is best-effort and must never block command processing
            # (TypeScript oracle parity: audit failures do not block dispatch).
            append_jsonl_line(
                self._audit_path(tenant_id),
                json.dumps(event.to_json_object(), sort_keys=True),
                rotation=_NO_ROTATION,
            )

    def read_audit(self, tenant_id: TenantId, *, limit: int = 50) -> list[CommandAuditEvent]:
        """Return the trailing audit events for a tenant, oldest first."""
        lines = _read_audit_lines(self._audit_path(tenant_id))
        if limit >= 0:
            lines = lines[-limit:]
        events: list[CommandAuditEvent] = []
        for line in lines:
            try:
                payload = json.loads(line)
                events.append(CommandAuditEvent.from_json_object(payload))
            except (TypeError, ValueError):
                continue
        return events

    # ------------------------------------------------------------------
    # CommandBus transport conformance
    # ------------------------------------------------------------------

    def receive(self, tenant_id: TenantId) -> AsyncIterator[DirectorCommand]:
        return self.pending(tenant_id)


__all__ = ["FileCommandBus"]
