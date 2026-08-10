"""Cross-tenant command and read-projection boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, TypeVar, runtime_checkable

from arena_hero_agent.domain import TenantId

CommandT = TypeVar("CommandT")
SnapshotT_co = TypeVar("SnapshotT_co", covariant=True)


@runtime_checkable
class CommandBus(Protocol[CommandT]):
    """Publish and receive versioned commands; it does not mutate tenant state."""

    async def publish(self, command: CommandT) -> None: ...

    def receive(self, tenant_id: TenantId) -> AsyncIterator[CommandT]: ...


@runtime_checkable
class SnapshotReader(Protocol[SnapshotT_co]):
    """Read immutable tenant projections without writer authority."""

    async def read(self, tenant_id: TenantId) -> SnapshotT_co | None: ...
