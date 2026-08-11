"""Runtime enactment contract and conductor fencing tests (migration-system-v1 §6.2).

Fail-closed: lease expiry, epoch mismatch, and coreId mismatch must each block
START_MOVE; a stale takeover must reject the old conductor's orders.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from arena_hero_agent.adapters.runtime import MemoryLeaseCoordinator
from arena_hero_agent.domain import DeadlineBudget, FencingToken, Generation, TenantId
from arena_hero_agent.migration.enactment import (
    acquire_conductor_fence,
    conductor_epoch_matches,
    core_generation_matches,
    fence_authorizes_plan,
    fence_is_monotonic,
    lease_is_fresh,
    may_start_move,
    take_over_conductor_fence,
)
from arena_hero_agent.migration.plan import (
    MigrationCoreIdentity,
    MigrationLease,
    MigrationPlanV1,
)
from arena_hero_agent.ports import LeaseDisposition

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


def test_may_start_move_true_when_all_preconditions_met(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    plan = make_plan(lease=fresh_lease())
    assert may_start_move(
        plan,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_conductor_epoch=1,
        observed_core_id="uuid-A",
    )


def test_lease_tick_expired_blocks_move(make_plan: Callable[..., MigrationPlanV1]) -> None:
    plan = make_plan(lease=MigrationLease(until_tick=74_123, heartbeat_at=HEARTBEAT))
    assert not may_start_move(
        plan,
        current_tick=74_124,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_conductor_epoch=1,
        observed_core_id="uuid-A",
    )


def test_lease_tick_at_boundary_is_fresh(make_plan: Callable[..., MigrationPlanV1]) -> None:
    plan = make_plan(lease=fresh_lease())
    assert lease_is_fresh(
        plan.lease,
        current_tick=74_123,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
    )


def test_lease_heartbeat_stale_blocks_move(make_plan: Callable[..., MigrationPlanV1]) -> None:
    stale = (NOW - timedelta(seconds=TTL_SECONDS + 1)).isoformat()
    plan = make_plan(lease=MigrationLease(until_tick=74_123, heartbeat_at=stale))
    assert not may_start_move(
        plan,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_conductor_epoch=1,
        observed_core_id="uuid-A",
    )


def test_lease_heartbeat_at_ttl_boundary_is_fresh(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    edge = (NOW - timedelta(seconds=TTL_SECONDS)).isoformat()
    plan = make_plan(lease=MigrationLease(until_tick=74_123, heartbeat_at=edge))
    assert lease_is_fresh(
        plan.lease,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
    )


def test_lease_heartbeat_invalid_blocks_move_fail_closed(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    plan = make_plan(lease=MigrationLease(until_tick=74_123, heartbeat_at="not-a-date"))
    assert not lease_is_fresh(
        plan.lease,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
    )


def test_lease_custom_ttl_is_respected(make_plan: Callable[..., MigrationPlanV1]) -> None:
    stale = (NOW - timedelta(seconds=31)).isoformat()
    plan = make_plan(lease=MigrationLease(until_tick=74_123, heartbeat_at=stale))
    assert not lease_is_fresh(
        plan.lease,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=30,
    )
    assert lease_is_fresh(
        plan.lease,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=60,
    )


def test_epoch_mismatch_blocks_move(make_plan: Callable[..., MigrationPlanV1]) -> None:
    plan = make_plan(lease=fresh_lease(), conductor_epoch=1)
    assert conductor_epoch_matches(plan, 1)
    assert not may_start_move(
        plan,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_conductor_epoch=2,
        observed_core_id="uuid-A",
    )


@pytest.mark.parametrize(
    "observed_core_id",
    ["uuid-B", None],
)
def test_core_id_mismatch_blocks_move(
    make_plan: Callable[..., MigrationPlanV1],
    observed_core_id: str | None,
) -> None:
    plan = make_plan(lease=fresh_lease())
    assert not core_generation_matches(plan, observed_core_id)
    assert not may_start_move(
        plan,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_conductor_epoch=1,
        observed_core_id=observed_core_id,
    )


def test_origin_core_id_none_blocks_move(make_plan: Callable[..., MigrationPlanV1]) -> None:
    plan = make_plan(
        lease=fresh_lease(),
        core=MigrationCoreIdentity(origin_core_id=None, current_core_id=None, generation=1),
    )
    assert not core_generation_matches(plan, None)
    assert not may_start_move(
        plan,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_conductor_epoch=1,
        observed_core_id=None,
    )


def test_plan_self_inconsistent_core_generation_blocks_move(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    plan = make_plan(
        lease=fresh_lease(),
        core=MigrationCoreIdentity(
            origin_core_id="uuid-A",
            current_core_id="uuid-B",
            generation=2,
        ),
    )
    assert not core_generation_matches(plan, "uuid-B")


async def test_conductor_fence_is_tenant_exclusive() -> None:
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    first = await acquire_conductor_fence(leases, TENANT, GENERATION, BUDGET)
    second = await acquire_conductor_fence(leases, TENANT, GENERATION, BUDGET)
    assert first is not None
    assert second is None
    assert first.disposition is LeaseDisposition.ACTIVE


async def test_stale_takeover_increments_epoch_monotonically() -> None:
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    old = await acquire_conductor_fence(leases, TENANT, GENERATION, BUDGET)
    assert old is not None
    old_epoch = old.fencing_token

    # Active lease cannot be replaced.
    too_early = await take_over_conductor_fence(
        leases,
        TENANT,
        GENERATION,
        expected_fencing_token=old_epoch,
        budget=BUDGET,
    )
    assert too_early is None

    clock.advance(100)  # lease expires
    replacement = await take_over_conductor_fence(
        leases,
        TENANT,
        GENERATION,
        expected_fencing_token=old_epoch,
        budget=BUDGET,
    )
    assert replacement is not None
    assert fence_is_monotonic(old_epoch, replacement.fencing_token)
    assert replacement.fencing_token.value == old_epoch.value + 1
    assert old.disposition is LeaseDisposition.REPLACED


async def test_takeover_requires_exact_observed_fence() -> None:
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    old = await acquire_conductor_fence(leases, TENANT, GENERATION, BUDGET)
    assert old is not None
    clock.advance(100)
    wrong_fence = await take_over_conductor_fence(
        leases,
        TENANT,
        GENERATION,
        expected_fencing_token=FencingToken(old.fencing_token.value + 1),
        budget=BUDGET,
    )
    assert wrong_fence is None
    assert old.disposition is LeaseDisposition.EXPIRED


async def test_stale_takeover_rejects_old_conductor_order_fault_injection(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    """故障注入：旧 conductor 接管前写的订单在接管后必须被拒。"""
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)

    old = await acquire_conductor_fence(leases, TENANT, GENERATION, BUDGET)
    assert old is not None
    old_order = make_plan(lease=fresh_lease(), conductor_epoch=old.fencing_token.value)

    clock.advance(100)
    replacement = await take_over_conductor_fence(
        leases,
        TENANT,
        GENERATION,
        expected_fencing_token=old.fencing_token,
        budget=BUDGET,
    )
    assert replacement is not None
    new_epoch = replacement.fencing_token.value

    # 旧 conductor 订单：epoch 失配 → 拒发 START_MOVE（fail-closed）。
    assert not may_start_move(
        old_order,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_conductor_epoch=new_epoch,
        observed_core_id="uuid-A",
    )
    # 新 conductor 订单（epoch 匹配）→ 允许。
    new_order = make_plan(lease=fresh_lease(), conductor_epoch=new_epoch)
    assert may_start_move(
        new_order,
        current_tick=74_000,
        now=NOW,
        heartbeat_ttl_seconds=TTL_SECONDS,
        expected_conductor_epoch=new_epoch,
        observed_core_id="uuid-A",
    )
    # 旧 handle 不再授权任何计划写（fence 已失效）。
    assert not fence_authorizes_plan(old, new_order)
    assert not fence_authorizes_plan(replacement, old_order)


async def test_fence_authorizes_plan_only_for_active_matching_handle(
    make_plan: Callable[..., MigrationPlanV1],
) -> None:
    clock = ManualClock()
    leases = MemoryLeaseCoordinator(clock, lease_duration_ns=100)
    handle = await acquire_conductor_fence(leases, TENANT, GENERATION, BUDGET)
    assert handle is not None
    plan = make_plan(lease=fresh_lease(), conductor_epoch=handle.fencing_token.value)
    assert fence_authorizes_plan(handle, plan)

    wrong_epoch = make_plan(lease=fresh_lease(), conductor_epoch=handle.fencing_token.value + 1)
    assert not fence_authorizes_plan(handle, wrong_epoch)
    await handle.release()
    assert not fence_authorizes_plan(handle, plan)
