"""Migration plan persistence: atomic writes, corruption tolerance, crash recovery.

Single-writer discipline (migration-system-v1 §6): the conductor is the only
writer and every write or cleanup must be authorized by a fenced writer lease
(P4-15). Writes are atomic (same-directory tmp + rename); reads fail closed on
any corruption and never silently drop or overwrite the damaged plan. Restart
recovery keeps the plan and only resumes the same operation (operationId +
revision + epoch) while the lease is fresh; RECOVERY_ABORT never resumes from
old legProgress.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from arena_hero_agent.ports.leases import WriterLeaseHandle

from .enactment import fence_authorizes_plan, fence_holds_tenant, lease_is_fresh
from .plan import MigrationPlanV1, parse_migration_plan
from .state_machine import MigrationState


class PlanStoreError(RuntimeError):
    """Base failure for the migration plan store."""


class CorruptPlanError(PlanStoreError):
    """The persisted plan is unreadable; fail-closed and never silently dropped."""


class UnauthorizedPlanWrite(PlanStoreError):
    """A plan write/cleanup was attempted without a fenced writer lease."""


class MigrationPlanStore:
    """Single-writer plan persistence under a fenced writer lease.

    Layout mirrors the TS/design contract: ``<root>/runtime/migration/<tenant>.json``.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def plan_path(self, tenant: str) -> Path:
        return self._root / "runtime" / "migration" / f"{tenant}.json"

    def write_plan(self, plan: MigrationPlanV1, *, lease: WriterLeaseHandle | None) -> None:
        """Atomically persist a plan; requires an active fenced writer lease.

        The fence check is the only entry into the write path, so a plan can
        never be written without a lease or with a stale (replaced) one.
        """
        if lease is None or not fence_authorizes_plan(lease, plan):
            raise UnauthorizedPlanWrite(
                f"plan write for tenant {plan.tenant!r} requires an active fenced writer lease"
            )
        target = self.plan_path(plan.tenant)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(plan.to_json_object(), indent=2))
                handle.write("\n")
            os.replace(tmp, target)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp)
            raise

    def read_plan(self, tenant: str) -> MigrationPlanV1 | None:
        """Read and strictly parse the persisted plan.

        Missing = no plan (module closed). Malformed = fail-closed raise; the
        damaged file is left untouched so it is never silently discarded.
        """
        path = self.plan_path(tenant)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise CorruptPlanError(
                f"unreadable migration plan for tenant {tenant!r}: {error}"
            ) from error
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            raise CorruptPlanError(
                f"corrupt migration plan for tenant {tenant!r}: {error}"
            ) from error
        result = parse_migration_plan(raw)
        if not result.ok:
            raise CorruptPlanError(f"corrupt migration plan for tenant {tenant!r}: {result.reason}")
        return result.plan

    def delete_plan(self, tenant: str, *, lease: WriterLeaseHandle | None) -> None:
        """ABORT/RECOVERY_ABORT cleanup; still requires the fenced writer lease."""

        if lease is None or not fence_holds_tenant(lease, tenant):
            raise UnauthorizedPlanWrite(
                f"plan cleanup for tenant {tenant!r} requires an active fenced writer lease"
            )
        self.plan_path(tenant).unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """Restart-time continuation verdict."""

    can_resume: bool
    reason: str


def recovery_blocks_resume(plan: MigrationPlanV1) -> bool:
    """RECOVERY_ABORT forbids continuing from old legProgress."""

    return plan.state == MigrationState.RECOVERY_ABORT


def resume_continuation_allowed(
    plan: MigrationPlanV1,
    *,
    expected_operation_id: str,
    expected_revision: int,
    expected_epoch: int,
) -> bool:
    """断点续传仅限同一 operation: operationId + revision + epoch all match."""

    return (
        plan.operation_id == expected_operation_id
        and plan.revision == expected_revision
        and plan.conductor_epoch == expected_epoch
    )


def evaluate_recovery(
    plan: MigrationPlanV1 | None,
    *,
    current_tick: int,
    now: datetime,
    heartbeat_ttl_seconds: int,
    expected_operation_id: str,
    expected_revision: int,
    expected_epoch: int,
) -> RecoveryOutcome:
    """Crash-recovery gate: plan retained + lease fresh + same operation.

    Any unmet precondition fails closed (no continuation; the core stays in
    NORMAL, which is the safe state). This evaluation never deletes the plan.
    """
    if plan is None:
        return RecoveryOutcome(can_resume=False, reason="no-plan")
    if not lease_is_fresh(
        plan.lease,
        current_tick=current_tick,
        now=now,
        heartbeat_ttl_seconds=heartbeat_ttl_seconds,
    ):
        return RecoveryOutcome(can_resume=False, reason="lease-expired-fail-closed")
    if recovery_blocks_resume(plan):
        return RecoveryOutcome(can_resume=False, reason="recovery-abort-blocks-resume")
    if not resume_continuation_allowed(
        plan,
        expected_operation_id=expected_operation_id,
        expected_revision=expected_revision,
        expected_epoch=expected_epoch,
    ):
        return RecoveryOutcome(can_resume=False, reason="operation-identity-mismatch")
    return RecoveryOutcome(can_resume=True, reason="resume-ok")


__all__ = [
    "CorruptPlanError",
    "MigrationPlanStore",
    "PlanStoreError",
    "RecoveryOutcome",
    "UnauthorizedPlanWrite",
    "evaluate_recovery",
    "recovery_blocks_resume",
    "resume_continuation_allowed",
]
