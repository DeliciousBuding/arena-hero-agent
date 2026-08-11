"""Canonical replay input decoding and an offline tick source.

P4-7 defines one stable offline input format for the ``run`` command: a JSON
document (a bare array, or an object with ``version`` and ``observations``
keys) or a JSON Lines file whose lines are canonical ``TurnObservation``
payloads. The canonical payload shape matches the application projection:
snake_case dataclass field names, string enum values, ``[x, y]`` coordinates,
and ``null`` for absent optional values.

Decoding is strict and fails closed: unknown keys, wrong types, and
unrecognized enum values raise :class:`ReplayError` instead of being silently
dropped or coerced.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import TypeVar

from arena_hero_agent.application.tick_loop import TurnStream
from arena_hero_agent.application.turns import (
    PlayerLifecycle,
    TurnEvent,
    TurnObservation,
)
from arena_hero_agent.domain import (
    BeaconObservation,
    BeaconStatus,
    Coordinate,
    CoreObservation,
    CoreState,
    EntityId,
    EntityKind,
    EntityObservation,
    ResourceObservation,
    RulesVersion,
    TerrainObservation,
    TerrainState,
    UnitObservation,
    UnitRole,
    WorldProjection,
)

REPLAY_FORMAT_VERSION = 1

_T = TypeVar("_T")


class ReplayError(Exception):
    """Replay input is malformed, unsupported, or fails closed on validation."""


def _mapping(payload: object, what: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ReplayError(f"{what} must be a JSON object")
    return payload


def _expect_only_keys(
    data: Mapping[str, object],
    allowed: set[str],
    what: str,
) -> None:
    unknown = sorted(key for key in data if key not in allowed)
    if unknown:
        raise ReplayError(f"{what} has unknown keys: {', '.join(unknown)}")


def _require(data: Mapping[str, object], key: str, what: str) -> object:
    if key not in data:
        raise ReplayError(f"{what} is missing required key {key!r}")
    return data[key]


def _require_str(data: Mapping[str, object], key: str, what: str) -> str:
    value = _require(data, key, what)
    if not isinstance(value, str):
        raise ReplayError(f"{what} key {key!r} must be a string")
    return value


def _optional_str(data: Mapping[str, object], key: str, what: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReplayError(f"{what} key {key!r} must be a string or null")
    return value


def _require_int(data: Mapping[str, object], key: str, what: str) -> int:
    value = _require(data, key, what)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayError(f"{what} key {key!r} must be an integer")
    return value


def _optional_int(data: Mapping[str, object], key: str, what: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayError(f"{what} key {key!r} must be an integer or null")
    return value


def _coordinate(value: object, what: str) -> Coordinate:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise ReplayError(f"{what} must be a two-integer coordinate array")
    return Coordinate(value[0], value[1])


def _optional_coordinate(
    data: Mapping[str, object],
    key: str,
    what: str,
) -> Coordinate | None:
    value = data.get(key)
    if value is None:
        return None
    return _coordinate(value, f"{what} key {key!r}")


def _entity(value: object, what: str) -> EntityId:
    if not isinstance(value, str):
        raise ReplayError(f"{what} must be a string")
    try:
        return EntityId(value)
    except (TypeError, ValueError) as exc:
        raise ReplayError(f"{what} is not a valid entity id") from exc


def _optional_entity(
    data: Mapping[str, object],
    key: str,
    what: str,
) -> EntityId | None:
    value = data.get(key)
    if value is None:
        return None
    return _entity(value, f"{what} key {key!r}")


def _enum(enum_type: Callable[[str], _T], value: object, what: str) -> _T:
    if not isinstance(value, str):
        raise ReplayError(f"{what} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ReplayError(f"{what} has an unknown value") from exc


def _optional_enum(
    enum_type: Callable[[str], _T],
    data: Mapping[str, object],
    key: str,
    what: str,
) -> _T | None:
    value = data.get(key)
    if value is None:
        return None
    return _enum(enum_type, value, f"{what} key {key!r}")


def _list_of(
    data: Mapping[str, object],
    key: str,
    what: str,
    decode: Callable[[object], _T],
) -> tuple[_T, ...]:
    value = _require(data, key, what)
    if not isinstance(value, list):
        raise ReplayError(f"{what} key {key!r} must be an array")
    return tuple(decode(item) for item in value)


def decode_event(payload: object) -> TurnEvent:
    data = _mapping(payload, "turn event")
    _expect_only_keys(
        data,
        {"actor_id", "id", "kind", "position", "reason", "target_id", "tick"},
        "turn event",
    )
    return TurnEvent(
        id=_entity(_require(data, "id", "turn event"), "event id"),
        tick=_require_int(data, "tick", "turn event"),
        kind=_require_str(data, "kind", "turn event"),
        reason=_optional_str(data, "reason", "turn event"),
        actor_id=_optional_entity(data, "actor_id", "turn event"),
        target_id=_optional_entity(data, "target_id", "turn event"),
        position=_optional_coordinate(data, "position", "turn event"),
    )


def decode_unit(payload: object) -> UnitObservation:
    data = _mapping(payload, "unit observation")
    _expect_only_keys(data, {"cargo", "health", "id", "position", "role"}, "unit observation")
    return UnitObservation(
        id=_entity(_require(data, "id", "unit observation"), "unit id"),
        position=_coordinate(_require(data, "position", "unit observation"), "unit position"),
        role=_enum(UnitRole, _require_str(data, "role", "unit observation"), "unit role"),
        health=_require_int(data, "health", "unit observation"),
        cargo=_require_int(data, "cargo", "unit observation"),
    )


def decode_entity(payload: object) -> EntityObservation:
    data = _mapping(payload, "entity observation")
    _expect_only_keys(
        data,
        {"health", "id", "kind", "owner", "position", "unit_role"},
        "entity observation",
    )
    return EntityObservation(
        id=_entity(_require(data, "id", "entity observation"), "entity id"),
        kind=_enum(EntityKind, _require_str(data, "kind", "entity observation"), "entity kind"),
        position=_coordinate(_require(data, "position", "entity observation"), "entity position"),
        health=_require_int(data, "health", "entity observation"),
        owner=_optional_str(data, "owner", "entity observation"),
        unit_role=_optional_enum(UnitRole, data, "unit_role", "entity observation"),
    )


def decode_core(payload: object) -> CoreObservation:
    data = _mapping(payload, "core observation")
    _expect_only_keys(
        data,
        {"destination", "health", "id", "owner", "position", "shield", "state"},
        "core observation",
    )
    return CoreObservation(
        id=_entity(_require(data, "id", "core observation"), "core id"),
        position=_coordinate(_require(data, "position", "core observation"), "core position"),
        state=_enum(CoreState, _require_str(data, "state", "core observation"), "core state"),
        health=_require_int(data, "health", "core observation"),
        shield=_require_int(data, "shield", "core observation"),
        owner=_require_str(data, "owner", "core observation"),
        destination=_optional_coordinate(data, "destination", "core observation"),
    )


def decode_resource(payload: object) -> ResourceObservation:
    data = _mapping(payload, "resource observation")
    _expect_only_keys(data, {"position", "remaining"}, "resource observation")
    return ResourceObservation(
        position=_coordinate(
            _require(data, "position", "resource observation"), "resource position"
        ),
        remaining=_optional_int(data, "remaining", "resource observation"),
    )


def decode_terrain(payload: object) -> TerrainObservation:
    data = _mapping(payload, "terrain observation")
    _expect_only_keys(data, {"position", "state"}, "terrain observation")
    return TerrainObservation(
        position=_coordinate(_require(data, "position", "terrain observation"), "terrain position"),
        state=_enum(
            TerrainState, _require_str(data, "state", "terrain observation"), "terrain state"
        ),
    )


def decode_beacon(payload: object) -> BeaconObservation:
    data = _mapping(payload, "beacon observation")
    _expect_only_keys(data, {"carrier_id", "position", "status"}, "beacon observation")
    return BeaconObservation(
        position=_coordinate(_require(data, "position", "beacon observation"), "beacon position"),
        status=_enum(
            BeaconStatus, _require_str(data, "status", "beacon observation"), "beacon status"
        ),
        carrier_id=_optional_entity(data, "carrier_id", "beacon observation"),
    )


def decode_projection(payload: object) -> WorldProjection:
    data = _mapping(payload, "world projection")
    _expect_only_keys(
        data,
        {"beacon", "core", "entities", "resources", "rules_version", "terrain", "tick", "units"},
        "world projection",
    )
    core_value = data.get("core")
    core = None if core_value is None else decode_core(core_value)
    beacon_value = data.get("beacon")
    beacon = None if beacon_value is None else decode_beacon(beacon_value)
    return WorldProjection(
        tick=_require_int(data, "tick", "world projection"),
        rules_version=_enum(
            RulesVersion,
            _require_str(data, "rules_version", "world projection"),
            "rules version",
        ),
        core=core,
        units=_list_of(data, "units", "world projection", decode_unit),
        entities=_list_of(data, "entities", "world projection", decode_entity),
        resources=_list_of(data, "resources", "world projection", decode_resource),
        terrain=_list_of(data, "terrain", "world projection", decode_terrain),
        beacon=beacon,
    )


def decode_observation(payload: object) -> TurnObservation:
    data = _mapping(payload, "turn observation")
    _expect_only_keys(
        data,
        {"events", "lifecycle", "population", "projection", "respawn_at_tick", "resources", "tick"},
        "turn observation",
    )
    events = _list_of(data, "events", "turn observation", decode_event)
    return TurnObservation(
        tick=_require_int(data, "tick", "turn observation"),
        lifecycle=_enum(
            PlayerLifecycle,
            _require_str(data, "lifecycle", "turn observation"),
            "turn lifecycle",
        ),
        resources=_require_int(data, "resources", "turn observation"),
        population=_require_int(data, "population", "turn observation"),
        projection=decode_projection(_require(data, "projection", "turn observation")),
        events=events,
        respawn_at_tick=_optional_int(data, "respawn_at_tick", "turn observation"),
    )


def load_observations(path: str | os.PathLike[str]) -> tuple[TurnObservation, ...]:
    """Load and decode a replay file into immutable turn observations."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise ReplayError("replay input could not be read") from exc
    if not text.strip():
        raise ReplayError("replay input is empty")
    if source.suffix.lower() == ".jsonl":
        return _load_jsonl(text)
    return _load_json(text)


def _load_jsonl(text: str) -> tuple[TurnObservation, ...]:
    observations: list[TurnObservation] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ReplayError(f"replay input line {line_no} is not valid JSON") from exc
        observations.append(decode_observation(payload))
    if not observations:
        raise ReplayError("replay input contains no observations")
    return tuple(observations)


def _load_json(text: str) -> tuple[TurnObservation, ...]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReplayError("replay input is not valid JSON") from exc
    if isinstance(document, list):
        observations = tuple(decode_observation(item) for item in document)
        if not observations:
            raise ReplayError("replay input contains no observations")
        return observations
    data = _mapping(document, "replay document")
    _expect_only_keys(data, {"observations", "version"}, "replay document")
    version = data.get("version")
    if version is not None and version != REPLAY_FORMAT_VERSION:
        raise ReplayError("replay document version is not supported")
    observations_value = _require(data, "observations", "replay document")
    if not isinstance(observations_value, list):
        raise ReplayError("replay document observations must be an array")
    observations = tuple(decode_observation(item) for item in observations_value)
    if not observations:
        raise ReplayError("replay document contains no observations")
    return observations


class ReplayTickSource:
    """Offline, reopenable tick source over in-memory observations."""

    def __init__(self, observations: Sequence[TurnObservation]) -> None:
        self._observations = tuple(observations)
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def stream(self) -> TurnStream:
        if self._closed:
            raise ReplayError("replay source is closed")
        return _ReplayTurnStream(self._observations)

    def close(self) -> None:
        self._closed = True


class _ReplayTurnStream:
    def __init__(self, observations: tuple[TurnObservation, ...]) -> None:
        self._observations = observations
        self._index = 0

    def __aiter__(self) -> AsyncIterator[TurnObservation]:
        return self

    async def __anext__(self) -> TurnObservation:
        if self._index >= len(self._observations):
            raise StopAsyncIteration
        observation = self._observations[self._index]
        self._index += 1
        return observation

    async def aclose(self) -> None:
        return None


__all__ = [
    "REPLAY_FORMAT_VERSION",
    "ReplayError",
    "ReplayTickSource",
    "decode_observation",
    "load_observations",
]
