"""Human override application: audit/apply/reject/expiry loop (P4-13).

The deterministic override loop is the highest-priority control surface above
the agent plan: one-shot commands and persistent goals from the P5-3 human
store override the base ``Plan`` per unit, the merged plan is re-validated with
the same authoritative validator the agent uses, and every outcome
(applied/rejected/satisfied/stale) is returned so the caller can audit it.

Behavior mirrors the legacy TypeScript ``runtime/human-override.ts``:

- ``mode=disabled``, an empty store, and a stale override all hand control
  back to the agent (base plan unchanged, nothing applied).
- Unknown units and actions that do not match the unit's capability are
  rejected per entry with a precise reason.
- One-shot commands win over persistent goals for the same unit.
- A stale override (``updatedAt`` older than ``STALE_OVERRIDE_MAX_AGE_MS`` and
  a valid ISO timestamp) is ignored wholesale so a crashed writer can never
  freeze the economy; invalid/missing timestamps never trigger expiry (legacy
  hand-written store compatibility).

Registered differences from the oracle are listed in
``docs/planning-differences.md`` (mine_hold goals are not carried by the
Python store; the pathing helper reuses the oracle-compared domain
``first_step`` without the oracle's abandon-factor pruning).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arena_hero_agent.domain import (
    TS_COMPATIBLE_SEARCH_LIMITS,
    Coordinate,
    Direction,
    EntityId,
    NavigationGrid,
    SearchLimitExceeded,
    SearchLimits,
    UnitRole,
    UnknownTraversalPolicy,
    UnreachableError,
    chebyshev,
    first_step,
)

from ..planning.plan import CoreAction, CoreActionType, Plan, UnitAction, UnitActionType
from ..planning.plan_validator import validate_plan
from ..planning.planning_snapshot import PlanningSnapshot, PlanningUnit
from .goal_store import _parse_epoch_ms
from .human_store import GoalEntry, HumanStore, read_human_store

STALE_OVERRIDE_MAX_AGE_MS = 10 * 60 * 1000
FAR_STEP = 60
_ADAPTIVE_RADIUS_CAP = 64

_WIRE_DIRECTIONS = {
    "UP": Direction.NORTH,
    "DOWN": Direction.SOUTH,
    "LEFT": Direction.WEST,
    "RIGHT": Direction.EAST,
}
_WIRE_ROLES = {
    "WORKER": UnitRole.WORKER,
    "VANGUARD": UnitRole.VANGUARD,
    "RANGER": UnitRole.RANGER,
}

# Wire action types -> Python unit-action types (is_core=False).
_UNIT_WIRE_TYPES = {
    "WAIT": UnitActionType.WAIT,
    "MOVE": UnitActionType.MOVE,
    "HARVEST": UnitActionType.HARVEST,
    "DEPOSIT": UnitActionType.DEPOSIT,
    "SWEEP": UnitActionType.SWEEP,
    "SHOOT": UnitActionType.SHOOT,
    "PICKUP_BEACON": UnitActionType.PICKUP_BEACON,
    "DROP_BEACON": UnitActionType.DROP_BEACON,
    "SELF_DESTRUCT": UnitActionType.SELF_DESTRUCT,
    "HEAL": UnitActionType.HEAL,
}
# Wire action types -> Python core-action types (is_core=True).
_CORE_WIRE_TYPES = {
    "WAIT": CoreActionType.WAIT,
    "START_MOVE": CoreActionType.START_MOVE,
    "REPAIR_SHIELD": CoreActionType.REPAIR_SHIELD,
    "CANCEL_MOVE": CoreActionType.CANCEL_MOVE,
    "SPAWN": CoreActionType.SPAWN,
    "PICKUP_BEACON": CoreActionType.PICKUP_BEACON,
    "DROP_BEACON": CoreActionType.DROP_BEACON,
    "SELF_DESTRUCT": CoreActionType.SELF_DESTRUCT,
    "HEAL": CoreActionType.HEAL,
}


@dataclass(frozen=True, slots=True)
class HumanRejection:
    """One rejected human directive with the precise reason."""

    __canonical_name__ = "arena-hero.human-rejection.v1"

    unit_id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, str) or not self.unit_id:
            raise ValueError("rejection unit_id must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("rejection reason must be a non-empty string")


@dataclass(frozen=True, slots=True)
class HumanOverrideResult:
    """Outcome of applying one human store to one plan (audit surface)."""

    __canonical_name__ = "arena-hero.human-override-result.v1"

    plan: Plan
    active: bool
    applied: tuple[str, ...]
    rejected: tuple[HumanRejection, ...]
    satisfied: tuple[str, ...]
    updated_at: str | None
    stale: bool

    def __post_init__(self) -> None:
        if not isinstance(self.plan, Plan):
            raise TypeError("plan must be a Plan")
        if not isinstance(self.active, bool):
            raise TypeError("active must be a boolean")
        if not isinstance(self.applied, tuple) or any(
            not isinstance(item, str) for item in self.applied
        ):
            raise TypeError("applied must be a tuple of strings")
        if not isinstance(self.rejected, tuple) or any(
            not isinstance(item, HumanRejection) for item in self.rejected
        ):
            raise TypeError("rejected must be a tuple of HumanRejection")
        if not isinstance(self.satisfied, tuple) or any(
            not isinstance(item, str) for item in self.satisfied
        ):
            raise TypeError("satisfied must be a tuple of strings")
        if self.updated_at is not None and not isinstance(self.updated_at, str):
            raise TypeError("updated_at must be a string or None")
        if not isinstance(self.stale, bool):
            raise TypeError("stale must be a boolean")


def is_stale_override(store: HumanStore, now_ms: int) -> bool:
    """Return whether the store is a stale override (whole-store ignore).

    Only ``mode=override`` stores with a valid ISO ``updatedAt`` older than
    ``STALE_OVERRIDE_MAX_AGE_MS`` expire; invalid or missing timestamps never
    expire (legacy store compatibility), exactly like the oracle.
    """

    if not isinstance(store, HumanStore):
        raise TypeError("store must be a HumanStore")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int):
        raise TypeError("now_ms must be an integer")
    if store.mode != "override" or store.updated_at is None:
        return False
    updated_at = _parse_epoch_ms(store.updated_at)
    if updated_at is None:
        return False
    return now_ms - updated_at > STALE_OVERRIDE_MAX_AGE_MS


def action_from_wire(
    unit_id: str, action: Mapping[str, Any], *, is_core: bool
) -> UnitAction | CoreAction | None:
    """Parse one wire action into a domain action; None when malformed.

    ``is_core`` selects the core action surface (START_MOVE/SPAWN/...). A wire
    type that belongs to the other surface is malformed for this target and
    returns None, so the caller rejects it with ``invalid_action`` (fail-closed;
    the oracle defers some of these to plan validation instead).
    """

    if not isinstance(unit_id, str) or not unit_id:
        raise ValueError("unit_id must be a non-empty string")
    if not isinstance(action, Mapping):
        return None
    if is_core:
        return _core_action_from_wire(action)
    return _unit_action_from_wire(unit_id, action)


def _unit_action_from_wire(unit_id: str, action: Mapping[str, Any]) -> UnitAction | None:
    raw_type = action.get("type")
    if not isinstance(raw_type, str):
        return None
    wire_type = raw_type.upper()
    action_type = _UNIT_WIRE_TYPES.get(wire_type)
    if action_type is None:
        return None
    if wire_type in ("MOVE", "SWEEP"):
        direction = _wire_direction(action.get("direction"))
        if direction is None:
            return None
        return UnitAction(unit_id=EntityId(unit_id), type=action_type, direction=direction)
    if wire_type == "SHOOT":
        raw_target = action.get("targetId")
        target_id: EntityId | None = None
        if raw_target not in (None, ""):
            if not isinstance(raw_target, str):
                return None
            target_id = EntityId(raw_target)
        raw_cell = action.get("expectedCell")
        if (
            not isinstance(raw_cell, (list, tuple))
            or len(raw_cell) != 2
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in raw_cell)
        ):
            return None
        return UnitAction(
            unit_id=EntityId(unit_id),
            type=UnitActionType.SHOOT,
            target_id=target_id,
            expected_cell=Coordinate(raw_cell[0], raw_cell[1]),
        )
    return UnitAction(unit_id=EntityId(unit_id), type=action_type)


def _core_action_from_wire(action: Mapping[str, Any]) -> CoreAction | None:
    raw_type = action.get("type")
    if not isinstance(raw_type, str):
        return None
    wire_type = raw_type.upper()
    action_type = _CORE_WIRE_TYPES.get(wire_type)
    if action_type is None:
        return None
    if wire_type == "START_MOVE":
        direction = _wire_direction(action.get("direction"))
        if direction is None:
            return None
        return CoreAction(type=CoreActionType.START_MOVE, direction=direction)
    if wire_type == "SPAWN":
        raw_role = action.get("unitType")
        role = _WIRE_ROLES.get(raw_role) if isinstance(raw_role, str) else None
        if role is None:
            return None
        return CoreAction(type=CoreActionType.SPAWN, unit_role=role)
    return CoreAction(type=action_type)


def _wire_direction(value: object) -> Direction | None:
    if not isinstance(value, str):
        return None
    return _WIRE_DIRECTIONS.get(value)


def basic_check(
    snapshot: PlanningSnapshot, unit_id: str, action: UnitAction | CoreAction
) -> str | None:
    """Base check: unit exists and the action matches its capability.

    Returns the rejection reason or None. The core accepts any parsed action;
    authoritative semantic validation happens once in :func:`validate_plan`.
    """

    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot must be a PlanningSnapshot")
    if not isinstance(unit_id, str) or not unit_id:
        raise ValueError("unit_id must be a non-empty string")
    is_core = snapshot.core_id is not None and unit_id == snapshot.core_id
    if not is_core:
        unit = next((unit for unit in snapshot.units if unit.id.value == unit_id), None)
        if unit is None:
            return "unknown_unit"
        return _capability_check(unit, action)
    return None


def _capability_check(unit: PlanningUnit, action: UnitAction | CoreAction) -> str | None:
    if isinstance(action, CoreAction):
        return None
    if action.type in (UnitActionType.HARVEST, UnitActionType.DEPOSIT) and (
        unit.unit_role is not UnitRole.WORKER
    ):
        return "action_requires_worker"
    if action.type is UnitActionType.SWEEP and unit.unit_role is not UnitRole.VANGUARD:
        return "action_requires_vanguard"
    if action.type is UnitActionType.SHOOT and unit.unit_role is not UnitRole.RANGER:
        return "action_requires_ranger"
    if action.type in (UnitActionType.PICKUP_BEACON, UnitActionType.DROP_BEACON) and (
        unit.unit_role not in (UnitRole.WORKER, UnitRole.VANGUARD)
    ):
        return "beacon_requires_worker_or_vanguard"
    return None


def goal_action_for_unit(
    snapshot: PlanningSnapshot, unit: PlanningUnit, goal: GoalEntry
) -> UnitAction | None:
    """Compute this tick's action for a persistent goal; None = satisfied.

    - ``mine``: harvest at the target, deposit at the core when full, and hand
      the unit back to the agent when the target mine is exhausted.
    - ``goto``: move to the target and hand the unit back on arrival.
    Pathing reuses the oracle-compared domain ``first_step``; far targets move
    through an obstacle-free interpolated mid-point (oracle parity).
    """

    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot must be a PlanningSnapshot")
    if not isinstance(unit, PlanningUnit):
        raise TypeError("unit must be a PlanningUnit")
    if not isinstance(goal, GoalEntry):
        raise TypeError("goal must be a GoalEntry")
    target = Coordinate(goal.target[0], goal.target[1])

    if goal.kind == "mine":
        if unit.cargo > 0:
            return _return_to_core(snapshot, unit)
        if unit.position == target:
            return UnitAction(unit_id=unit.id, type=UnitActionType.HARVEST)
        if not _resource_visible(snapshot, target):
            return None  # mine exhausted -> satisfied, hand back to the agent
        return _move_action(unit, target, snapshot.obstacle_cells)
    if goal.kind == "goto":
        if unit.position == target:
            return None  # arrived -> satisfied, hand back to the agent
        return _move_action(unit, target, snapshot.obstacle_cells)
    raise ValueError(f"unsupported goal kind {goal.kind!r}")


def apply_human_overrides(
    snapshot: PlanningSnapshot,
    plan: Plan,
    store: HumanStore,
    *,
    now_ms: int,
) -> HumanOverrideResult:
    """Merge the human store into the base plan and audit every directive.

    Stale, disabled, or empty stores return the base plan unchanged. Otherwise
    one-shot commands apply first (over goals), goals apply to non-overridden
    units, and the merged plan is re-validated with the authoritative plan
    validator; directives whose actions fail validation are rejected with the
    validator's message.
    """

    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot must be a PlanningSnapshot")
    if not isinstance(plan, Plan):
        raise TypeError("plan must be a Plan")
    if not isinstance(store, HumanStore):
        raise TypeError("store must be a HumanStore")
    if isinstance(now_ms, bool) or not isinstance(now_ms, int):
        raise TypeError("now_ms must be an integer")

    stale = is_stale_override(store, now_ms)
    if stale:
        return HumanOverrideResult(
            plan=plan,
            active=False,
            applied=(),
            rejected=(),
            satisfied=(),
            updated_at=store.updated_at,
            stale=True,
        )
    if store.mode != "override" or (not store.commands and not store.goals):
        # Oracle parity: disabled and empty stores carry no audit timestamp.
        return HumanOverrideResult(
            plan=plan,
            active=False,
            applied=(),
            rejected=(),
            satisfied=(),
            updated_at=None,
            stale=False,
        )

    unit_actions = {action.unit_id.value: action for action in plan.unit_actions}
    core_action = plan.core_action
    applied: list[str] = []
    rejected: list[HumanRejection] = []

    for command in store.commands:
        is_core = snapshot.core_id is not None and command.unit_id == snapshot.core_id
        action = action_from_wire(command.unit_id, command.action, is_core=is_core)
        if action is None:
            rejected.append(HumanRejection(command.unit_id, "invalid_action"))
            continue
        issue = basic_check(snapshot, command.unit_id, action)
        if issue is not None:
            rejected.append(HumanRejection(command.unit_id, issue))
            continue
        if is_core:
            assert isinstance(action, CoreAction)
            core_action = action
        else:
            assert isinstance(action, UnitAction)
            unit_actions[command.unit_id] = action
        applied.append(command.unit_id)

    satisfied: list[str] = []
    units_by_id = {unit.id.value: unit for unit in snapshot.units}
    core_overridden = core_action is not plan.core_action
    core_ids = {snapshot.core_id} if core_overridden and snapshot.core_id else set()
    overridden = set(applied) | core_ids
    for goal in store.goals:
        if goal.unit_id in overridden:
            continue
        unit = units_by_id.get(goal.unit_id)
        if unit is None:
            rejected.append(HumanRejection(goal.unit_id, "unknown_unit"))
            continue
        action = goal_action_for_unit(snapshot, unit, goal)
        if action is None:
            satisfied.append(goal.unit_id)
            continue
        unit_actions[goal.unit_id] = action
        applied.append(goal.unit_id)
        overridden.add(goal.unit_id)

    merged = Plan(
        tick=plan.tick,
        unit_actions=tuple(unit_actions.values()),
        core_action=core_action,
        intents=plan.intents,
    )
    validation = validate_plan(snapshot, merged)
    result_plan = validation.plan
    issue_actors = {issue.actor_id for issue in validation.issues}
    for unit_id in applied:
        if unit_id in issue_actors:
            message = next(
                (issue.message for issue in validation.issues if issue.actor_id == unit_id),
                "validation_failed",
            )
            rejected.append(HumanRejection(unit_id, message))
    final_applied = tuple(unit_id for unit_id in applied if unit_id not in issue_actors)

    return HumanOverrideResult(
        plan=result_plan,
        active=bool(final_applied),
        applied=final_applied,
        rejected=tuple(rejected),
        satisfied=tuple(satisfied),
        updated_at=store.updated_at,
        stale=False,
    )


def read_and_apply_human_overrides(
    data_root: str,
    tenant: str,
    snapshot: PlanningSnapshot,
    plan: Plan,
    *,
    now_ms: int,
) -> HumanOverrideResult:
    """Read one tenant's human store from disk and apply it to a plan.

    This is the persistence-source wiring over the P5-3 human store; the pure
    override loop stays deterministic because ``now_ms`` is explicit.
    """

    store = read_human_store(data_root, tenant)
    return apply_human_overrides(snapshot, plan, store, now_ms=now_ms)


def _resource_visible(snapshot: PlanningSnapshot, target: Coordinate) -> bool:
    resource = snapshot.resource_cells.get(target.cell_key)
    return resource is not None and resource.visible


def _return_to_core(snapshot: PlanningSnapshot, unit: PlanningUnit) -> UnitAction:
    if snapshot.core_position is None:
        return UnitAction(unit_id=unit.id, type=UnitActionType.WAIT)
    if unit.position == snapshot.core_position:
        return UnitAction(unit_id=unit.id, type=UnitActionType.DEPOSIT)
    return _move_action(unit, snapshot.core_position, snapshot.obstacle_cells)


def _move_action(unit: PlanningUnit, target: Coordinate, obstacles: frozenset[str]) -> UnitAction:
    direction = _step_toward_path(unit.position, target, obstacles)
    if direction is None:
        return UnitAction(unit_id=unit.id, type=UnitActionType.WAIT)
    return UnitAction(unit_id=unit.id, type=UnitActionType.MOVE, direction=direction)


def _step_toward_path(
    start: Coordinate, target: Coordinate, obstacles: frozenset[str]
) -> Direction | None:
    """First step toward a target, mirroring the oracle's bounded pathing.

    Returns None when the target is blocked, the start is already there, or the
    bounded search cannot conclude reachability (the caller waits).
    """

    if start == target or target.cell_key in obstacles:
        return None
    goal_chebyshev = chebyshev(start, target)
    path_target = target
    if goal_chebyshev > FAR_STEP:
        step = FAR_STEP
        while step >= 8:
            mid = Coordinate(
                start.x + math.trunc((target.x - start.x) * (step / goal_chebyshev)),
                start.y + math.trunc((target.y - start.y) * (step / goal_chebyshev)),
            )
            if mid.cell_key not in obstacles:
                path_target = mid
                break
            step = math.trunc(step / 2)
    distance = chebyshev(start, path_target)
    if distance <= TS_COMPATIBLE_SEARCH_LIMITS.search_radius:
        limits = TS_COMPATIBLE_SEARCH_LIMITS
    else:
        radius = min(_ADAPTIVE_RADIUS_CAP, distance + 2)
        limits = SearchLimits(node_budget=(2 * radius + 1) ** 2, search_radius=radius)
    blocked_coordinates = frozenset(Coordinate.parse_cell_key(key) for key in obstacles)
    try:
        return first_step(
            NavigationGrid(blocked_cells=blocked_coordinates),
            start,
            path_target,
            unknown_policy=UnknownTraversalPolicy.ALLOW,
            limits=limits,
        )
    except (UnreachableError, SearchLimitExceeded):
        return None


__all__ = [
    "FAR_STEP",
    "STALE_OVERRIDE_MAX_AGE_MS",
    "HumanOverrideResult",
    "HumanRejection",
    "action_from_wire",
    "apply_human_overrides",
    "basic_check",
    "goal_action_for_unit",
    "is_stale_override",
    "read_and_apply_human_overrides",
]
