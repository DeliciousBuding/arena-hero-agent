"""Application-owned immutable turn and decision DTOs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from arena_hero_agent.domain import (
    Coordinate,
    Direction,
    EntityId,
    UnitRole,
    WorldProjection,
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


def _canonical_text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty canonical text")
    return value


class PlayerLifecycle(StrEnum):
    """Reliable player lifecycle states exposed to application policy."""

    __canonical_name__ = "arena-hero.player-lifecycle.v1"

    ACTIVE = "active"
    RESPAWNING = "respawning"


class UnitAction(StrEnum):
    """Application actions supported for controlled units."""

    __canonical_name__ = "arena-hero.unit-action.v1"

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


class CoreAction(StrEnum):
    """Application actions supported for the controlled core."""

    __canonical_name__ = "arena-hero.core-action.v1"

    WAIT = "wait"
    SPAWN = "spawn"
    REPAIR_SHIELD = "repair_shield"
    START_MOVE = "start_move"
    CANCEL_MOVE = "cancel_move"
    PICKUP_BEACON = "pickup_beacon"
    DROP_BEACON = "drop_beacon"
    SELF_DESTRUCT = "self_destruct"
    HEAL = "heal"


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """Stable event envelope; SDK-specific values remain outside the application DTO."""

    __canonical_name__ = "arena-hero.turn-event.v1"

    id: EntityId
    tick: int
    kind: str
    reason: str | None = None
    actor_id: EntityId | None = None
    target_id: EntityId | None = None
    position: Coordinate | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, EntityId):
            raise TypeError("event id must be an EntityId")
        _safe_int("event tick", self.tick, minimum=1)
        _canonical_text("event kind", self.kind)
        if self.reason is not None:
            _canonical_text("event reason", self.reason)
        if self.actor_id is not None and not isinstance(self.actor_id, EntityId):
            raise TypeError("event actor_id must be an EntityId or None")
        if self.target_id is not None and not isinstance(self.target_id, EntityId):
            raise TypeError("event target_id must be an EntityId or None")
        if self.position is not None and not isinstance(self.position, Coordinate):
            raise TypeError("event position must be a Coordinate or None")


@dataclass(frozen=True, slots=True)
class TurnObservation:
    """Minimal application view of one SDK turn."""

    __canonical_name__ = "arena-hero.turn-observation.v1"

    tick: int
    lifecycle: PlayerLifecycle
    resources: int
    population: int
    projection: WorldProjection
    events: tuple[TurnEvent, ...] = ()
    respawn_at_tick: int | None = None

    def __post_init__(self) -> None:
        _safe_int("turn tick", self.tick, minimum=1)
        if not isinstance(self.lifecycle, PlayerLifecycle):
            raise TypeError("turn lifecycle must be a PlayerLifecycle")
        _safe_int("turn resources", self.resources)
        _safe_int("turn population", self.population)
        if not isinstance(self.projection, WorldProjection):
            raise TypeError("turn projection must be a WorldProjection")
        if self.projection.tick != self.tick:
            raise ValueError("turn projection tick must match turn tick")
        if self.respawn_at_tick is not None:
            _safe_int("turn respawn_at_tick", self.respawn_at_tick, minimum=1)
        if self.lifecycle is PlayerLifecycle.RESPAWNING and self.respawn_at_tick is None:
            raise ValueError("respawning turns require respawn_at_tick")
        if self.lifecycle is PlayerLifecycle.ACTIVE and self.respawn_at_tick is not None:
            raise ValueError("active turns cannot declare respawn_at_tick")
        normalized = _normalize_events(self.events)
        object.__setattr__(self, "events", normalized)


def _normalize_events(events: Iterable[TurnEvent]) -> tuple[TurnEvent, ...]:
    if isinstance(events, str | bytes) or not isinstance(events, Iterable):
        raise TypeError("events must be an iterable of TurnEvent")
    normalized = tuple(events)
    if any(not isinstance(event, TurnEvent) for event in normalized):
        raise TypeError("events must contain only TurnEvent values")
    ids = [event.id.value for event in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate event identity")
    return tuple(sorted(normalized, key=lambda event: codepoint_order_key(event.id.value)))


@dataclass(frozen=True, slots=True)
class UnitIntent:
    """One deterministic intent for one controlled unit."""

    __canonical_name__ = "arena-hero.unit-intent.v1"

    unit_id: EntityId
    action: UnitAction
    direction: Direction | None = None
    target_id: EntityId | None = None
    expected_cell: Coordinate | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, EntityId):
            raise TypeError("unit intent unit_id must be an EntityId")
        if not isinstance(self.action, UnitAction):
            raise TypeError("unit intent action must be a UnitAction")
        if self.direction is not None and not isinstance(self.direction, Direction):
            raise TypeError("unit intent direction must be a Direction or None")
        if self.target_id is not None and not isinstance(self.target_id, EntityId):
            raise TypeError("unit intent target_id must be an EntityId or None")
        if self.expected_cell is not None and not isinstance(self.expected_cell, Coordinate):
            raise TypeError("unit intent expected_cell must be a Coordinate or None")
        direction_actions = {UnitAction.MOVE, UnitAction.SWEEP}
        if (self.action in direction_actions) != (self.direction is not None):
            raise ValueError("move and sweep intents require exactly one direction")
        if self.action is UnitAction.SHOOT:
            if self.expected_cell is None:
                raise ValueError("shoot intents require expected_cell")
        elif self.target_id is not None or self.expected_cell is not None:
            raise ValueError("only shoot intents may declare target_id or expected_cell")


@dataclass(frozen=True, slots=True)
class CoreIntent:
    """One deterministic intent for the controlled core."""

    __canonical_name__ = "arena-hero.core-intent.v1"

    action: CoreAction
    direction: Direction | None = None
    unit_role: UnitRole | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, CoreAction):
            raise TypeError("core intent action must be a CoreAction")
        if self.direction is not None and not isinstance(self.direction, Direction):
            raise TypeError("core intent direction must be a Direction or None")
        if self.unit_role is not None and not isinstance(self.unit_role, UnitRole):
            raise TypeError("core intent unit_role must be a UnitRole or None")
        if (self.action is CoreAction.START_MOVE) != (self.direction is not None):
            raise ValueError("start-move intents require exactly one direction")
        if (self.action is CoreAction.SPAWN) != (self.unit_role is not None):
            raise ValueError("spawn intents require exactly one unit_role")


@dataclass(frozen=True, slots=True)
class Decision:
    """Immutable application decision for a single observed tick."""

    __canonical_name__ = "arena-hero.decision.v1"

    tick: int
    unit_intents: tuple[UnitIntent, ...] = ()
    core_intent: CoreIntent | None = None

    def __post_init__(self) -> None:
        _safe_int("decision tick", self.tick, minimum=1)
        if isinstance(self.unit_intents, str | bytes) or not isinstance(
            self.unit_intents, Iterable
        ):
            raise TypeError("unit_intents must be an iterable of UnitIntent")
        normalized = tuple(self.unit_intents)
        if any(not isinstance(intent, UnitIntent) for intent in normalized):
            raise TypeError("unit_intents must contain only UnitIntent values")
        ids = [intent.unit_id.value for intent in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate unit intent identity")
        object.__setattr__(
            self,
            "unit_intents",
            tuple(sorted(normalized, key=lambda intent: codepoint_order_key(intent.unit_id.value))),
        )
        if self.core_intent is not None and not isinstance(self.core_intent, CoreIntent):
            raise TypeError("core_intent must be a CoreIntent or None")
