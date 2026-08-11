"""Deterministic planning-layer plan DTOs mirroring the legacy TS ``Plan`` shape.

The application layer owns its own ``Decision``/``UnitIntent`` DTOs; planning stays
independent so deterministic candidate generation can be validated, repaired, and
compared against the TS oracle without an application dependency. Actions are
canonicalized and fail closed on contradictory shapes (for example a ``SHOOT``
without an expected cell).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from arena_hero_agent.domain import (
    Coordinate,
    Direction,
    EntityId,
    UnitRole,
    codepoint_order_key,
)

_MAX_SAFE_INTEGER = 2**53 - 1


def _safe_int(name: str, value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if value > _MAX_SAFE_INTEGER:
        raise ValueError(f"{name} exceeds the cross-language safe-integer range")
    return value


class UnitActionType(StrEnum):
    """Unit action kinds accepted by the deterministic planner and validator."""

    __canonical_name__ = "arena-hero.plan-unit-action-type.v1"

    WAIT = "wait"
    MOVE = "move"
    HARVEST = "harvest"
    DEPOSIT = "deposit"
    SWEEP = "sweep"
    SHOOT = "shoot"
    PICKUP_BEACON = "pickup_beacon"
    DROP_BEACON = "drop_beacon"
    SELF_DESTRUCT = "self_destruct"
    HEAL = "heal"


class CoreActionType(StrEnum):
    """Core action kinds accepted by the deterministic planner and validator."""

    __canonical_name__ = "arena-hero.plan-core-action-type.v1"

    WAIT = "wait"
    SPAWN = "spawn"
    REPAIR_SHIELD = "repair_shield"
    HEAL = "heal"
    START_MOVE = "start_move"
    CANCEL_MOVE = "cancel_move"
    PICKUP_BEACON = "pickup_beacon"
    DROP_BEACON = "drop_beacon"
    SELF_DESTRUCT = "self_destruct"


@dataclass(frozen=True, slots=True)
class UnitAction:
    """One deterministic action for one controlled unit."""

    __canonical_name__ = "arena-hero.plan-unit-action.v1"

    unit_id: EntityId
    type: UnitActionType
    direction: Direction | None = None
    target_id: EntityId | None = None
    expected_cell: Coordinate | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, EntityId):
            raise TypeError("unit action unit_id must be an EntityId")
        if not isinstance(self.type, UnitActionType):
            raise TypeError("unit action type must be a UnitActionType")
        if self.direction is not None and not isinstance(self.direction, Direction):
            raise TypeError("unit action direction must be a Direction or None")
        if self.target_id is not None and not isinstance(self.target_id, EntityId):
            raise TypeError("unit action target_id must be an EntityId or None")
        if self.expected_cell is not None and not isinstance(self.expected_cell, Coordinate):
            raise TypeError("unit action expected_cell must be a Coordinate or None")
        direction_actions = {UnitActionType.MOVE, UnitActionType.SWEEP}
        if (self.type in direction_actions) != (self.direction is not None):
            raise ValueError("move and sweep actions require exactly one direction")
        if self.type is UnitActionType.SHOOT:
            if self.expected_cell is None:
                raise ValueError("shoot actions require expected_cell")
        elif self.target_id is not None or self.expected_cell is not None:
            raise ValueError("only shoot actions may declare target_id or expected_cell")


@dataclass(frozen=True, slots=True)
class CoreAction:
    """One deterministic action for the controlled core."""

    __canonical_name__ = "arena-hero.plan-core-action.v1"

    type: CoreActionType
    direction: Direction | None = None
    unit_role: UnitRole | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, CoreActionType):
            raise TypeError("core action type must be a CoreActionType")
        if self.direction is not None and not isinstance(self.direction, Direction):
            raise TypeError("core action direction must be a Direction or None")
        if self.unit_role is not None and not isinstance(self.unit_role, UnitRole):
            raise TypeError("core action unit_role must be a UnitRole or None")
        if (self.type is CoreActionType.START_MOVE) != (self.direction is not None):
            raise ValueError("start-move actions require exactly one direction")
        if (self.type is CoreActionType.SPAWN) != (self.unit_role is not None):
            raise ValueError("spawn actions require exactly one unit_role")


@dataclass(frozen=True, slots=True)
class PlanIntent:
    """Planner annotation attached to one accepted action."""

    __canonical_name__ = "arena-hero.plan-intent.v1"

    actor_id: str
    intent: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id:
            raise ValueError("intent actor_id must be a non-empty string")
        if not isinstance(self.intent, str) or not self.intent:
            raise ValueError("intent must be a non-empty string")


@dataclass(frozen=True, slots=True)
class Plan:
    """Deterministic plan for one tick: unit actions, optional core action, intents."""

    __canonical_name__ = "arena-hero.plan.v1"

    tick: int
    unit_actions: tuple[UnitAction, ...] = ()
    core_action: CoreAction | None = None
    intents: tuple[PlanIntent, ...] = ()

    def __post_init__(self) -> None:
        _safe_int("plan tick", self.tick, minimum=1)
        if isinstance(self.unit_actions, str | bytes) or not isinstance(
            self.unit_actions, Iterable
        ):
            raise TypeError("unit_actions must be an iterable of UnitAction")
        normalized = tuple(self.unit_actions)
        if any(not isinstance(action, UnitAction) for action in normalized):
            raise TypeError("unit_actions must contain only UnitAction values")
        ids = [action.unit_id.value for action in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate unit action identity")
        object.__setattr__(
            self,
            "unit_actions",
            tuple(
                sorted(
                    normalized,
                    key=lambda action: codepoint_order_key(action.unit_id.value),
                )
            ),
        )
        if self.core_action is not None and not isinstance(self.core_action, CoreAction):
            raise TypeError("core_action must be a CoreAction or None")
        if isinstance(self.intents, str | bytes) or not isinstance(self.intents, Iterable):
            raise TypeError("intents must be an iterable of PlanIntent")
        normalized_intents = tuple(self.intents)
        if any(not isinstance(intent, PlanIntent) for intent in normalized_intents):
            raise TypeError("intents must contain only PlanIntent values")
        object.__setattr__(
            self,
            "intents",
            tuple(
                sorted(
                    normalized_intents,
                    key=lambda intent: codepoint_order_key(intent.actor_id),
                )
            ),
        )

    def action_for(self, unit_id: str) -> UnitAction | None:
        """Return the action for one unit id, or None when the unit is not planned."""

        for action in self.unit_actions:
            if action.unit_id.value == unit_id:
                return action
        return None
