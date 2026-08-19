"""Deterministic fail-closed plan validation and repair (legacy plan-validator).

The validator checks every planned action against the observed snapshot, drops
invalid actions with an explicit issue, and returns the repaired plan. Unlike the
legacy TypeScript switch, unknown or malformed action shapes are rejected instead
of silently passing through; this is a registered intentional difference.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    Coordinate,
    UnitRole,
    shot_line_blocked,
    unit_price,
)

from .plan import CoreAction, CoreActionType, Plan, PlanIntent, UnitAction, UnitActionType
from .planning_snapshot import PlanningSnapshot, PlanningUnit

UNIT_MAX_HP: dict[UnitRole, int] = {
    UnitRole.WORKER: 2,
    UnitRole.VANGUARD: 4,
    UnitRole.RANGER: 2,
}
CORE_MAX_HP = 5
CORE_SHIELD_CAP = 5
CORE_SHIELD_CAP_WITH_BEACON = 10
RANGER_SHOOT_RANGE = 3

_BEACON_GROUND = "ground"
_BEACON_CARRIED = "carried"
_CORE_NORMAL = "normal"
_CORE_MOVING = "moving"


class ValidationCode(StrEnum):
    """Stable validation failure codes shared with the legacy oracle."""

    __canonical_name__ = "arena-hero.validation-code.v1"

    TICK_MISMATCH = "tick_mismatch"
    UNKNOWN_UNIT = "unknown_unit"
    WRONG_CAPABILITY = "wrong_capability"
    BLOCKED_MOVE = "blocked_move"
    INVALID_HARVEST = "invalid_harvest"
    INVALID_DEPOSIT = "invalid_deposit"
    INVALID_HEAL = "invalid_heal"
    INVALID_BEACON = "invalid_beacon"
    INVALID_SHOT = "invalid_shot"
    MISSING_CORE = "missing_core"
    CORE_UNAVAILABLE = "core_unavailable"
    INSUFFICIENT_RESOURCES = "insufficient_resources"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One rejected plan element with a stable code and deterministic message."""

    __canonical_name__ = "arena-hero.validation-issue.v1"

    code: ValidationCode
    actor_id: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, ValidationCode):
            raise TypeError("issue code must be a ValidationCode")
        if not isinstance(self.actor_id, str) or not self.actor_id:
            raise ValueError("issue actor_id must be a non-empty string")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("issue message must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of validating and repairing one plan."""

    __canonical_name__ = "arena-hero.validation-result.v1"

    valid: bool
    repaired: bool
    plan: Plan
    issues: tuple[ValidationIssue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.valid, bool):
            raise TypeError("valid must be a boolean")
        if not isinstance(self.repaired, bool):
            raise TypeError("repaired must be a boolean")
        if not isinstance(self.plan, Plan):
            raise TypeError("plan must be a Plan")
        if not isinstance(self.issues, tuple) or any(
            not isinstance(issue, ValidationIssue) for issue in self.issues
        ):
            raise TypeError("issues must be a tuple of ValidationIssue")


def _issue(code: ValidationCode, actor_id: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, actor_id=actor_id, message=message)


def validate_plan(
    snapshot: PlanningSnapshot,
    plan: Plan,
    *,
    obstacles: frozenset[str] | None = None,
) -> ValidationResult:
    """Validate and repair one plan against the observed snapshot."""

    if not isinstance(snapshot, PlanningSnapshot):
        raise TypeError("snapshot must be a PlanningSnapshot")
    if not isinstance(plan, Plan):
        raise TypeError("plan must be a Plan")
    blocked = snapshot.obstacle_cells if obstacles is None else obstacles
    if not isinstance(blocked, frozenset):
        raise TypeError("obstacles must be a frozenset of cell keys or None")

    if plan.tick != snapshot.tick:
        return ValidationResult(
            valid=False,
            repaired=True,
            plan=Plan(tick=snapshot.tick),
            issues=(
                _issue(
                    ValidationCode.TICK_MISMATCH,
                    "plan",
                    f"plan tick {plan.tick} does not match state tick {snapshot.tick}",
                ),
            ),
        )

    issues: list[ValidationIssue] = []
    unit_actions: list[UnitAction] = []
    intents: list[PlanIntent] = []
    units_by_id = {unit.id.value: unit for unit in snapshot.units}

    for action in plan.unit_actions:
        unit = units_by_id.get(action.unit_id.value)
        if unit is None:
            issues.append(
                _issue(
                    ValidationCode.UNKNOWN_UNIT,
                    action.unit_id.value,
                    "unit is not currently controlled",
                )
            )
            continue
        issue = _validate_unit_action(snapshot, unit, action, blocked)
        if issue is not None:
            issues.append(issue)
            continue
        unit_actions.append(action)
        intent = next(
            (candidate for candidate in plan.intents if candidate.actor_id == action.unit_id.value),
            None,
        )
        if intent is not None:
            intents.append(intent)

    core_action = None
    if plan.core_action is not None:
        core_issue = _validate_core_action(snapshot, plan.core_action)
        if core_issue is not None:
            issues.append(core_issue)
        else:
            core_action = plan.core_action
            core_intent = next(
                (candidate for candidate in plan.intents if candidate.actor_id == "core"),
                None,
            )
            if core_intent is not None:
                intents.append(core_intent)

    return ValidationResult(
        valid=not issues,
        repaired=bool(issues),
        plan=Plan(
            tick=snapshot.tick,
            unit_actions=tuple(unit_actions),
            core_action=core_action,
            intents=tuple(intents),
        ),
        issues=tuple(issues),
    )


def _validate_unit_action(
    snapshot: PlanningSnapshot,
    unit: PlanningUnit,
    action: UnitAction,
    blocked: frozenset[str],
) -> ValidationIssue | None:
    actor = unit.id.value

    if action.type in (UnitActionType.WAIT, UnitActionType.SELF_DESTRUCT):
        return None
    if action.type is UnitActionType.MOVE:
        assert action.direction is not None
        destination = unit.position.step(action.direction)
        if destination.cell_key in blocked:
            return _issue(ValidationCode.BLOCKED_MOVE, actor, "destination is a known obstacle")
        return None
    if action.type is UnitActionType.HARVEST:
        if unit.unit_role is not UnitRole.WORKER:
            return _issue(ValidationCode.WRONG_CAPABILITY, actor, "only Workers can harvest")
        if unit.position.cell_key in snapshot.resource_cells:
            return None
        return _issue(
            ValidationCode.INVALID_HARVEST,
            actor,
            "Worker is not standing on a visible resource cell",
        )
    if action.type is UnitActionType.DEPOSIT:
        if unit.unit_role is not UnitRole.WORKER:
            return _issue(ValidationCode.WRONG_CAPABILITY, actor, "only Workers can deposit")
        if unit.cargo <= 0 or snapshot.resource_space <= 0 or snapshot.core_position is None:
            return _issue(
                ValidationCode.INVALID_DEPOSIT,
                actor,
                "deposit requires cargo, Core capacity, and an active Core",
            )
        if unit.position == snapshot.core_position:
            return None
        return _issue(ValidationCode.INVALID_DEPOSIT, actor, "Worker must be on the Core cell")
    if action.type is UnitActionType.SWEEP:
        if unit.unit_role is UnitRole.VANGUARD:
            return None
        return _issue(ValidationCode.WRONG_CAPABILITY, actor, "only Vanguards can sweep")
    if action.type is UnitActionType.SHOOT:
        return _validate_shot(snapshot, unit, action, blocked)
    if action.type is UnitActionType.PICKUP_BEACON:
        if (
            snapshot.beacon.status == _BEACON_GROUND
            and snapshot.beacon.carrier_id is None
            and unit.position == snapshot.beacon.position
        ):
            return None
        return _issue(ValidationCode.INVALID_BEACON, actor, "Beacon is not available on this cell")
    if action.type is UnitActionType.DROP_BEACON:
        if snapshot.beacon.carrier_id is not None and snapshot.beacon.carrier_id.value == actor:
            return None
        return _issue(ValidationCode.INVALID_BEACON, actor, "unit is not carrying the Beacon")
    if action.type is UnitActionType.HEAL:
        if (
            snapshot.core_position is None
            or snapshot.core_state != _CORE_NORMAL
            or unit.position != snapshot.core_position
        ):
            return _issue(
                ValidationCode.INVALID_HEAL,
                actor,
                "unit healing requires a stationary Core on the same cell",
            )
        if unit.health < UNIT_MAX_HP[unit.unit_role]:
            return None
        return _issue(ValidationCode.INVALID_HEAL, actor, "unit is already at maximum HP")
    raise ValueError(f"unsupported unit action type {action.type.value!r}")


def _validate_shot(
    snapshot: PlanningSnapshot,
    unit: PlanningUnit,
    action: UnitAction,
    blocked: frozenset[str],
) -> ValidationIssue | None:
    assert action.expected_cell is not None
    actor = unit.id.value
    if unit.unit_role is not UnitRole.RANGER:
        return _issue(ValidationCode.WRONG_CAPABILITY, actor, "only Rangers can shoot")
    dx = action.expected_cell.x - unit.position.x
    dy = action.expected_cell.y - unit.position.y
    distance = max(abs(dx), abs(dy))
    aligned = dx == 0 or dy == 0 or abs(dx) == abs(dy)
    blocked_cells = frozenset(Coordinate.parse_cell_key(key) for key in blocked)
    if (
        distance < 1
        or distance > RANGER_SHOOT_RANGE
        or not aligned
        or shot_line_blocked(unit.position, action.expected_cell, blocked_cells)
    ):
        return _issue(
            ValidationCode.INVALID_SHOT, actor, "target cell is out of line-of-sight range"
        )
    if action.target_id is not None:
        target = next(
            (enemy for enemy in snapshot.enemy_units if enemy.id == action.target_id),
            None,
        )
        if target is None or target.position != action.expected_cell:
            return _issue(
                ValidationCode.INVALID_SHOT, actor, "target is not visible at expected_cell"
            )
    return None


def _validate_core_action(
    snapshot: PlanningSnapshot,
    action: CoreAction,
) -> ValidationIssue | None:
    actor = snapshot.core_id if snapshot.core_id is not None else "core"

    if snapshot.core_position is None:
        return _issue(ValidationCode.MISSING_CORE, actor, "no controlled Core is available")

    if action.type is CoreActionType.WAIT:
        return None
    if action.type is CoreActionType.HEAL:
        if snapshot.core_health is not None and snapshot.core_health < CORE_MAX_HP:
            return None
        return _issue(ValidationCode.CORE_UNAVAILABLE, actor, "Core is already at maximum HP")
    if action.type is CoreActionType.REPAIR_SHIELD:
        owns_beacon = (
            snapshot.beacon.status == _BEACON_CARRIED
            and snapshot.beacon.carrier_id is not None
            and any(unit.id.value == snapshot.beacon.carrier_id.value for unit in snapshot.units)
        )
        shield_cap = CORE_SHIELD_CAP_WITH_BEACON if owns_beacon else CORE_SHIELD_CAP
        core_shield = snapshot.core_shield
        if core_shield is None or snapshot.core_state != _CORE_NORMAL or core_shield >= shield_cap:
            return _issue(
                ValidationCode.CORE_UNAVAILABLE,
                actor,
                "shield repair requires a stationary damaged Core",
            )
        if snapshot.resources >= 1:
            return None
        return _issue(
            ValidationCode.INSUFFICIENT_RESOURCES,
            actor,
            "shield repair costs one resource",
        )
    if action.type is CoreActionType.SPAWN:
        assert action.unit_role is not None
        if snapshot.core_state != _CORE_NORMAL:
            return _issue(ValidationCode.CORE_UNAVAILABLE, actor, "moving Core cannot spawn")
        spawn_cost = unit_price(action.unit_role, snapshot.population, CURRENT_RULES_VERSION)
        if snapshot.resources >= spawn_cost:
            return None
        return _issue(
            ValidationCode.INSUFFICIENT_RESOURCES,
            actor,
            f"spawn {action.unit_role.name} costs {spawn_cost}",
        )
    if action.type is CoreActionType.START_MOVE:
        if snapshot.core_state == _CORE_NORMAL:
            return None
        return _issue(ValidationCode.CORE_UNAVAILABLE, actor, "Core is already moving")
    if action.type is CoreActionType.CANCEL_MOVE:
        if snapshot.core_state == _CORE_MOVING:
            return None
        return _issue(ValidationCode.CORE_UNAVAILABLE, actor, "Core is not moving")
    if action.type is CoreActionType.PICKUP_BEACON:
        if (
            snapshot.beacon.status == _BEACON_GROUND
            and snapshot.core_position == snapshot.beacon.position
        ):
            return None
        return _issue(
            ValidationCode.INVALID_BEACON,
            actor,
            "Beacon is not available on the Core cell",
        )
    if action.type is CoreActionType.DROP_BEACON:
        if (
            snapshot.beacon.carrier_id is not None
            and snapshot.core_id is not None
            and snapshot.beacon.carrier_id.value == snapshot.core_id
        ):
            return None
        return _issue(ValidationCode.INVALID_BEACON, actor, "Core is not carrying the Beacon")
    if action.type is CoreActionType.SELF_DESTRUCT:
        return None
    raise ValueError(f"unsupported core action type {action.type.value!r}")
