"""Strict SDK 0.2.x turn-to-application adaptation."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from arena_hero_agent.application import PlayerLifecycle, TurnEvent, TurnObservation
from arena_hero_agent.domain import (
    CURRENT_RULES_VERSION,
    BeaconObservation,
    BeaconStatus,
    Coordinate,
    CoreObservation,
    CoreState,
    EntityId,
    EntityKind,
    EntityObservation,
    ResourceObservation,
    TerrainObservation,
    TerrainState,
    UnitObservation,
    UnitRole,
    WorldProjection,
)

from .bindings import load_sdk_bindings
from .errors import SdkContractViolationError


def _sdk() -> Any:
    load_sdk_bindings()
    return import_module("arena_hero")


def _violation(message: str) -> SdkContractViolationError:
    return SdkContractViolationError("adapt-turn", message)


def _exact(value: object, expected: type[Any], label: str) -> Any:
    if type(value) is not expected:
        raise _violation(f"expected exact SDK {label}, got {type(value).__name__}")
    return value


def _coordinate(value: object, label: str) -> Coordinate:
    if type(value) is not tuple or len(value) != 2:
        raise _violation(f"{label} must be an exact two-item tuple")
    x, y = value
    if (
        isinstance(x, bool)
        or not isinstance(x, int)
        or isinstance(y, bool)
        or not isinstance(y, int)
    ):
        raise _violation(f"{label} must contain only integers")
    try:
        return Coordinate(x, y)
    except (TypeError, ValueError) as exc:
        raise _violation(f"invalid {label}: {exc}") from exc


def _entity_id(value: object, label: str) -> EntityId:
    try:
        text = str(value)
        return EntityId(text)
    except (TypeError, ValueError) as exc:
        raise _violation(f"invalid {label}: {exc}") from exc


def _enum_name(value: object, expected: type[Any], mapping: dict[str, Any], label: str) -> Any:
    _exact(value, expected, label)
    name = getattr(value, "name", None)
    if not isinstance(name, str) or name not in mapping:
        raise _violation(f"unsupported SDK {label} member {name!r}")
    return mapping[name]


def adapt_async_turn(turn: object) -> TurnObservation:
    """Adapt one exact SDK ``AsyncTurn`` and fail closed on contract drift."""

    sdk = _sdk()
    _exact(turn, sdk.AsyncTurn, "AsyncTurn")
    state = _exact(getattr(turn, "state", None), sdk.PlayerState, "PlayerState")
    tick = getattr(turn, "tick", None)
    if isinstance(tick, bool) or not isinstance(tick, int) or tick < 1:
        raise _violation("turn tick must be a positive integer")

    lifecycle = _enum_name(
        state.status,
        sdk.PlayerStatus,
        {"ACTIVE": PlayerLifecycle.ACTIVE, "RESPAWNING": PlayerLifecycle.RESPAWNING},
        "PlayerStatus",
    )
    unit_roles = {
        "WORKER": UnitRole.WORKER,
        "VANGUARD": UnitRole.VANGUARD,
        "RANGER": UnitRole.RANGER,
    }
    core_states = {"NORMAL": CoreState.NORMAL, "MOVING": CoreState.MOVING}

    controlled_core: CoreObservation | None = None
    units: list[UnitObservation] = []
    entities: list[EntityObservation] = []
    resources: list[ResourceObservation] = []
    terrain: list[TerrainObservation] = []
    identities: set[str] = set()
    terrain_cells: set[str] = set()

    objects = state.objects
    if type(objects) is not tuple:
        raise _violation("PlayerState.objects must be an exact tuple")
    for obj in objects:
        if type(obj) is sdk.UnitView:
            identifier = _entity_id(obj.id, "unit id")
            if identifier.value in identities:
                raise _violation(f"duplicate object identity {identifier.value!r}")
            identities.add(identifier.value)
            role = _enum_name(obj.unit_type, sdk.UnitType, unit_roles, "UnitType")
            position = _coordinate(obj.position, "unit position")
            if type(obj.controlled) is not bool:
                raise _violation("unit controlled flag must be a bool")
            if obj.controlled:
                units.append(
                    UnitObservation(
                        id=identifier,
                        position=position,
                        role=role,
                        health=obj.hp,
                        cargo=0 if obj.cargo is None else obj.cargo,
                    )
                )
            else:
                entities.append(
                    EntityObservation(
                        id=identifier,
                        kind=EntityKind.UNIT,
                        position=position,
                        health=obj.hp,
                        unit_role=role,
                    )
                )
        elif type(obj) is sdk.CoreView:
            identifier = _entity_id(obj.id, "core id")
            if identifier.value in identities:
                raise _violation(f"duplicate object identity {identifier.value!r}")
            identities.add(identifier.value)
            state_value = _enum_name(obj.state, sdk.CoreState, core_states, "CoreState")
            position = _coordinate(obj.position, "core position")
            destination = (
                None
                if obj.destination is None
                else _coordinate(obj.destination, "core destination")
            )
            if type(obj.controlled) is not bool:
                raise _violation("core controlled flag must be a bool")
            if obj.controlled:
                if controlled_core is not None:
                    raise _violation("multiple controlled cores")
                controlled_core = CoreObservation(
                    id=identifier,
                    position=position,
                    health=obj.hp,
                    shield=obj.shield,
                    state=state_value,
                    owner=obj.owner_username,
                    destination=destination,
                )
            else:
                entities.append(
                    EntityObservation(
                        id=identifier,
                        kind=EntityKind.CORE,
                        position=position,
                        health=obj.hp,
                        owner=obj.owner_username,
                    )
                )
        elif type(obj) is sdk.TerrainView:
            if obj.kind not in {"OBSTACLE", "RESOURCE"}:
                raise _violation(f"unsupported terrain kind {obj.kind!r}")
            if type(obj.positions) is not tuple or not obj.positions:
                raise _violation("terrain positions must be a non-empty exact tuple")
            for raw_position in obj.positions:
                position = _coordinate(raw_position, "terrain position")
                if position.cell_key in terrain_cells:
                    raise _violation(f"duplicate terrain identity {position.cell_key!r}")
                terrain_cells.add(position.cell_key)
                if obj.kind == "OBSTACLE":
                    terrain.append(TerrainObservation(position, TerrainState.BLOCKED))
                else:
                    resources.append(ResourceObservation(position))
        else:
            raise _violation(f"unsupported PlayerState object {type(obj).__name__}")

    beacon_value = _exact(state.champion_beacon, sdk.ChampionBeacon, "ChampionBeacon")
    if beacon_value.status is None:
        beacon_status = BeaconStatus.UNKNOWN
    else:
        beacon_status = _enum_name(
            beacon_value.status,
            sdk.BeaconStatus,
            {"GROUND": BeaconStatus.GROUND, "CARRIED": BeaconStatus.CARRIED},
            "BeaconStatus",
        )
    carrier_id = (
        None
        if beacon_value.carrier_id is None
        else _entity_id(beacon_value.carrier_id, "beacon carrier id")
    )
    beacon = BeaconObservation(
        position=_coordinate(beacon_value.position, "beacon position"),
        status=beacon_status,
        carrier_id=carrier_id,
    )

    event_values = state.events
    if type(event_values) is not tuple:
        raise _violation("PlayerState.events must be an exact tuple")
    events: list[TurnEvent] = []
    for event in event_values:
        _exact(event, sdk.ResolutionEvent, "ResolutionEvent")
        if event.tick != tick:
            raise _violation("resolution event tick must match turn tick")
        events.append(
            TurnEvent(
                id=_entity_id(event.event_id, "event id"),
                tick=event.tick,
                kind=event.event_type,
                reason=event.reason_code,
                actor_id=None if event.actor_id is None else _entity_id(event.actor_id, "actor id"),
                target_id=(
                    None if event.target_id is None else _entity_id(event.target_id, "target id")
                ),
                position=(
                    None
                    if event.position is None
                    else _coordinate(event.position, "event position")
                ),
            )
        )

    try:
        projection = WorldProjection(
            tick=tick,
            rules_version=CURRENT_RULES_VERSION,
            core=controlled_core,
            units=tuple(units),
            entities=tuple(entities),
            resources=tuple(resources),
            terrain=tuple(terrain),
            beacon=beacon,
        )
        return TurnObservation(
            tick=tick,
            lifecycle=lifecycle,
            resources=state.resources,
            population=state.population,
            projection=projection,
            events=tuple(events),
            respawn_at_tick=state.respawn_at_tick,
        )
    except (TypeError, ValueError) as exc:
        raise _violation(f"malformed SDK turn: {exc}") from exc
