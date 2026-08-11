"""Deterministic alliance task-market assignment.

P4-17 requires a task market whose allocation is reproducible: the same input
always yields the same assignment. There is no task-market oracle under the
read-only TS boundary (lib/alliance/), so this module implements the P4-17
spec directly (difference recorded in PROGRESS.md):

- no randomness at all (stronger than a seeded RNG): tasks are processed in a
  stable total order (priority desc, deadline asc, task id asc) and tenants in
  tenant-id order, so ties always break deterministically;
- unassignable tasks are explicitly rejected with a reason, never silently
  dropped;
- malformed input (unknown tenant, duplicate capacity, negative capacity, bad
  weight/deadline/priority) fails closed by raising, because determinism
  cannot be guaranteed over corrupt input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from arena_hero_agent.domain import TenantId

# Stable sentinel for tasks without a deadline: sorts after every real deadline.
_NO_DEADLINE = 2**63 - 1


@dataclass(frozen=True, slots=True)
class MarketTask:
    """One task offered on the market."""

    __canonical_name__ = "arena-hero.market-task.v1"

    task_id: str
    priority: int = 0
    deadline_tick: int | None = None
    weight: int = 1
    eligible_tenants: tuple[TenantId, ...] = ()
    required_tenant: TenantId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task id must be a non-empty string")
        _require_int("task priority", self.priority, minimum=0)
        if self.deadline_tick is not None:
            _require_int("task deadline_tick", self.deadline_tick, minimum=0)
        _require_int("task weight", self.weight, minimum=1)
        if not isinstance(self.eligible_tenants, tuple) or not all(
            isinstance(tenant, TenantId) for tenant in self.eligible_tenants
        ):
            raise TypeError("task eligible_tenants must be a tuple of TenantId")
        object.__setattr__(self, "eligible_tenants", tuple(dict.fromkeys(self.eligible_tenants)))
        if self.required_tenant is not None and not isinstance(self.required_tenant, TenantId):
            raise TypeError("task required_tenant must be a TenantId or None")
        if (
            self.required_tenant is not None
            and self.eligible_tenants
            and self.required_tenant not in self.eligible_tenants
        ):
            raise ValueError("task required_tenant must be within eligible_tenants")


@dataclass(frozen=True, slots=True)
class TenantCapacity:
    """Market capacity of one assignable tenant."""

    __canonical_name__ = "arena-hero.tenant-capacity.v1"

    tenant_id: TenantId
    capacity: int

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("tenant capacity tenant_id must be a TenantId")
        _require_int("tenant capacity", self.capacity, minimum=0)


@dataclass(frozen=True, slots=True)
class TaskAssignment:
    """A task deterministically assigned to a tenant."""

    __canonical_name__ = "arena-hero.task-assignment.v1"

    task_id: str
    tenant_id: TenantId

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("assignment task_id must be a non-empty string")
        if not isinstance(self.tenant_id, TenantId):
            raise TypeError("assignment tenant_id must be a TenantId")


@dataclass(frozen=True, slots=True)
class RejectedTask:
    """A task explicitly rejected with a reason."""

    __canonical_name__ = "arena-hero.rejected-task.v1"

    task_id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("rejected task_id must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("rejected reason must be a non-empty string")


@dataclass(frozen=True, slots=True)
class AssignmentResult:
    """Deterministic market outcome; both lists are sorted by task id."""

    __canonical_name__ = "arena-hero.assignment-result.v1"

    assignments: tuple[TaskAssignment, ...]
    rejected: tuple[RejectedTask, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.assignments, tuple) or not all(
            isinstance(item, TaskAssignment) for item in self.assignments
        ):
            raise TypeError("assignments must be a tuple of TaskAssignment")
        if not isinstance(self.rejected, tuple) or not all(
            isinstance(item, RejectedTask) for item in self.rejected
        ):
            raise TypeError("rejected must be a tuple of RejectedTask")
        assigned_ids = [item.task_id for item in self.assignments]
        rejected_ids = [item.task_id for item in self.rejected]
        if len(set(assigned_ids)) != len(assigned_ids) or len(set(rejected_ids)) != len(
            rejected_ids
        ):
            raise ValueError("task ids must not repeat within assignments or rejected")
        if set(assigned_ids) & set(rejected_ids):
            raise ValueError("a task cannot be both assigned and rejected")


def _require_int(name: str, value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def assign_tasks(
    tasks: Sequence[MarketTask],
    tenants: Sequence[TenantCapacity],
) -> AssignmentResult:
    """Deterministically assign tasks to tenants (stable total order, no RNG).

    Task order: priority desc, then deadline_tick asc (no deadline last), then
    task id asc. Tenants are considered in tenant-id order. A task whose
    eligible set is empty may bind any tenant with remaining capacity; a task
    with ``required_tenant`` binds exactly that tenant. Every unassignable task
    is returned in ``rejected`` with a reason.
    """

    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        raise TypeError("tasks must be a sequence of MarketTask")
    if not isinstance(tenants, Sequence) or isinstance(tenants, (str, bytes)):
        raise TypeError("tenants must be a sequence of TenantCapacity")

    tenant_list = list(tenants)
    for item in tenant_list:
        if not isinstance(item, TenantCapacity):
            raise TypeError("tenants must contain only TenantCapacity")
    tenant_list.sort(key=lambda item: item.tenant_id.value)
    capacities: dict[TenantId, int] = {}
    for item in tenant_list:
        if item.tenant_id in capacities:
            raise ValueError(f"duplicate tenant capacity for {item.tenant_id.value}")
        capacities[item.tenant_id] = item.capacity
    tenant_ids = tuple(capacities)

    task_list = list(tasks)
    for item in task_list:
        if not isinstance(item, MarketTask):
            raise TypeError("tasks must contain only MarketTask")

    def order_key(task: MarketTask) -> tuple[int, int, str]:
        deadline = task.deadline_tick if task.deadline_tick is not None else _NO_DEADLINE
        return (-task.priority, deadline, task.task_id)

    ordered_tasks = sorted(task_list, key=order_key)
    remaining = dict(capacities)

    assignments: list[TaskAssignment] = []
    rejected: list[RejectedTask] = []
    for task in ordered_tasks:
        candidate_tenants = _candidate_tenants(task, tenant_ids)
        chosen: TenantId | None = None
        for tenant_id in candidate_tenants:
            if remaining[tenant_id] >= task.weight:
                chosen = tenant_id
                break
        if chosen is None:
            rejected.append(
                RejectedTask(
                    task_id=task.task_id, reason=_rejection_reason(task, tenant_ids, remaining)
                )
            )
            continue
        remaining[chosen] -= task.weight
        assignments.append(TaskAssignment(task_id=task.task_id, tenant_id=chosen))

    assignments.sort(key=lambda item: item.task_id)
    rejected.sort(key=lambda item: item.task_id)
    return AssignmentResult(assignments=tuple(assignments), rejected=tuple(rejected))


def _candidate_tenants(task: MarketTask, tenant_ids: tuple[TenantId, ...]) -> tuple[TenantId, ...]:
    if task.required_tenant is not None:
        if task.required_tenant not in tenant_ids:
            return ()
        return (task.required_tenant,)
    if task.eligible_tenants:
        return tuple(tenant for tenant in tenant_ids if tenant in task.eligible_tenants)
    return tenant_ids


def _rejection_reason(
    task: MarketTask,
    tenant_ids: tuple[TenantId, ...],
    remaining: dict[TenantId, int],
) -> str:
    if task.required_tenant is not None:
        if task.required_tenant not in remaining:
            return f"required tenant {task.required_tenant.value} is not on the market"
        return f"required tenant {task.required_tenant.value} has no remaining capacity"
    if task.eligible_tenants and not any(
        remaining.get(tenant, 0) >= task.weight for tenant in task.eligible_tenants
    ):
        return "no eligible tenant has remaining capacity"
    return "no tenant has remaining capacity"


__all__ = [
    "AssignmentResult",
    "MarketTask",
    "RejectedTask",
    "TaskAssignment",
    "TenantCapacity",
    "assign_tasks",
]
