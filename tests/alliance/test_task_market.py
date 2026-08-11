"""P4-17 task market: deterministic assignment, stable ties, explicit reject."""

from __future__ import annotations

import pytest

from arena_hero_agent.alliance.task_market import (
    AssignmentResult,
    MarketTask,
    RejectedTask,
    TaskAssignment,
    TenantCapacity,
    assign_tasks,
)
from arena_hero_agent.domain import TenantId

T1 = TenantId("t1")
T2 = TenantId("t2")
T3 = TenantId("t3")


def task(
    *,
    task_id: str,
    priority: int = 0,
    deadline_tick: int | None = None,
    weight: int = 1,
    eligible_tenants: tuple[TenantId, ...] = (),
    required_tenant: TenantId | None = None,
) -> MarketTask:
    return MarketTask(
        task_id=task_id,
        priority=priority,
        deadline_tick=deadline_tick,
        weight=weight,
        eligible_tenants=eligible_tenants,
        required_tenant=required_tenant,
    )


def tenant(*, tenant_id: TenantId, capacity: int) -> TenantCapacity:
    return TenantCapacity(tenant_id=tenant_id, capacity=capacity)


def assign_ids(result: AssignmentResult) -> dict[str, str]:
    return {item.task_id: item.tenant_id.value for item in result.assignments}


def test_deterministic_same_input_twice() -> None:
    tasks = [task(task_id="b"), task(task_id="a", priority=3), task(task_id="c", deadline_tick=5)]
    tenants = [tenant(tenant_id=T1, capacity=2), tenant(tenant_id=T2, capacity=2)]

    first = assign_tasks(tasks, tenants)
    second = assign_tasks(tasks, tenants)
    assert first == second
    assert first is not second
    assert first.assignments == second.assignments
    assert first.rejected == second.rejected


def test_tie_break_by_task_id_then_tenant_id() -> None:
    # Same priority and deadline: ids "b" and "a" tie; one slot -> "a" wins.
    tasks = [
        task(task_id="b", priority=5, deadline_tick=10),
        task(task_id="a", priority=5, deadline_tick=10),
    ]
    tenants = [tenant(tenant_id=T2, capacity=1), tenant(tenant_id=T1, capacity=1)]
    result = assign_tasks(tasks, tenants)
    assert assign_ids(result) == {"a": "t1", "b": "t2"}  # tenant order t1 < t2


def test_priority_desc_then_deadline_asc() -> None:
    tasks = [
        task(task_id="low", priority=1),
        task(task_id="high", priority=9),
        task(task_id="soon", priority=9, deadline_tick=3),
        task(task_id="later", priority=9, deadline_tick=9),
    ]
    tenants = [tenant(tenant_id=T1, capacity=4)]
    result = assign_tasks(tasks, tenants)
    assert assign_ids(result) == {
        "soon": "t1",  # priority 9, deadline 3 first
        "later": "t1",
        "high": "t1",  # no deadline sorts last within the same priority
        "low": "t1",
    }


def test_unassignable_rejected_explicitly() -> None:
    tasks = [
        task(task_id="a"),
        task(task_id="b"),
        task(task_id="c"),
    ]
    tenants = [tenant(tenant_id=T1, capacity=2)]
    result = assign_tasks(tasks, tenants)
    assert assign_ids(result) == {"a": "t1", "b": "t1"}
    assert result.rejected == (
        RejectedTask(task_id="c", reason="no tenant has remaining capacity"),
    )


def test_eligible_tenants_constraint() -> None:
    tasks = [
        task(task_id="only-t2", eligible_tenants=(T2,)),
        task(task_id="any"),
    ]
    tenants = [tenant(tenant_id=T1, capacity=1), tenant(tenant_id=T2, capacity=1)]
    result = assign_tasks(tasks, tenants)
    assert assign_ids(result) == {"only-t2": "t2", "any": "t1"}


def test_eligible_tenants_without_capacity_rejected() -> None:
    tasks = [task(task_id="only-t2", eligible_tenants=(T2,))]
    tenants = [tenant(tenant_id=T1, capacity=3)]
    result = assign_tasks(tasks, tenants)
    assert result.assignments == ()
    assert result.rejected == (
        RejectedTask(task_id="only-t2", reason="no eligible tenant has remaining capacity"),
    )


def test_required_tenant_binding() -> None:
    tasks = [
        task(task_id="defend", required_tenant=T2),
        task(task_id="raid", required_tenant=T2),
    ]
    tenants = [tenant(tenant_id=T2, capacity=1), tenant(tenant_id=T1, capacity=5)]
    result = assign_tasks(tasks, tenants)
    assert assign_ids(result) == {"defend": "t2"}
    assert result.rejected == (
        RejectedTask(task_id="raid", reason="required tenant t2 has no remaining capacity"),
    )


def test_required_tenant_missing_from_market_rejected() -> None:
    tasks = [task(task_id="defend", required_tenant=T3)]
    tenants = [tenant(tenant_id=T1, capacity=5)]
    result = assign_tasks(tasks, tenants)
    assert result.assignments == ()
    assert result.rejected == (
        RejectedTask(task_id="defend", reason="required tenant t3 is not on the market"),
    )


def test_weight_consumes_capacity() -> None:
    tasks = [
        task(task_id="heavy", weight=3),
        task(task_id="light", weight=1),
    ]
    tenants = [tenant(tenant_id=T1, capacity=3)]
    result = assign_tasks(tasks, tenants)
    assert assign_ids(result) == {"heavy": "t1"}  # heavy took all capacity
    assert result.rejected == (
        RejectedTask(task_id="light", reason="no tenant has remaining capacity"),
    )


def test_output_sorted_by_task_id() -> None:
    tasks = [
        task(task_id="z", priority=1),
        task(task_id="m", priority=1),
        task(task_id="a", priority=1),
    ]
    tenants = [tenant(tenant_id=T1, capacity=10)]
    result = assign_tasks(tasks, tenants)
    assert [item.task_id for item in result.assignments] == ["a", "m", "z"]


def test_empty_market() -> None:
    empty = assign_tasks([], [tenant(tenant_id=T1, capacity=3)])
    assert empty.assignments == ()
    assert empty.rejected == ()
    no_tenants = assign_tasks([task(task_id="a")], [])
    assert no_tenants.assignments == ()
    assert no_tenants.rejected == (
        RejectedTask(task_id="a", reason="no tenant has remaining capacity"),
    )


# --- fail-closed input rejection ---


def test_market_task_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        MarketTask(task_id="")  # empty id
    with pytest.raises(ValueError):
        MarketTask(task_id="a", priority=-1)  # negative priority
    with pytest.raises(ValueError):
        MarketTask(task_id="a", deadline_tick=-2)  # negative deadline
    with pytest.raises(ValueError):
        MarketTask(task_id="a", weight=0)  # non-positive weight
    with pytest.raises(TypeError):
        MarketTask(task_id="a", eligible_tenants=[T1])  # type: ignore
    with pytest.raises(TypeError):
        MarketTask(task_id="a", eligible_tenants=(T1, "t2"))  # type: ignore
    with pytest.raises(TypeError):
        MarketTask(task_id="a", required_tenant="t1")  # type: ignore
    with pytest.raises(TypeError):
        MarketTask(task_id="a", priority=True)  # type: ignore[arg-type]


def test_required_tenant_outside_eligible_rejected() -> None:
    with pytest.raises(ValueError):
        MarketTask(task_id="a", eligible_tenants=(T1,), required_tenant=T2)


def test_tenant_capacity_rejects_malformed() -> None:
    with pytest.raises(TypeError):
        TenantCapacity(tenant_id="t1", capacity=1)  # type: ignore
    with pytest.raises(ValueError):
        TenantCapacity(tenant_id=T1, capacity=-1)  # negative capacity
    with pytest.raises(TypeError):
        TenantCapacity(tenant_id=T1, capacity=True)  # type: ignore[arg-type]


def test_duplicate_tenant_capacity_fails_closed() -> None:
    with pytest.raises(ValueError, match="duplicate tenant capacity"):
        assign_tasks(
            [task(task_id="a")],
            [tenant(tenant_id=T1, capacity=1), tenant(tenant_id=T1, capacity=2)],
        )


def test_duplicate_task_ids_fail_closed() -> None:
    with pytest.raises(ValueError, match="must not repeat"):
        assign_tasks(
            [task(task_id="a"), task(task_id="a")],
            [tenant(tenant_id=T1, capacity=5)],
        )


def test_assign_tasks_rejects_non_task_entries() -> None:
    with pytest.raises(TypeError):
        assign_tasks(["not-a-task"], [tenant(tenant_id=T1, capacity=1)])  # type: ignore


def test_assign_tasks_rejects_non_tenant_entries() -> None:
    with pytest.raises(TypeError):
        assign_tasks([task(task_id="a")], ["not-a-tenant"])  # type: ignore


def test_result_rejects_duplicate_assignment_ids() -> None:
    with pytest.raises(ValueError):
        AssignmentResult(
            assignments=(
                TaskAssignment(task_id="a", tenant_id=T1),
                TaskAssignment(task_id="a", tenant_id=T2),
            ),
            rejected=(),
        )


def test_result_rejects_overlap_between_assigned_and_rejected() -> None:
    with pytest.raises(ValueError):
        AssignmentResult(
            assignments=(TaskAssignment(task_id="a", tenant_id=T1),),
            rejected=(RejectedTask(task_id="a", reason="no capacity"),),
        )


def test_result_immutable_tuples() -> None:
    result = assign_tasks([task(task_id="a")], [tenant(tenant_id=T1, capacity=1)])
    assert isinstance(result.assignments, tuple)
    assert isinstance(result.rejected, tuple)
