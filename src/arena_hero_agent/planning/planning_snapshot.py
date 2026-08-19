"""Deterministic planning snapshot extracted from the domain world/economy.

The legacy TS ``extractPlanningSnapshot`` downsamples the full tick state into the
fields planning cares about and precomputes a distance-decayed threat map. This
module is the Python equivalent: a pure, deterministic projection that never
mutates the domain values it reads. Cell keys use the canonical ``x,y`` form.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from arena_hero_agent.domain import (
    Coordinate,
    EconomyState,
    EntityId,
    EntityKind,
    RulesVersion,
    TerrainState,
    UnitRole,
    WorldProjection,
    cell_key,
)

from .plan import _safe_int

THREAT_RADIUS = 3


def threat_contribution(distance: int) -> float:
    """Inverse-distance decay: the enemy's own cell contributes exactly 1."""

    checked = _safe_int("threat distance", distance)
    return 1.0 / (1.0 + checked)


@dataclass(frozen=True, slots=True)
class EnemyUnit:
    """One visible hostile unit in planning terms."""

    __canonical_name__ = "arena-hero.planning-enemy-unit.v1"

    id: EntityId
    position: Coordinate
    unit_role: UnitRole

    def __post_init__(self) -> None:
        if not isinstance(self.id, EntityId):
            raise TypeError("enemy id must be an EntityId")
        if not isinstance(self.position, Coordinate):
            raise TypeError("enemy position must be a Coordinate")
        if not isinstance(self.unit_role, UnitRole):
            raise TypeError("enemy unit_role must be a UnitRole")


@dataclass(frozen=True, slots=True)
class MoveFailureEvent:
    """One unit move-resolution failure from the previous tick.

    Only the unit id and engine reason string are kept; the blocked cell is
    derived by pairing the failure with the previous planned direction, so no
    position needs to be stored here.
    """

    __canonical_name__ = "arena-hero.planning-move-failure.v1"

    unit_id: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.unit_id, str) or not self.unit_id:
            raise TypeError("move failure unit_id must be a non-empty string")
        if not isinstance(self.reason, str) or not self.reason:
            raise TypeError("move failure reason must be a non-empty string")


@dataclass(frozen=True, slots=True)
class PlanningUnit:
    """One controlled unit downsample for planning."""

    __canonical_name__ = "arena-hero.planning-unit.v1"

    id: EntityId
    unit_role: UnitRole
    position: Coordinate
    health: int
    cargo: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.id, EntityId):
            raise TypeError("planning unit id must be an EntityId")
        if not isinstance(self.unit_role, UnitRole):
            raise TypeError("planning unit unit_role must be a UnitRole")
        if not isinstance(self.position, Coordinate):
            raise TypeError("planning unit position must be a Coordinate")
        _safe_int("planning unit health", self.health)
        _safe_int("planning unit cargo", self.cargo)


@dataclass(frozen=True, slots=True)
class ResourceCellInfo:
    """Known resource cell metadata used by task and mission planning."""

    __canonical_name__ = "arena-hero.planning-resource-cell.v1"

    position: Coordinate
    visible: bool
    last_seen_tick: int | None = None
    seeded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.position, Coordinate):
            raise TypeError("resource cell position must be a Coordinate")
        if not isinstance(self.visible, bool):
            raise TypeError("resource cell visible must be a boolean")
        if self.last_seen_tick is not None:
            _safe_int("resource cell last_seen_tick", self.last_seen_tick, minimum=1)
        if not isinstance(self.seeded, bool):
            raise TypeError("resource cell seeded must be a boolean")


@dataclass(frozen=True, slots=True)
class BeaconInfo:
    """Planning beacon knowledge; None status means the beacon cell is unseen."""

    __canonical_name__ = "arena-hero.planning-beacon.v1"

    position: Coordinate
    status: str | None = None
    carrier_id: EntityId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.position, Coordinate):
            raise TypeError("beacon position must be a Coordinate")
        if self.status is not None and not isinstance(self.status, str):
            raise TypeError("beacon status must be a string or None")
        if self.carrier_id is not None and not isinstance(self.carrier_id, EntityId):
            raise TypeError("beacon carrier_id must be an EntityId or None")


@dataclass(frozen=True, slots=True)
class PlanningSnapshot:
    """Immutable downsample consumed by deterministic planning layers."""

    __canonical_name__ = "arena-hero.planning-snapshot.v1"

    tick: int
    rules_version: RulesVersion
    resources: int
    resource_capacity: int
    resource_space: int
    population: int
    units: tuple[PlanningUnit, ...]
    resource_cells: Mapping[str, ResourceCellInfo]
    obstacle_cells: frozenset[str]
    enemy_cells: frozenset[str]
    enemy_units: tuple[EnemyUnit, ...]
    core_id: str | None
    core_position: Coordinate | None
    core_health: int | None
    core_shield: int | None
    core_state: str | None
    beacon: BeaconInfo
    threat_map: Mapping[str, float]
    move_failures: tuple[MoveFailureEvent, ...] = ()

    def __post_init__(self) -> None:
        _safe_int("snapshot tick", self.tick, minimum=1)
        if not isinstance(self.rules_version, RulesVersion):
            raise TypeError("snapshot rules_version must be a RulesVersion")
        _safe_int("snapshot resources", self.resources)
        _safe_int("snapshot resource_capacity", self.resource_capacity)
        _safe_int("snapshot resource_space", self.resource_space)
        _safe_int("snapshot population", self.population)
        if not isinstance(self.units, tuple) or any(
            not isinstance(unit, PlanningUnit) for unit in self.units
        ):
            raise TypeError("snapshot units must be a tuple of PlanningUnit")
        if not isinstance(self.resource_cells, Mapping):
            raise TypeError("snapshot resource_cells must be a Mapping")
        if not isinstance(self.obstacle_cells, frozenset) or any(
            not isinstance(key, str) for key in self.obstacle_cells
        ):
            raise TypeError("snapshot obstacle_cells must be a frozenset of cell keys")
        if not isinstance(self.enemy_cells, frozenset) or any(
            not isinstance(key, str) for key in self.enemy_cells
        ):
            raise TypeError("snapshot enemy_cells must be a frozenset of cell keys")
        if not isinstance(self.enemy_units, tuple) or any(
            not isinstance(enemy, EnemyUnit) for enemy in self.enemy_units
        ):
            raise TypeError("snapshot enemy_units must be a tuple of EnemyUnit")
        if self.core_id is not None and not isinstance(self.core_id, str):
            raise TypeError("snapshot core_id must be a string or None")
        if self.core_position is not None and not isinstance(self.core_position, Coordinate):
            raise TypeError("snapshot core_position must be a Coordinate or None")
        if self.core_health is not None:
            _safe_int("snapshot core_health", self.core_health)
        if self.core_shield is not None:
            _safe_int("snapshot core_shield", self.core_shield)
        if self.core_state is not None and not isinstance(self.core_state, str):
            raise TypeError("snapshot core_state must be a string or None")
        if not isinstance(self.beacon, BeaconInfo):
            raise TypeError("snapshot beacon must be a BeaconInfo")
        if not isinstance(self.threat_map, Mapping):
            raise TypeError("snapshot threat_map must be a Mapping")
        if not isinstance(self.move_failures, tuple) or any(
            not isinstance(failure, MoveFailureEvent) for failure in self.move_failures
        ):
            raise TypeError("snapshot move_failures must be a tuple of MoveFailureEvent")

    @property
    def core_present(self) -> bool:
        """True when a controlled core was observed at this tick."""

        return self.core_position is not None


def build_threat_map(enemies: tuple[EnemyUnit, ...]) -> dict[str, float]:
    """Build the distance-decayed threat map from visible enemy units.

    Contributions accumulate in enemy input order and per-enemy in fixed
    (dx, dy) order, matching the oracle's IEEE-754 accumulation bit-for-bit.
    """

    if not isinstance(enemies, tuple) or any(not isinstance(enemy, EnemyUnit) for enemy in enemies):
        raise TypeError("enemies must be a tuple of EnemyUnit")
    threat: dict[str, float] = {}
    for enemy in enemies:
        ex = enemy.position.x
        ey = enemy.position.y
        for dx in range(-THREAT_RADIUS, THREAT_RADIUS + 1):
            for dy in range(-THREAT_RADIUS, THREAT_RADIUS + 1):
                distance = abs(dx) + abs(dy)
                if distance > THREAT_RADIUS:
                    continue
                key = cell_key(Coordinate(ex + dx, ey + dy))
                threat[key] = threat.get(key, 0.0) + threat_contribution(distance)
    return threat


def extract_planning_snapshot(
    projection: WorldProjection,
    economy: EconomyState,
) -> PlanningSnapshot:
    """Project one world observation plus its economy state into a snapshot."""

    if not isinstance(projection, WorldProjection):
        raise TypeError("projection must be a WorldProjection")
    if not isinstance(economy, EconomyState):
        raise TypeError("economy must be an EconomyState")
    if economy.tick != projection.tick:
        raise ValueError("economy tick does not match projection tick")
    if economy.rules_version is not projection.rules_version:
        raise ValueError("economy rules_version does not match projection rules_version")

    units = tuple(
        PlanningUnit(
            id=unit.id,
            unit_role=unit.role,
            position=unit.position,
            health=unit.health,
            cargo=unit.cargo,
        )
        for unit in projection.units
    )
    enemy_units = tuple(
        EnemyUnit(id=entity.id, position=entity.position, unit_role=entity.unit_role)
        for entity in projection.entities
        if entity.kind is EntityKind.UNIT and entity.unit_role is not None
    )
    enemy_cells = frozenset(entity.position.cell_key for entity in projection.entities)
    resource_cells = {
        resource.position.cell_key: ResourceCellInfo(
            position=resource.position,
            visible=True,
            last_seen_tick=projection.tick,
        )
        for resource in projection.resources
    }
    obstacle_cells = frozenset(
        observation.position.cell_key
        for observation in projection.terrain
        if observation.state is TerrainState.BLOCKED
    )
    core = projection.core
    beacon = projection.beacon
    if beacon is None:
        beacon_info = BeaconInfo(position=Coordinate(0, 0), status=None, carrier_id=None)
    else:
        beacon_info = BeaconInfo(
            position=beacon.position,
            status=None if beacon.status.value == "unknown" else beacon.status.value,
            carrier_id=beacon.carrier_id,
        )
    return PlanningSnapshot(
        tick=projection.tick,
        rules_version=projection.rules_version,
        resources=economy.resources,
        resource_capacity=economy.resource_capacity,
        resource_space=economy.resource_space,
        population=economy.population,
        units=units,
        resource_cells=resource_cells,
        obstacle_cells=obstacle_cells,
        enemy_cells=enemy_cells,
        enemy_units=enemy_units,
        core_id=core.id.value if core is not None else None,
        core_position=core.position if core is not None else None,
        core_health=core.health if core is not None else None,
        core_shield=core.shield if core is not None else None,
        core_state=core.state.value if core is not None else None,
        beacon=beacon_info,
        threat_map=build_threat_map(enemy_units),
    )
