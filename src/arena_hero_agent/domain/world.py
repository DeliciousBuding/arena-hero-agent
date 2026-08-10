"""Immutable semantic world observations and canonical projection."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from .navigation import Bounds, NavigationGrid
from .rules import RulesVersion
from .value_objects import (
    Coordinate,
    EntityId,
    StateDigest,
    _require_int,
    codepoint_order_key,
)

_Observation = TypeVar("_Observation")
_MAX_SAFE_INTEGER = 2**53 - 1


def _nonnegative_int(name: str, value: object) -> int:
    checked = _require_int(name, value)
    if checked < 0:
        raise ValueError(f"{name} cannot be negative")
    if checked > _MAX_SAFE_INTEGER:
        raise ValueError(f"{name} exceeds the cross-language safe-integer range")
    return checked


def _nonempty_text(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty canonical text")
    return value


def _normalized_tuple(
    name: str,
    values: Iterable[_Observation],
    expected_type: type[_Observation],
    *,
    key: Callable[[_Observation], tuple[int, ...]],
) -> tuple[_Observation, ...]:
    if isinstance(values, str | bytes) or not isinstance(values, Iterable):
        raise TypeError(f"{name} must be an iterable of {expected_type.__name__}")
    normalized = tuple(values)
    if any(not isinstance(value, expected_type) for value in normalized):
        raise TypeError(f"{name} must contain only {expected_type.__name__} values")
    return tuple(sorted(normalized, key=key))


def _identifier_key(value: UnitObservation | EntityObservation) -> tuple[int, ...]:
    return codepoint_order_key(value.id.value)


def _coordinate_key(
    value: ResourceObservation | TerrainObservation,
) -> tuple[int, int]:
    return value.position.x, value.position.y


def _duplicate_values(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates, key=codepoint_order_key))


class UnitRole(StrEnum):
    """Semantic controlled-unit role, independent of SDK enums."""

    __canonical_name__ = "arena-hero.unit-role.v1"

    WORKER = "worker"
    VANGUARD = "vanguard"
    RANGER = "ranger"


class EntityKind(StrEnum):
    """Kind of externally observed world entity."""

    __canonical_name__ = "arena-hero.entity-kind.v1"

    UNIT = "unit"
    CORE = "core"


class CoreState(StrEnum):
    """Semantic core mobility state."""

    __canonical_name__ = "arena-hero.core-state.v1"

    NORMAL = "normal"
    MOVING = "moving"


class TerrainState(StrEnum):
    """Known terrain state; absence from observations remains unknown."""

    __canonical_name__ = "arena-hero.terrain-state.v1"

    OPEN = "open"
    BLOCKED = "blocked"


class BeaconStatus(StrEnum):
    """Beacon carrier knowledge with fog represented explicitly."""

    __canonical_name__ = "arena-hero.beacon-status.v1"

    UNKNOWN = "unknown"
    GROUND = "ground"
    CARRIED = "carried"


@dataclass(frozen=True, slots=True)
class UnitObservation:
    """Minimal semantic observation of a controlled unit."""

    __canonical_name__ = "arena-hero.unit-observation.v1"

    id: EntityId
    position: Coordinate
    role: UnitRole
    health: int
    cargo: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.id, EntityId):
            raise TypeError("unit id must be an EntityId")
        if not isinstance(self.position, Coordinate):
            raise TypeError("unit position must be a Coordinate")
        if not isinstance(self.role, UnitRole):
            raise TypeError("unit role must be a UnitRole")
        _nonnegative_int("unit health", self.health)
        _nonnegative_int("unit cargo", self.cargo)


@dataclass(frozen=True, slots=True)
class EntityObservation:
    """Minimal semantic observation of an external unit or core."""

    __canonical_name__ = "arena-hero.entity-observation.v1"

    id: EntityId
    kind: EntityKind
    position: Coordinate
    health: int
    owner: str | None = None
    unit_role: UnitRole | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, EntityId):
            raise TypeError("entity id must be an EntityId")
        if not isinstance(self.kind, EntityKind):
            raise TypeError("entity kind must be an EntityKind")
        if not isinstance(self.position, Coordinate):
            raise TypeError("entity position must be a Coordinate")
        _nonnegative_int("entity health", self.health)
        if self.owner is not None:
            _nonempty_text("entity owner", self.owner)
        if self.kind is EntityKind.UNIT and self.unit_role is None:
            raise ValueError("unit entity observations require unit_role")
        if self.kind is EntityKind.CORE and self.unit_role is not None:
            raise ValueError("core entity observations cannot declare unit_role")


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    """Observed resource presence, optionally with a known remaining amount."""

    __canonical_name__ = "arena-hero.resource-observation.v1"

    position: Coordinate
    remaining: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.position, Coordinate):
            raise TypeError("resource position must be a Coordinate")
        if self.remaining is not None:
            _nonnegative_int("resource remaining", self.remaining)


@dataclass(frozen=True, slots=True)
class TerrainObservation:
    """Observed open or blocked terrain at one coordinate."""

    __canonical_name__ = "arena-hero.terrain-observation.v1"

    position: Coordinate
    state: TerrainState

    def __post_init__(self) -> None:
        if not isinstance(self.position, Coordinate):
            raise TypeError("terrain position must be a Coordinate")
        if not isinstance(self.state, TerrainState):
            raise TypeError("terrain state must be a TerrainState")


@dataclass(frozen=True, slots=True)
class BeaconObservation:
    """Beacon position plus explicitly known, unknown, or carried status."""

    __canonical_name__ = "arena-hero.beacon-observation.v1"

    position: Coordinate
    status: BeaconStatus
    carrier_id: EntityId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.position, Coordinate):
            raise TypeError("beacon position must be a Coordinate")
        if not isinstance(self.status, BeaconStatus):
            raise TypeError("beacon status must be a BeaconStatus")
        if self.carrier_id is not None and not isinstance(self.carrier_id, EntityId):
            raise TypeError("beacon carrier_id must be an EntityId or None")
        if self.status is BeaconStatus.CARRIED and self.carrier_id is None:
            raise ValueError("carried beacon observations require carrier_id")
        if self.status is not BeaconStatus.CARRIED and self.carrier_id is not None:
            raise ValueError("only carried beacon observations may declare carrier_id")


@dataclass(frozen=True, slots=True)
class CoreObservation:
    """Minimal semantic observation of the controlled core."""

    __canonical_name__ = "arena-hero.core-observation.v1"

    id: EntityId
    position: Coordinate
    health: int
    shield: int
    state: CoreState
    owner: str
    destination: Coordinate | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, EntityId):
            raise TypeError("core id must be an EntityId")
        if not isinstance(self.position, Coordinate):
            raise TypeError("core position must be a Coordinate")
        if not isinstance(self.state, CoreState):
            raise TypeError("core state must be a CoreState")
        _nonnegative_int("core health", self.health)
        _nonnegative_int("core shield", self.shield)
        _nonempty_text("core owner", self.owner)
        if self.destination is not None and not isinstance(self.destination, Coordinate):
            raise TypeError("core destination must be a Coordinate or None")
        if self.state is CoreState.MOVING and self.destination is None:
            raise ValueError("moving core observations require a destination")
        if self.state is CoreState.NORMAL and self.destination is not None:
            raise ValueError("normal core observations cannot declare a destination")


@dataclass(frozen=True, slots=True)
class WorldProjection:
    """Canonical immutable world projection for pure consumers and state digests."""

    __canonical_name__ = "arena-hero.world-projection.v1"

    tick: int
    rules_version: RulesVersion
    core: CoreObservation | None = None
    units: tuple[UnitObservation, ...] = ()
    entities: tuple[EntityObservation, ...] = ()
    resources: tuple[ResourceObservation, ...] = ()
    terrain: tuple[TerrainObservation, ...] = ()
    beacon: BeaconObservation | None = None

    def __post_init__(self) -> None:
        _nonnegative_int("world tick", self.tick)
        if not isinstance(self.rules_version, RulesVersion):
            raise TypeError("rules_version must be a RulesVersion")
        if self.core is not None and not isinstance(self.core, CoreObservation):
            raise TypeError("core must be a CoreObservation or None")
        if self.beacon is not None and not isinstance(self.beacon, BeaconObservation):
            raise TypeError("beacon must be a BeaconObservation or None")

        units = _normalized_tuple("units", self.units, UnitObservation, key=_identifier_key)
        entities = _normalized_tuple(
            "entities", self.entities, EntityObservation, key=_identifier_key
        )
        resources = _normalized_tuple(
            "resources", self.resources, ResourceObservation, key=_coordinate_key
        )
        terrain = _normalized_tuple(
            "terrain", self.terrain, TerrainObservation, key=_coordinate_key
        )
        object.__setattr__(self, "units", units)
        object.__setattr__(self, "entities", entities)
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "terrain", terrain)

        identifiers = [unit.id.value for unit in units]
        identifiers.extend(entity.id.value for entity in entities)
        if self.core is not None:
            identifiers.append(self.core.id.value)
        duplicate_ids = _duplicate_values(identifiers)
        if duplicate_ids:
            raise ValueError(f"duplicate world entity ids: {', '.join(duplicate_ids)}")

        duplicate_resources = _duplicate_values(
            resource.position.cell_key for resource in resources
        )
        if duplicate_resources:
            raise ValueError(f"duplicate resource cells: {', '.join(duplicate_resources)}")
        duplicate_terrain = _duplicate_values(
            observation.position.cell_key for observation in terrain
        )
        if duplicate_terrain:
            raise ValueError(f"duplicate terrain cells: {', '.join(duplicate_terrain)}")

        blocked_cells = {
            observation.position
            for observation in terrain
            if observation.state is TerrainState.BLOCKED
        }
        resource_conflicts = sorted(
            resource.position for resource in resources if resource.position in blocked_cells
        )
        if resource_conflicts:
            cells = ", ".join(position.cell_key for position in resource_conflicts)
            raise ValueError(f"resource cells cannot also be blocked terrain: {cells}")

    @property
    def state_digest(self) -> StateDigest:
        """Digest the typed canonical projection without wire-format hashing."""

        return StateDigest.from_state(self)

    def navigation_grid(self, *, bounds: Bounds | None = None) -> NavigationGrid:
        """Project known terrain into the navigation seam; omitted cells stay unknown."""

        open_cells = frozenset(
            observation.position
            for observation in self.terrain
            if observation.state is TerrainState.OPEN
        )
        blocked_cells = frozenset(
            observation.position
            for observation in self.terrain
            if observation.state is TerrainState.BLOCKED
        )
        return NavigationGrid(open_cells=open_cells, blocked_cells=blocked_cells, bounds=bounds)
