"""Runtime enactment contract and conductor fencing (migration-system-v1 §6.2).

The runtime gate is fail-closed: every tick it may only issue the next
START_MOVE when ``leaseFresh && conductorEpoch 匹配 && currentCoreId ==
originCoreId``; any unmet precondition degrades to NORMAL/WAIT. Conductor
fencing reuses the P4-15 :class:`~arena_hero_agent.ports.leases.WriterLease`
protocol: the tenant-level exclusive writer lease is the fence, its fencing
token is the plan's ``conductorEpoch``, and a stale takeover replaces only the
expired holder presenting the exact observed token, which bumps the token
(epoch) monotonically so the old conductor's orders are rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arena_hero_agent.domain import DeadlineBudget, FencingToken, Generation, TenantId
from arena_hero_agent.ports.leases import LeaseDisposition, WriterLease, WriterLeaseHandle

from .plan import MigrationLease, MigrationPlanV1

DEFAULT_HEARTBEAT_TTL_SECONDS = 60


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _parse_heartbeat(value: str) -> datetime | None:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return _as_utc(parsed)


def lease_is_fresh(
    lease: MigrationLease,
    *,
    current_tick: int,
    now: datetime,
    heartbeat_ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
) -> bool:
    """§6.2 ``leaseFresh``: untilTick not passed and heartbeat within TTL.

    Both conditions must hold; an unparsable heartbeat is treated as stale
    (fail-closed), matching the TS ``isMigrationLeaseFresh`` oracle.
    """
    heartbeat = _parse_heartbeat(lease.heartbeat_at)
    if heartbeat is None:
        return False
    tick_fresh = lease.until_tick >= current_tick
    heartbeat_fresh = (_as_utc(now) - heartbeat) <= timedelta(seconds=heartbeat_ttl_seconds)
    return tick_fresh and heartbeat_fresh


def conductor_epoch_matches(plan: MigrationPlanV1, expected_conductor_epoch: int) -> bool:
    """§6.2 epoch check: the plan was issued by the current conductor fence."""

    return plan.conductor_epoch == expected_conductor_epoch


def core_generation_matches(plan: MigrationPlanV1, observed_core_id: str | None) -> bool:
    """§6.2 same-generation check: currentCoreId == originCoreId == live id."""

    if plan.core.origin_core_id is None:
        return False
    if plan.core.current_core_id != plan.core.origin_core_id:
        return False
    return observed_core_id == plan.core.origin_core_id


def may_start_move(
    plan: MigrationPlanV1,
    *,
    current_tick: int,
    now: datetime,
    heartbeat_ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
    expected_conductor_epoch: int,
    observed_core_id: str | None,
) -> bool:
    """Fail-closed §6.2 gate: only NORMAL/WAIT when any precondition fails."""

    return (
        lease_is_fresh(
            plan.lease,
            current_tick=current_tick,
            now=now,
            heartbeat_ttl_seconds=heartbeat_ttl_seconds,
        )
        and conductor_epoch_matches(plan, expected_conductor_epoch)
        and core_generation_matches(plan, observed_core_id)
    )


def fence_holds_tenant(handle: WriterLeaseHandle, tenant: str) -> bool:
    """True only while the handle still owns an active lease for the tenant."""

    return handle.disposition is LeaseDisposition.ACTIVE and handle.tenant_id.value == tenant


def fence_authorizes_plan(handle: WriterLeaseHandle, plan: MigrationPlanV1) -> bool:
    """Plan write gate: active tenant lease and epoch equals the fencing token.

    This is the single guard that makes "write a plan without a fenced lease"
    impossible for any code path that goes through the store.
    """

    return (
        fence_holds_tenant(handle, plan.tenant)
        and handle.fencing_token.value == plan.conductor_epoch
    )


async def acquire_conductor_fence(
    leases: WriterLease,
    tenant_id: TenantId,
    generation: Generation,
    budget: DeadlineBudget,
) -> WriterLeaseHandle | None:
    """Acquire the tenant-level exclusive conductor lock (P4-15 writer lease)."""

    return await leases.acquire_writer(tenant_id, generation, budget)


async def take_over_conductor_fence(
    leases: WriterLease,
    tenant_id: TenantId,
    generation: Generation,
    *,
    expected_fencing_token: FencingToken,
    budget: DeadlineBudget,
) -> WriterLeaseHandle | None:
    """Stale takeover: replace only the expired holder with the exact observed fence.

    A successful takeover issues a strictly larger fencing token (epoch), so
    the replaced conductor's existing orders fail the epoch check.
    """

    return await leases.replace_writer(
        tenant_id,
        generation,
        expected_fencing_token=expected_fencing_token,
        budget=budget,
    )


def fence_is_monotonic(previous: FencingToken, current: FencingToken) -> bool:
    """Fencing tokens are strictly monotonic: the new token supersedes the old."""

    return current.supersedes(previous)


__all__ = [
    "DEFAULT_HEARTBEAT_TTL_SECONDS",
    "acquire_conductor_fence",
    "conductor_epoch_matches",
    "core_generation_matches",
    "fence_authorizes_plan",
    "fence_holds_tenant",
    "fence_is_monotonic",
    "lease_is_fresh",
    "may_start_move",
    "take_over_conductor_fence",
]
