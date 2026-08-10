"""Explicit mappings between domain values and SDK-owned wire enumerations."""

from __future__ import annotations

from typing import Any

from arena_hero_agent.domain import Direction

from .bindings import SdkBindings, load_sdk_bindings
from .errors import SdkContractViolationError

_DOMAIN_TO_SDK_NAME = {
    Direction.NORTH: "UP",
    Direction.EAST: "RIGHT",
    Direction.SOUTH: "DOWN",
    Direction.WEST: "LEFT",
}
_SDK_NAME_TO_DOMAIN = {sdk_name: domain for domain, sdk_name in _DOMAIN_TO_SDK_NAME.items()}


def to_sdk_direction(direction: Direction, *, bindings: SdkBindings | None = None) -> Any:
    """Map a domain direction to the SDK enum without copying the wire model."""

    if not isinstance(direction, Direction):
        raise SdkContractViolationError("map-direction", "expected a domain Direction")
    sdk = bindings or load_sdk_bindings()
    member_name = _DOMAIN_TO_SDK_NAME[direction]
    try:
        return sdk.direction_type[member_name]
    except (KeyError, TypeError) as exc:
        raise SdkContractViolationError(
            "map-direction", f"SDK Direction is missing member {member_name!r}"
        ) from exc


def from_sdk_direction(value: object, *, bindings: SdkBindings | None = None) -> Direction:
    """Map an SDK direction to the domain and reject non-public or future values."""

    sdk = bindings or load_sdk_bindings()
    if not isinstance(value, sdk.direction_type):
        raise SdkContractViolationError("map-direction", "expected an SDK Direction value")
    member_name = getattr(value, "name", None)
    if not isinstance(member_name, str):
        raise SdkContractViolationError("map-direction", "SDK Direction has no string name")
    try:
        return _SDK_NAME_TO_DOMAIN[member_name]
    except (KeyError, TypeError) as exc:
        raise SdkContractViolationError(
            "map-direction", f"unsupported SDK Direction member {member_name!r}"
        ) from exc
