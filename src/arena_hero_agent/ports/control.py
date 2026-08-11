"""Cross-tenant command and read-projection boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

from arena_hero_agent.domain import Generation, TenantId
from arena_hero_agent.ports.leases import WriterLeaseHandle

if TYPE_CHECKING:
    from arena_hero_agent.alliance.commands import CommandAuditEvent, DirectorCommand

CommandT = TypeVar("CommandT")
SnapshotT_co = TypeVar("SnapshotT_co", covariant=True)


@runtime_checkable
class CommandBus(Protocol[CommandT]):
    """Publish and receive versioned commands; it does not mutate tenant state."""

    async def publish(self, command: CommandT) -> None: ...

    def receive(self, tenant_id: TenantId) -> AsyncIterator[CommandT]: ...


@runtime_checkable
class CommandLedger(Protocol):
    """Durable director-command store; the tenant side is restart-safe."""

    async def publish(self, command: DirectorCommand) -> None: ...

    def pending(self, tenant_id: TenantId) -> AsyncIterator[DirectorCommand]:
        """Yield commands not yet applied, in issue order."""
        ...

    async def is_applied(self, tenant_id: TenantId, *, command: DirectorCommand) -> bool: ...

    async def mark_applied(
        self,
        tenant_id: TenantId,
        *,
        command: DirectorCommand,
        generation: Generation,
        applied_at_tick: int,
        lease: WriterLeaseHandle,
    ) -> bool:
        """Record a command as applied; ``False`` when it was already applied."""
        ...


@runtime_checkable
class CommandAudit(Protocol):
    """Append-only lifecycle audit for director commands."""

    async def record(self, tenant_id: TenantId, *, event: CommandAuditEvent) -> None: ...


@runtime_checkable
class SnapshotReader(Protocol[SnapshotT_co]):
    """Read immutable tenant projections without writer authority."""

    async def read(self, tenant_id: TenantId) -> SnapshotT_co | None: ...
