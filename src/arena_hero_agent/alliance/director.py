"""Director-side command issuance; the director never writes tenant state.

P4-16 "State ownership": a director publishes versioned commands into the
command ledger and records every issuance outcome in the audit trail. It never
touches tenant partitions (applied markers, journals, or snapshots) — those
belong to the receiving tenant runtime behind its fenced writer lease.
"""

from __future__ import annotations

from arena_hero_agent.alliance.commands import (
    CommandAuditEvent,
    CommandDisposition,
    CommandIssuer,
    DirectorCommand,
    validate_issuance,
)
from arena_hero_agent.ports import CommandAudit, CommandLedger


class Director:
    """Publish versioned commands and audit every issuance outcome fail-closed."""

    def __init__(self, *, ledger: CommandLedger, audit: CommandAudit) -> None:
        self._ledger = ledger
        self._audit = audit

    async def issue(self, command: DirectorCommand) -> CommandDisposition:
        """Publish a command, or audit the rejection and publish nothing."""
        reason = validate_issuance(command)
        if reason is not None:
            disposition = (
                CommandDisposition.UNAUTHORIZED
                if command.issuer is CommandIssuer.AGENT
                else CommandDisposition.REJECTED
            )
            await self._audit.record(
                command.tenant_id,
                event=CommandAuditEvent(
                    command_id=command.command_id,
                    tenant_id=command.tenant_id,
                    disposition=disposition,
                    reason=reason,
                    generation=command.expected_generation,
                    tick=command.issued_at_tick,
                ),
            )
            return disposition
        await self._ledger.publish(command)
        await self._audit.record(
            command.tenant_id,
            event=CommandAuditEvent(
                command_id=command.command_id,
                tenant_id=command.tenant_id,
                disposition=CommandDisposition.ACCEPTED,
                reason=None,
                generation=command.expected_generation,
                tick=command.issued_at_tick,
            ),
        )
        return CommandDisposition.ACCEPTED


__all__ = ["Director"]
