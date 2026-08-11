"""Migration plan persistence and restart recovery tests (migration-system-v1 §6).

Atomic tmp+rename writes, corruption fail-closed (never silently dropped),
ABORT cleanup, and crash recovery: plan retained + lease expiry fails closed;
resume is only allowed for the same operation (operationId+revision+epoch) and
never from a RECOVERY_ABORT legProgress.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from arena_hero_agent.adapters.runtime import MemoryLeaseCoordinator
from arena_hero_agent.domain import DeadlineBudget, Generation, TenantId
from arena_hero_agent.migration.enactment import take_over_conductor_fence
from arena_hero_agent.migration.plan import (
    MigrationLease,
    MigrationPlanV1,
)
from arena_hero_agent.migration.state_machine import (
    MigrationEvent,
    MigrationEventType,
    MigrationState,
    transition,
)
from arena_hero_agent.migration.store import (
    CorruptPlanError,
    MigrationPlanStore,
    RecoveryOutcome,
    UnauthorizedPlanWrite,
    evaluate_recovery,
    recovery_blocks_resume,
    resume_continuation_allowed,
)

NOW = datetime(2026, 8, 8, 21, 30, 0, tzinfo=UTC)
HEARTBEAT = "2026-08-08T21:30:00.000Z"
TTL_SECONDS = 60

TENANT = TenantId("t1")
GENERATION = Generation(1)
BUDGET = DeadlineBudget(1)


class ManualClock:
    def __init__(self) -> None:
        self.now = 0

    def monotonic_ns(self) -> int:
        return self.now

    def advance(self, nanoseconds: int) -> None:
        self.now += nanoseconds


def fresh_lease() -> MigrationLease:
    return MigrationLease(until_tick=74_123, heartbeat_at=HEARTBEAT)


async def writer_handle():
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    handle = await leases.acquire_writer(TENANT, GENERATION, BUDGET)
    assert handle is not None
    return clock, leases, handle


async def test_write_read_round_trip(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    _, _, handle = await writer_handle()
    store = MigrationPlanStore(tmp_path)
    plan = make_plan(conductor_epoch=handle.fencing_token.value)
    store.write_plan(plan, lease=handle)
    assert store.read_plan(TENANT.value) == plan


async def test_write_is_atomic_and_leaves_no_tmp_files(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    _, _, handle = await writer_handle()
    store = MigrationPlanStore(tmp_path)
    store.write_plan(make_plan(conductor_epoch=handle.fencing_token.value), lease=handle)
    store.write_plan(
        make_plan(revision=2, conductor_epoch=handle.fencing_token.value),
        lease=handle,
    )
    leftovers = [
        path.name
        for path in store.plan_path(TENANT.value).parent.iterdir()
        if path.name != f"{TENANT.value}.json"
    ]
    assert leftovers == []


async def test_crash_mid_write_restart_recovers_original_plan(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    _, _, handle = await writer_handle()
    store = MigrationPlanStore(tmp_path)
    plan = make_plan(conductor_epoch=handle.fencing_token.value)
    store.write_plan(plan, lease=handle)

    # 模拟崩溃：同目录残留半截 tmp 文件（rename 未发生）。
    partial = store.plan_path(TENANT.value).with_name(f".{TENANT.value}.json.crashed.tmp")
    partial.write_text('{"operationId": "op-test-01",', encoding="utf-8")

    # 重启：新 store 实例只读主文件；残留 tmp 不被提升，原计划完整可恢复。
    restarted = MigrationPlanStore(tmp_path)
    assert restarted.read_plan(TENANT.value) == plan
    assert partial.exists()


def test_corrupt_plan_fails_closed_and_is_never_overwritten(
    tmp_path,
) -> None:
    store = MigrationPlanStore(tmp_path)
    path = store.plan_path(TENANT.value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(CorruptPlanError):
        store.read_plan(TENANT.value)
    # 损坏文件不得静默丢弃/覆盖。
    assert path.read_text(encoding="utf-8") == "{ not json"
    with pytest.raises(CorruptPlanError):
        store.read_plan(TENANT.value)


def test_schema_mismatch_fails_closed(tmp_path) -> None:
    store = MigrationPlanStore(tmp_path)
    path = store.plan_path(TENANT.value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema": "other", "operationId": "x"}', encoding="utf-8")
    with pytest.raises(CorruptPlanError):
        store.read_plan(TENANT.value)


def test_missing_plan_returns_none(tmp_path) -> None:
    store = MigrationPlanStore(tmp_path)
    assert store.read_plan(TENANT.value) is None


async def test_write_without_lease_is_rejected(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    store = MigrationPlanStore(tmp_path)
    with pytest.raises(UnauthorizedPlanWrite):
        store.write_plan(make_plan(), lease=None)
    assert store.read_plan(TENANT.value) is None


async def test_write_with_wrong_epoch_lease_is_rejected(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    _, _, handle = await writer_handle()
    store = MigrationPlanStore(tmp_path)
    wrong_epoch = make_plan(conductor_epoch=handle.fencing_token.value + 1)
    with pytest.raises(UnauthorizedPlanWrite):
        store.write_plan(wrong_epoch, lease=handle)


async def test_write_with_released_lease_is_rejected(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    _, _, handle = await writer_handle()
    store = MigrationPlanStore(tmp_path)
    plan = make_plan(conductor_epoch=handle.fencing_token.value)
    await handle.release()
    with pytest.raises(UnauthorizedPlanWrite):
        store.write_plan(plan, lease=handle)


async def test_write_with_other_tenant_lease_is_rejected(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    other = await leases.acquire_writer(TenantId("t2"), GENERATION, BUDGET)
    assert other is not None
    store = MigrationPlanStore(tmp_path)
    with pytest.raises(UnauthorizedPlanWrite):
        store.write_plan(make_plan(conductor_epoch=other.fencing_token.value), lease=other)


async def test_delete_plan_requires_fenced_lease_and_is_idempotent(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    _, _, handle = await writer_handle()
    store = MigrationPlanStore(tmp_path)
    store.write_plan(make_plan(conductor_epoch=handle.fencing_token.value), lease=handle)

    with pytest.raises(UnauthorizedPlanWrite):
        store.delete_plan(TENANT.value, lease=None)
    assert store.read_plan(TENANT.value) is not None

    store.delete_plan(TENANT.value, lease=handle)
    assert store.read_plan(TENANT.value) is None
    store.delete_plan(TENANT.value, lease=handle)  # 幂等


async def test_stale_takeover_blocks_old_conductor_writes_fault_injection(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    """故障注入：stale takeover 后旧 conductor 的写/清被拒，新 epoch 才可写。"""
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    old = await leases.acquire_writer(TENANT, GENERATION, BUDGET)
    assert old is not None
    store = MigrationPlanStore(tmp_path)
    store.write_plan(
        make_plan(conductor_epoch=old.fencing_token.value),
        lease=old,
    )

    clock.advance(100)
    replacement = await take_over_conductor_fence(
        leases,
        TENANT,
        GENERATION,
        expected_fencing_token=old.fencing_token,
        budget=BUDGET,
    )
    assert replacement is not None

    # 旧 conductor：继续写/清理都被拒（无越权 writer）。
    with pytest.raises(UnauthorizedPlanWrite):
        store.write_plan(
            make_plan(conductor_epoch=old.fencing_token.value),
            lease=old,
        )
    with pytest.raises(UnauthorizedPlanWrite):
        store.delete_plan(TENANT.value, lease=old)

    # 新 conductor（epoch 单调 +1）恢复写入权。
    new_plan = make_plan(conductor_epoch=replacement.fencing_token.value)
    store.write_plan(new_plan, lease=replacement)
    assert store.read_plan(TENANT.value) == new_plan


def test_recovery_fresh_lease_same_operation_is_resumable(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    plan = make_plan(
        state=MigrationState.LEG_MOVE,
        lease=fresh_lease(),
        operation_id="op-1",
        revision=2,
        conductor_epoch=3,
    )
    outcome = evaluate_recovery(
        plan,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_operation_id="op-1",
        expected_revision=2,
        expected_epoch=3,
    )
    assert outcome == RecoveryOutcome(can_resume=True, reason="resume-ok")


def test_recovery_no_plan_is_not_resumable() -> None:
    outcome = evaluate_recovery(
        None,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_operation_id="op-1",
        expected_revision=2,
        expected_epoch=3,
    )
    assert outcome == RecoveryOutcome(can_resume=False, reason="no-plan")


def test_recovery_lease_expired_fails_closed_and_plan_retained(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    plan = make_plan(
        state=MigrationState.LEG_MOVE,
        lease=MigrationLease(until_tick=74_123, heartbeat_at=HEARTBEAT),
        operation_id="op-1",
        revision=2,
        conductor_epoch=3,
    )
    outcome = evaluate_recovery(
        plan,
        current_tick=74_124,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_operation_id="op-1",
        expected_revision=2,
        expected_epoch=3,
    )
    assert outcome == RecoveryOutcome(can_resume=False, reason="lease-expired-fail-closed")


def test_recovery_abort_blocks_resume_even_with_fresh_lease_and_matching_identity(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    plan = make_plan(
        state=MigrationState.RECOVERY_ABORT,
        lease=fresh_lease(),
        operation_id="op-1",
        revision=2,
        conductor_epoch=3,
    )
    assert recovery_blocks_resume(plan)
    outcome = evaluate_recovery(
        plan,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_operation_id="op-1",
        expected_revision=2,
        expected_epoch=3,
    )
    assert outcome == RecoveryOutcome(can_resume=False, reason="recovery-abort-blocks-resume")


@pytest.mark.parametrize(
    ("expected_operation_id", "expected_revision", "expected_epoch"),
    [
        ("op-2", 2, 3),
        ("op-1", 1, 3),
        ("op-1", 2, 4),
    ],
)
def test_recovery_identity_mismatch_blocks_resume(
    make_plan: Callable[..., MigrationPlanV1],
    expected_operation_id: str,
    expected_revision: int,
    expected_epoch: int,
) -> None:
    plan = make_plan(
        state=MigrationState.LEG_MOVE,
        lease=fresh_lease(),
        operation_id="op-1",
        revision=2,
        conductor_epoch=3,
    )
    assert resume_continuation_allowed(
        plan,
        expected_operation_id="op-1",
        expected_revision=2,
        expected_epoch=3,
    )
    outcome = evaluate_recovery(
        plan,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_operation_id=expected_operation_id,
        expected_revision=expected_revision,
        expected_epoch=expected_epoch,
    )
    assert outcome == RecoveryOutcome(can_resume=False, reason="operation-identity-mismatch")


async def test_abort_cleanup_flow_removes_plan_then_returns_to_idle(
    tmp_path,
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    """ABORT 两段式清理：状态机 ABORT → 计划删除（fenced）→ CLEANED → IDLE。"""
    _, _, handle = await writer_handle()
    store = MigrationPlanStore(tmp_path)
    store.write_plan(
        make_plan(state=MigrationState.LEG_MOVE, conductor_epoch=handle.fencing_token.value),
        lease=handle,
    )

    state = transition(
        MigrationState.LEG_MOVE,
        MigrationEvent(type=MigrationEventType.CANCEL),
    )
    assert state == MigrationState.ABORT
    store.delete_plan(TENANT.value, lease=handle)
    assert store.read_plan(TENANT.value) is None
    state = transition(state, MigrationEvent(type=MigrationEventType.CLEANED))
    assert state == MigrationState.IDLE
