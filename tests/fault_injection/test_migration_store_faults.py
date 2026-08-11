"""P4-18 migration store fault injection: half-write tmp, corruption, no lease.

Scenarios follow the shared shape: inject a real on-disk fault (half-written
tmp from an interrupted write, corrupt plan payload, SIGKILL mid write-loop) or
an unauthorized write path, assert fail-closed or recovery, then clean up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import STORE_WRITER_CHILD, hard_kill, read_line, reap, spawn_python

from arena_hero_agent.adapters.runtime.process_leases import FileWriterLeaseCoordinator
from arena_hero_agent.domain import DeadlineBudget, Generation, TenantId
from arena_hero_agent.migration.plan import MigrationPlanV1
from arena_hero_agent.migration.store import (
    CorruptPlanError,
    MigrationPlanStore,
    UnauthorizedPlanWrite,
)

TENANT = TenantId("t1")
GENERATION = Generation(1)
BUDGET = DeadlineBudget(1)
STORE_LEASE_NS = 300_000_000


class ManualWallClock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now

    def advance(self, nanoseconds: int) -> None:
        self.now += nanoseconds


async def _seed_plan(
    root: Path,
    make_plan: Callable[..., MigrationPlanV1],
    *,
    revision: int = 1,
) -> int:
    """Write a valid plan under a real fenced lease and release it."""
    clock = ManualWallClock()
    coordinator = FileWriterLeaseCoordinator(
        root, lease_duration_ns=STORE_LEASE_NS, holder_id="seed", wall_clock=clock
    )
    handle = await coordinator.acquire_writer(TENANT, GENERATION, BUDGET)
    assert handle is not None
    store = MigrationPlanStore(root)
    store.write_plan(
        make_plan(revision=revision, conductor_epoch=handle.fencing_token.value),
        lease=handle,
    )
    await handle.release()
    return handle.fencing_token.value


@pytest.mark.parametrize(
    "partial_body",
    [
        '{"operationId": "op-fault-01",',
        "{ not json",
        "",
    ],
)
def test_half_written_tmp_is_never_promoted_on_restart(
    tmp_path: Path, make_plan: Callable[..., MigrationPlanV1], partial_body: str
) -> None:
    """A tmp file left by an interrupted write is ignored, never promoted."""
    asyncio.run(_seed_plan(tmp_path, make_plan))
    target = MigrationPlanStore(tmp_path).plan_path(TENANT.value)
    partial = target.with_name(f".{TENANT.value}.json.crashed.tmp")
    partial.write_text(partial_body, encoding="utf-8")

    restarted = MigrationPlanStore(tmp_path)
    plan = restarted.read_plan(TENANT.value)
    assert plan is not None
    assert plan.operation_id == "op-fault-01"
    assert plan.revision == 1
    # The half-written tmp is still present for forensics, untouched.
    assert partial.exists()
    assert partial.read_text(encoding="utf-8") == partial_body


@pytest.mark.parametrize(
    "corrupt_payload",
    [
        "{ not json",
        '{"schema": "other"}',
        '{"schemaVersion": 99}',
        "[]",
        '{"schema": "migration-plan-v1", "operationId": "x"}',
    ],
)
def test_corrupt_plan_fails_closed_and_is_never_overwritten(
    tmp_path: Path, corrupt_payload: str
) -> None:
    """Corruption must raise and leave the damaged file untouched."""
    store = MigrationPlanStore(tmp_path)
    path = store.plan_path(TENANT.value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(corrupt_payload, encoding="utf-8")

    with pytest.raises(CorruptPlanError):
        store.read_plan(TENANT.value)
    # Fail-closed: the damaged plan is never silently dropped or overwritten.
    assert path.read_text(encoding="utf-8") == corrupt_payload
    with pytest.raises(CorruptPlanError):
        store.read_plan(TENANT.value)


async def _test_write_without_lease(
    tmp_path: Path, make_plan: Callable[..., MigrationPlanV1]
) -> None:
    store = MigrationPlanStore(tmp_path)
    coordinator = FileWriterLeaseCoordinator(
        tmp_path, lease_duration_ns=STORE_LEASE_NS, holder_id="writer"
    )
    handle = await coordinator.acquire_writer(TENANT, GENERATION, BUDGET)
    assert handle is not None
    plan = make_plan(conductor_epoch=handle.fencing_token.value)

    # None lease: rejected.
    with pytest.raises(UnauthorizedPlanWrite):
        store.write_plan(plan, lease=None)
    with pytest.raises(UnauthorizedPlanWrite):
        store.delete_plan(TENANT.value, lease=None)

    # Released (stale) lease: rejected even though it once owned the tenant.
    await handle.release()
    with pytest.raises(UnauthorizedPlanWrite):
        store.write_plan(plan, lease=handle)
    with pytest.raises(UnauthorizedPlanWrite):
        store.delete_plan(TENANT.value, lease=handle)

    # Wrong-epoch plan under an active lease: rejected (fence mismatch).
    fresh = await coordinator.replace_writer(
        TENANT,
        Generation(2),
        expected_fencing_token=handle.fencing_token,
        budget=BUDGET,
    )
    assert fresh is not None
    try:
        with pytest.raises(UnauthorizedPlanWrite):
            store.write_plan(make_plan(conductor_epoch=999), lease=fresh)
    finally:
        await fresh.release()

    # Wrong-tenant lease: rejected for this tenant's plan.
    other = await coordinator.acquire_writer(TenantId("t2"), Generation(1), BUDGET)
    assert other is not None
    try:
        with pytest.raises(UnauthorizedPlanWrite):
            store.write_plan(make_plan(conductor_epoch=other.fencing_token.value), lease=other)
    finally:
        await other.release()


def test_write_without_valid_lease_fails_closed(
    tmp_path: Path, make_plan: Callable[..., MigrationPlanV1]
) -> None:
    asyncio.run(_test_write_without_lease(tmp_path, make_plan))


def test_sigkill_mid_write_loop_keeps_a_valid_plan(
    tmp_path: Path, make_plan: Callable[..., MigrationPlanV1]
) -> None:
    """SIGKILL the real writer mid write-loop; the plan file is never torn."""
    observed_fence = asyncio.run(_seed_plan(tmp_path, make_plan))
    process = spawn_python(
        STORE_WRITER_CHILD,
        str(tmp_path),
        TENANT.value,
        str(STORE_LEASE_NS),
        str(observed_fence),
    )
    try:
        wrote = read_line(process)
        assert wrote.startswith("wrote:")
        hard_kill(process)
    finally:
        reap(process)

    restarted = MigrationPlanStore(tmp_path)
    plan = restarted.read_plan(TENANT.value)
    assert plan is not None
    # Either the seed plan or a later complete revision; never a torn plan.
    assert plan.operation_id == "op-fault-01"
    assert plan.revision >= 1
    assert plan.conductor_epoch >= 1
