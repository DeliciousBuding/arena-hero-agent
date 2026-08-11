"""Worker task types and the forced-task priority contract (legacy RP2).

Forced tasks bypass the cost matrix and are assigned directly, in priority order:

1. cargo > 0 and the Core can receive a deposit -> DEPOSIT;
2. standing on a visible resource cell with cargo = 0 -> HARVEST_CURRENT;
3. low health and a safe return path -> RETURN_FOR_HEAL.

Any worker that hits none of these returns ``None`` and falls through to the cost
matrix (P4-12). ``can_deposit`` additionally requires free resource space: with
``resource_space == 0`` a forced DEPOSIT would drag a full worker onto the Core
cell and immediately stall it (v0.2.14 oscillation fix).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from arena_hero_agent.domain import Coordinate, manhattan

from .planning_snapshot import PlanningSnapshot, PlanningUnit

_BEACON_GROUND = "ground"


class TaskType(StrEnum):
    """Worker task kinds produced by the deterministic task layer."""

    __canonical_name__ = "arena-hero.task-type.v1"

    HARVEST_CURRENT = "HARVEST_CURRENT"
    GO_RESOURCE = "GO_RESOURCE"
    DEPOSIT = "DEPOSIT"
    EXPLORE = "EXPLORE"
    PICKUP_BEACON = "PICKUP_BEACON"
    RETURN_FOR_HEAL = "RETURN_FOR_HEAL"
    WAIT = "WAIT"


_TARGET_REQUIRED = frozenset(
    {
        TaskType.HARVEST_CURRENT,
        TaskType.GO_RESOURCE,
        TaskType.DEPOSIT,
        TaskType.PICKUP_BEACON,
        TaskType.RETURN_FOR_HEAL,
    }
)
_CELL_KEY_REQUIRED = frozenset({TaskType.HARVEST_CURRENT, TaskType.PICKUP_BEACON})


@dataclass(frozen=True, slots=True)
class Task:
    """One deterministic worker task with an optional target cell."""

    __canonical_name__ = "arena-hero.task.v1"

    type: TaskType
    target: Coordinate | None = None
    target_cell_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, TaskType):
            raise TypeError("task type must be a TaskType")
        if self.target is not None and not isinstance(self.target, Coordinate):
            raise TypeError("task target must be a Coordinate or None")
        if self.target_cell_key is not None and not isinstance(self.target_cell_key, str):
            raise TypeError("task target_cell_key must be a string or None")
        if self.type in _TARGET_REQUIRED and self.target is None:
            raise ValueError(f"{self.type.value} tasks require a target")
        if self.type in _CELL_KEY_REQUIRED and self.target_cell_key is None:
            raise ValueError(f"{self.type.value} tasks require target_cell_key")
        if self.type not in _CELL_KEY_REQUIRED and self.target_cell_key is not None:
            raise ValueError("only harvest-current and pickup-beacon tasks declare target_cell_key")


def can_deposit(unit: PlanningUnit, snapshot: PlanningSnapshot) -> bool:
    """Return whether this worker can legally deposit now."""

    if not isinstance(unit, PlanningUnit):
        raise TypeError("unit must be a PlanningUnit")
    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot must be a PlanningSnapshot")
    return unit.cargo > 0 and snapshot.core_position is not None and snapshot.resource_space > 0


def can_return_for_heal(unit: PlanningUnit, core: Coordinate | None) -> bool:
    """Return whether the worker can return to a healing Core from another cell."""

    if not isinstance(unit, PlanningUnit):
        raise TypeError("unit must be a PlanningUnit")
    if core is not None and not isinstance(core, Coordinate):
        raise TypeError("core must be a Coordinate or None")
    return core is not None and manhattan(unit.position, core) > 0


def forced_task_for(unit: PlanningUnit, snapshot: PlanningSnapshot) -> Task | None:
    """Return the forced task for one worker, or None to fall through to the matrix.

    Priority: pickup a grounded Beacon on the worker's own cell, deposit cargo at
    the Core, harvest the visible resource under the worker, then return for heal.
    """

    if not isinstance(unit, PlanningUnit):
        raise TypeError("unit must be a PlanningUnit")
    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot must be a PlanningSnapshot")

    beacon = snapshot.beacon
    if (
        unit.cargo == 0
        and beacon.status == _BEACON_GROUND
        and beacon.carrier_id is None
        and unit.position == beacon.position
    ):
        return Task(
            type=TaskType.PICKUP_BEACON,
            target=beacon.position,
            target_cell_key=beacon.position.cell_key,
        )

    core = snapshot.core_position
    if unit.cargo > 0 and core is not None:
        return Task(type=TaskType.DEPOSIT, target=core)

    current = snapshot.resource_cells.get(unit.position.cell_key)
    if unit.cargo == 0 and current is not None and current.visible:
        return Task(
            type=TaskType.HARVEST_CURRENT,
            target=unit.position,
            target_cell_key=unit.position.cell_key,
        )

    if unit.health <= 1 and can_return_for_heal(unit, core):
        return Task(type=TaskType.RETURN_FOR_HEAL, target=core)

    return None
