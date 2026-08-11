from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from arena_hero import Accepted, AsyncGameEvent, CommandPlan

from arena_hero_agent.domain import (
    DeadlineBudget,
    DecisionId,
    FencingToken,
    Generation,
    StateDigest,
    TenantId,
)
from arena_hero_agent.ports import (
    Clock,
    CommandBus,
    DecisionLease,
    DecisionLeaseHandle,
    DecisionRecorder,
    EventJournal,
    GameClient,
    HealthReporter,
    LeaseDisposition,
    LeaseHandle,
    MigrationLease,
    MigrationLeaseHandle,
    SnapshotReader,
    TelemetrySink,
    TenantStateStore,
    WriterLease,
    WriterLeaseHandle,
)

TENANT = TenantId("sample")
DECISION = DecisionId("decision:sample")
GENERATION = Generation(1)
FENCE = FencingToken(1)
BUDGET = DeadlineBudget(1)


class FakeClock:
    def monotonic_ns(self) -> int:
        return 1


@dataclass
class _BaseHandle:
    tenant_id: TenantId = TENANT
    fencing_token: FencingToken = FENCE
    disposition: LeaseDisposition = LeaseDisposition.ACTIVE

    async def renew(self, budget: DeadlineBudget) -> bool:
        return not budget.exhausted

    async def release(self) -> None:
        return None


@dataclass
class FakeDecisionHandle(_BaseHandle):
    decision_id: DecisionId = DECISION


@dataclass
class FakeWriterHandle(_BaseHandle):
    generation: Generation = GENERATION


@dataclass
class FakeMigrationHandle(_BaseHandle):
    target_generation: Generation = GENERATION


class FakeDecisionLease:
    async def acquire_decision(
        self,
        tenant_id: TenantId,
        generation: Generation,
        decision_id: DecisionId,
        budget: DeadlineBudget,
    ) -> DecisionLeaseHandle | None:
        return FakeDecisionHandle(
            tenant_id=tenant_id,
            decision_id=decision_id,
        )

    async def replace_decision(
        self,
        tenant_id: TenantId,
        generation: Generation,
        decision_id: DecisionId,
        *,
        expected_fencing_token: FencingToken,
        budget: DeadlineBudget,
    ) -> DecisionLeaseHandle | None:
        return FakeDecisionHandle(
            tenant_id=tenant_id,
            decision_id=decision_id,
            fencing_token=expected_fencing_token.next(),
        )


class FakeWriterLease:
    async def acquire_writer(
        self,
        tenant_id: TenantId,
        generation: Generation,
        budget: DeadlineBudget,
    ) -> WriterLeaseHandle | None:
        return FakeWriterHandle(tenant_id=tenant_id, generation=generation)

    async def replace_writer(
        self,
        tenant_id: TenantId,
        generation: Generation,
        *,
        expected_fencing_token: FencingToken,
        budget: DeadlineBudget,
    ) -> WriterLeaseHandle | None:
        return FakeWriterHandle(
            tenant_id=tenant_id,
            generation=generation,
            fencing_token=expected_fencing_token.next(),
        )


class FakeMigrationLease:
    async def acquire_migration(
        self,
        tenant_id: TenantId,
        target_generation: Generation,
        budget: DeadlineBudget,
    ) -> MigrationLeaseHandle | None:
        return FakeMigrationHandle(tenant_id=tenant_id, target_generation=target_generation)


async def _empty_events() -> AsyncIterator[AsyncGameEvent]:
    if False:
        yield cast(AsyncGameEvent, object())


class FakeGameClient:
    def events(self) -> AsyncIterator[AsyncGameEvent]:
        return _empty_events()

    async def submit(self, plan: CommandPlan, *, decision_id: DecisionId) -> Accepted:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class FakeStateStore:
    async def load(self, tenant_id: TenantId) -> tuple[Generation, StateDigest, str] | None:
        return None

    async def compare_and_set(
        self,
        tenant_id: TenantId,
        *,
        expected_generation: Generation,
        next_generation: Generation,
        state_digest: StateDigest,
        state: str,
        lease: WriterLeaseHandle,
    ) -> bool:
        return next_generation.supersedes(expected_generation)

    async def restore(
        self,
        tenant_id: TenantId,
        *,
        generation: Generation,
        state_digest: StateDigest,
        state: str,
        lease: WriterLeaseHandle,
    ) -> bool:
        return lease.generation == generation


class FakeEventJournal:
    async def append(
        self,
        tenant_id: TenantId,
        *,
        generation: Generation,
        events: Sequence[str],
        lease: WriterLeaseHandle,
    ) -> int:
        return len(events) - 1

    async def _read(self) -> AsyncIterator[str]:
        if False:
            yield ""

    def read_from(self, tenant_id: TenantId, position: int) -> AsyncIterator[str]:
        return self._read()


class FakeDecisionRecorder:
    async def record(
        self,
        tenant_id: TenantId,
        *,
        decision_id: DecisionId,
        generation: Generation,
        state_digest: StateDigest,
        plan: CommandPlan,
        accepted: Accepted | None,
        lease: DecisionLeaseHandle,
    ) -> None:
        return None


class FakeCommandBus:
    async def publish(self, command: str) -> None:
        return None

    async def _receive(self) -> AsyncIterator[str]:
        if False:
            yield ""

    def receive(self, tenant_id: TenantId) -> AsyncIterator[str]:
        return self._receive()


class FakeSnapshotReader:
    async def read(self, tenant_id: TenantId) -> str | None:
        return None


class FakeTelemetrySink:
    async def emit(self, event: str) -> None:
        return None


class FakeHealthReporter:
    async def report(
        self,
        component: str,
        *,
        healthy: bool,
        message: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        return None


# These assignments are static conformance checks exercised by `ty check`.
clock: Clock = FakeClock()
lease_handle: LeaseHandle = FakeWriterHandle()
decision_handle: DecisionLeaseHandle = FakeDecisionHandle()
writer_handle: WriterLeaseHandle = FakeWriterHandle()
migration_handle: MigrationLeaseHandle = FakeMigrationHandle()
decision_lease: DecisionLease = FakeDecisionLease()
writer_lease: WriterLease = FakeWriterLease()
migration_lease: MigrationLease = FakeMigrationLease()
game_client: GameClient = FakeGameClient()
state_store: TenantStateStore[str] = FakeStateStore()
event_journal: EventJournal[str] = FakeEventJournal()
decision_recorder: DecisionRecorder = FakeDecisionRecorder()
command_bus: CommandBus[str] = FakeCommandBus()
snapshot_reader: SnapshotReader[str] = FakeSnapshotReader()
telemetry_sink: TelemetrySink[str] = FakeTelemetrySink()
health_reporter: HealthReporter = FakeHealthReporter()


def test_public_protocols_support_runtime_adapter_smoke_checks() -> None:
    checks = [
        (clock, Clock),
        (lease_handle, LeaseHandle),
        (decision_handle, DecisionLeaseHandle),
        (writer_handle, WriterLeaseHandle),
        (migration_handle, MigrationLeaseHandle),
        (decision_lease, DecisionLease),
        (writer_lease, WriterLease),
        (migration_lease, MigrationLease),
        (game_client, GameClient),
        (state_store, TenantStateStore),
        (event_journal, EventJournal),
        (decision_recorder, DecisionRecorder),
        (command_bus, CommandBus),
        (snapshot_reader, SnapshotReader),
        (telemetry_sink, TelemetrySink),
        (health_reporter, HealthReporter),
    ]

    assert all(isinstance(candidate, protocol) for candidate, protocol in checks)


def test_purpose_specific_lease_interfaces_do_not_collapse_authority() -> None:
    assert not isinstance(decision_lease, WriterLease)
    assert not isinstance(writer_lease, MigrationLease)
    assert not isinstance(migration_lease, DecisionLease)
    assert not isinstance(decision_handle, WriterLeaseHandle)
    assert not isinstance(writer_handle, MigrationLeaseHandle)
    assert not isinstance(migration_handle, DecisionLeaseHandle)
