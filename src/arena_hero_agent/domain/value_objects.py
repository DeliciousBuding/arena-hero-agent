"""Immutable value objects shared by deterministic application logic."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from .canonical import canonical_sha256

_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COORDINATE_MIN = -(2**31)
_COORDINATE_MAX = 2**31 - 1


def _require_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _validate_identifier(name: str, value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not pattern.fullmatch(value):
        raise ValueError(f"{name} is not in canonical form")
    return value


@dataclass(frozen=True, slots=True, order=True)
class TenantId:
    """Stable, lowercase tenant partition identifier."""

    __canonical_name__ = "arena-hero.tenant-id.v1"

    value: str

    def __post_init__(self) -> None:
        _validate_identifier("tenant id", self.value, _TENANT_ID)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class EntityId:
    """Stable opaque identifier for a domain entity."""

    __canonical_name__ = "arena-hero.entity-id.v1"

    value: str

    def __post_init__(self) -> None:
        _validate_identifier("entity id", self.value, _OPAQUE_ID)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class DecisionId:
    """Stable decision identifier that can be derived without wall-clock input."""

    __canonical_name__ = "arena-hero.decision-id.v1"

    value: str

    def __post_init__(self) -> None:
        _validate_identifier("decision id", self.value, _OPAQUE_ID)

    @classmethod
    def from_deterministic_input(cls, value: object) -> Self:
        """Build an identifier from deterministic state and sequence data."""

        return cls(f"decision:{canonical_sha256(value)}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class StateDigest:
    """Lowercase SHA-256 digest of a canonical state representation."""

    __canonical_name__ = "arena-hero.state-digest.v1"

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("state digest must be a string")
        if not _SHA256.fullmatch(self.value):
            raise ValueError("state digest must be 64 lowercase hexadecimal characters")

    @classmethod
    def from_state(cls, state: object) -> Self:
        return cls(canonical_sha256(state))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class Generation:
    """Monotonic revision number for tenant-owned state."""

    __canonical_name__ = "arena-hero.generation.v1"

    value: int

    def __post_init__(self) -> None:
        value = _require_int("generation", self.value)
        if value < 0:
            raise ValueError("generation cannot be negative")

    def next(self) -> Self:
        return type(self)(self.value + 1)

    def supersedes(self, other: Generation) -> bool:
        return self.value > other.value


@dataclass(frozen=True, slots=True, order=True)
class FencingToken:
    """Positive monotonic token carried by durable writer operations."""

    __canonical_name__ = "arena-hero.fencing-token.v1"

    value: int

    def __post_init__(self) -> None:
        value = _require_int("fencing token", self.value)
        if value < 1:
            raise ValueError("fencing token must be positive")

    def next(self) -> Self:
        return type(self)(self.value + 1)

    def supersedes(self, other: FencingToken) -> bool:
        return self.value > other.value


@dataclass(frozen=True, slots=True, order=True)
class DeadlineBudget:
    """Deterministic remaining execution budget measured in nanoseconds."""

    __canonical_name__ = "arena-hero.deadline-budget.v1"

    nanoseconds: int

    def __post_init__(self) -> None:
        value = _require_int("deadline budget", self.nanoseconds)
        if value < 0:
            raise ValueError("deadline budget cannot be negative")

    @classmethod
    def from_milliseconds(cls, milliseconds: int) -> Self:
        value = _require_int("milliseconds", milliseconds)
        if value < 0:
            raise ValueError("milliseconds cannot be negative")
        return cls(value * 1_000_000)

    @property
    def exhausted(self) -> bool:
        return self.nanoseconds == 0

    def consume(self, elapsed_nanoseconds: int) -> Self:
        elapsed = _require_int("elapsed nanoseconds", elapsed_nanoseconds)
        if elapsed < 0:
            raise ValueError("elapsed nanoseconds cannot be negative")
        return type(self)(max(0, self.nanoseconds - elapsed))


@dataclass(frozen=True, slots=True, order=True)
class Coordinate:
    """Signed 32-bit Cartesian coordinate with lexicographic ordering."""

    __canonical_name__ = "arena-hero.coordinate.v1"

    x: int
    y: int

    def __post_init__(self) -> None:
        x = _require_int("x coordinate", self.x)
        y = _require_int("y coordinate", self.y)
        if not _COORDINATE_MIN <= x <= _COORDINATE_MAX:
            raise ValueError("x coordinate is outside the signed 32-bit range")
        if not _COORDINATE_MIN <= y <= _COORDINATE_MAX:
            raise ValueError("y coordinate is outside the signed 32-bit range")

    def step(self, direction: Direction, distance: int = 1) -> Self:
        steps = _require_int("distance", distance)
        if steps < 0:
            raise ValueError("distance cannot be negative")
        dx, dy = direction.delta
        return type(self)(self.x + dx * steps, self.y + dy * steps)


class Direction(StrEnum):
    """Domain cardinal direction, independent from any wire enumeration."""

    __canonical_name__ = "arena-hero.direction.v1"

    NORTH = "north"
    EAST = "east"
    SOUTH = "south"
    WEST = "west"

    @property
    def delta(self) -> tuple[int, int]:
        return {
            Direction.NORTH: (0, -1),
            Direction.EAST: (1, 0),
            Direction.SOUTH: (0, 1),
            Direction.WEST: (-1, 0),
        }[self]

    @property
    def opposite(self) -> Direction:
        return {
            Direction.NORTH: Direction.SOUTH,
            Direction.EAST: Direction.WEST,
            Direction.SOUTH: Direction.NORTH,
            Direction.WEST: Direction.EAST,
        }[self]
