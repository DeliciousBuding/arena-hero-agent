"""Purpose-specific lease boundaries for decisions, durable writers, and migrations."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from arena_hero_agent.domain import (
    DeadlineBudget,
    DecisionId,
    FencingToken,
    Generation,
    TenantId,
)


class LeaseDisposition(StrEnum):
    """Observable lifecycle state for an acquired lease handle."""

    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"
    REPLACED = "replaced"


@runtime_checkable
class LeaseHandle(Protocol):
    """Common lifecycle exposed by an acquired purpose-specific lease handle."""

    @property
    def tenant_id(self) -> TenantId: ...

    @property
    def fencing_token(self) -> FencingToken: ...

    @property
    def disposition(self) -> LeaseDisposition: ...

    async def renew(self, budget: DeadlineBudget) -> bool:
        """Extend the lease if this handle still owns the current fence."""
        ...

    async def release(self) -> None:
        """Release this handle idempotently."""
        ...


@runtime_checkable
class DecisionLeaseHandle(LeaseHandle, Protocol):
    """Handle scoped to one deterministic decision."""

    @property
    def decision_id(self) -> DecisionId: ...


@runtime_checkable
class WriterLeaseHandle(LeaseHandle, Protocol):
    """Handle scoped to durable writes for one tenant generation."""

    @property
    def generation(self) -> Generation: ...


@runtime_checkable
class MigrationLeaseHandle(LeaseHandle, Protocol):
    """Handle scoped to a transition toward a target generation."""

    @property
    def target_generation(self) -> Generation: ...


@runtime_checkable
class DecisionLease(Protocol):
    """Acquire decision-scoped exclusion without granting durable writer authority."""

    async def acquire_decision(
        self,
        tenant_id: TenantId,
        generation: Generation,
        decision_id: DecisionId,
        budget: DeadlineBudget,
    ) -> DecisionLeaseHandle | None: ...

    async def replace_decision(
        self,
        tenant_id: TenantId,
        generation: Generation,
        decision_id: DecisionId,
        *,
        expected_fencing_token: FencingToken,
        budget: DeadlineBudget,
    ) -> DecisionLeaseHandle | None:
        """Replace only the expired holder identified by the exact observed fence."""
        ...


@runtime_checkable
class WriterLease(Protocol):
    """Acquire tenant writer authority with a monotonically increasing fence."""

    async def acquire_writer(
        self,
        tenant_id: TenantId,
        generation: Generation,
        budget: DeadlineBudget,
    ) -> WriterLeaseHandle | None: ...

    async def replace_writer(
        self,
        tenant_id: TenantId,
        generation: Generation,
        *,
        expected_fencing_token: FencingToken,
        budget: DeadlineBudget,
    ) -> WriterLeaseHandle | None:
        """Replace only the expired holder identified by the exact observed fence."""
        ...


@runtime_checkable
class MigrationLease(Protocol):
    """Acquire migration authority without implicitly acquiring decision authority."""

    async def acquire_migration(
        self,
        tenant_id: TenantId,
        target_generation: Generation,
        budget: DeadlineBudget,
    ) -> MigrationLeaseHandle | None: ...
