"""Shared fixtures for the fault injection suite."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import pytest

from arena_hero_agent.migration.plan import (
    MigrationAuditResult,
    MigrationConductorMeta,
    MigrationCoreIdentity,
    MigrationLease,
    MigrationLeg,
    MigrationLegProgress,
    MigrationPace,
    MigrationPath,
    MigrationPlanV1,
    MigrationPosition,
    MigrationRoleQuotas,
    MigrationRoles,
    MigrationTarget,
)
from arena_hero_agent.migration.state_machine import MigrationState

HEARTBEAT = "2026-08-08T21:30:00.000Z"
TENANT = "t1"
ORIGIN_CORE_ID = "uuid-A"


def _make_plan(
    *,
    operation_id: str = "op-fault-01",
    revision: int = 1,
    conductor_epoch: int = 1,
    tenant: str = TENANT,
    mode: Literal["migrate", "receive"] = "migrate",
    state: MigrationState = MigrationState.PLAN,
    lease: MigrationLease | None = None,
    updated_at: str = HEARTBEAT,
) -> MigrationPlanV1:
    return MigrationPlanV1(
        schema="migration-plan-v1",
        operation_id=operation_id,
        revision=revision,
        conductor_epoch=conductor_epoch,
        tenant=tenant,
        mode=mode,
        state=state,
        core=MigrationCoreIdentity(
            origin_core_id=ORIGIN_CORE_ID,
            current_core_id=ORIGIN_CORE_ID,
            generation=1,
        ),
        lease=lease or MigrationLease(until_tick=74_123, heartbeat_at=HEARTBEAT),
        target=MigrationTarget(x=-20, y=40, reason="t1/t2 meetup"),
        path=MigrationPath(cells=((-583, -111),), corridor_width=8, lookahead=30),
        legs=(
            MigrationLeg(
                index=0,
                from_pos=MigrationPosition(-583, -111),
                to=MigrationPosition(-450, -60),
                audit=MigrationAuditResult(ok=True, fresh_resources=12, active_enemy_cores=0),
            ),
        ),
        leg_progress=MigrationLegProgress(leg_index=0, cells_this_leg=0),
        pace=MigrationPace(
            policy="adaptive",
            burst_cells=8,
            settle_target=60,
            min_settle=30,
            max_settle=120,
            harvest_radius=12,
        ),
        roles=MigrationRoles(
            quotas=MigrationRoleQuotas(escort=40, sweep=30, scout=15, rear=15),
            seed=1,
        ),
        conductor=MigrationConductorMeta(pid=12_345),
        updated_at=updated_at,
    )


@pytest.fixture
def make_plan() -> Callable[..., MigrationPlanV1]:
    """Fixture returning the :func:`_make_plan` factory (valid full plan)."""

    return _make_plan
