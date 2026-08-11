"""Tenant-side command application state machine; restart-safe and fail-closed.

The receiving tenant replays pending commands from the durable ledger, validates
each envelope fail-closed (unsupported version, wrong tenant, unauthorized
issuer, expiry, stale generation, already applied), and only then applies the
command under one fenced writer lease. Applied markers are durable, so a
restart never double-applies and an interrupted append is recovered. Every
disposition is recorded in the append-only audit trail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from arena_hero_agent.alliance.commands import (
    CommandAuditEvent,
    CommandBusError,
    CommandDisposition,
    CommandResult,
    DirectorCommand,
    validate_command,
)
from arena_hero_agent.domain import DeadlineBudget, Generation, TenantId
from arena_hero_agent.ports import CommandAudit, CommandLedger, WriterLease
from arena_hero_agent.ports.leases import WriterLeaseHandle


class CommandLeaseUnavailable(CommandBusError):
    """Fail-closed when the tenant writer lease cannot be acquired for a drain."""


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Tenant-side revision facts used to validate pending commands."""

    tenant_id: TenantId
    generation: Generation


class TenantCommandApplier:
    """Validate, apply, or reject pending director commands for one tenant."""

    def __init__(
        self,
        *,
        ledger: CommandLedger,
        audit: CommandAudit,
        writer_leases: WriterLease,
        now: Callable[[], int],
    ) -> None:
        self._ledger = ledger
        self._audit = audit
        self._writer_leases = writer_leases
        self._now = now

    async def drain(
        self,
        *,
        context: CommandContext,
        budget: DeadlineBudget,
    ) -> tuple[CommandResult, ...]:
        """Process every pending command under one fenced writer lease.

        A command list that cannot be locked fails closed by raising, so the
        caller can retry with a fresh budget; nothing is applied and nothing is
        silently dropped.
        """
        pending = [command async for command in self._ledger.pending(context.tenant_id)]
        if not pending:
            return ()
        lease = await self._writer_leases.acquire_writer(
            context.tenant_id,
            context.generation,
            budget,
        )
        if lease is None:
            raise CommandLeaseUnavailable(
                f"writer lease unavailable for tenant {context.tenant_id.value}"
            )
        try:
            results = [await self._handle(command, context, lease) for command in pending]
            return tuple(results)
        finally:
            await lease.release()

    async def _handle(
        self,
        command: DirectorCommand,
        context: CommandContext,
        lease: WriterLeaseHandle,
    ) -> CommandResult:
        observed_tick = self._now()
        disposition, reason = validate_command(
            command,
            tenant_id=context.tenant_id,
            current_generation=context.generation,
            now=observed_tick,
        )
        if disposition is not CommandDisposition.ACCEPTED:
            await self._record(context, command, disposition, reason, observed_tick)
            return CommandResult(command=command, disposition=disposition, reason=reason)

        if await self._ledger.is_applied(context.tenant_id, command=command):
            return await self._duplicate(context, command)

        # Authoritative re-checks under the fenced writer lease: the command may
        # have expired or been applied by a concurrent writer since the cheap
        # pre-checks above. A fresh tick prevents an expiry race from applying.
        fresh_tick = self._now()
        if fresh_tick >= command.expires_at_tick:
            await self._record(
                context, command, CommandDisposition.EXPIRED, "command has expired", fresh_tick
            )
            return CommandResult(
                command=command,
                disposition=CommandDisposition.EXPIRED,
                reason="command has expired",
            )
        if await self._ledger.is_applied(context.tenant_id, command=command):
            return await self._duplicate(context, command)

        applied = await self._ledger.mark_applied(
            context.tenant_id,
            command=command,
            generation=context.generation,
            applied_at_tick=fresh_tick,
            lease=lease,
        )
        if not applied:
            return await self._duplicate(context, command)
        await self._record(context, command, CommandDisposition.APPLIED, None, fresh_tick)
        return CommandResult(command=command, disposition=CommandDisposition.APPLIED, reason=None)

    async def _duplicate(
        self,
        context: CommandContext,
        command: DirectorCommand,
    ) -> CommandResult:
        reason = "command was already applied"
        await self._record(context, command, CommandDisposition.DUPLICATE, reason, self._now())
        return CommandResult(
            command=command,
            disposition=CommandDisposition.DUPLICATE,
            reason=reason,
        )

    async def _record(
        self,
        context: CommandContext,
        command: DirectorCommand,
        disposition: CommandDisposition,
        reason: str | None,
        tick: int,
    ) -> None:
        await self._audit.record(
            context.tenant_id,
            event=CommandAuditEvent(
                command_id=command.command_id,
                tenant_id=context.tenant_id,
                disposition=disposition,
                reason=reason,
                generation=context.generation,
                tick=tick,
            ),
        )


__all__ = [
    "CommandContext",
    "CommandLeaseUnavailable",
    "TenantCommandApplier",
]
