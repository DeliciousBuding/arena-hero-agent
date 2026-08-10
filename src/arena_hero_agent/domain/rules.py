"""Explicit Arena rules identity and pure phase classification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .value_objects import _require_int

_MAX_SAFE_INTEGER = 2**53 - 1


class RulesVersion(StrEnum):
    """Recognized historical rules identities."""

    __canonical_name__ = "arena-hero.rules-version.v1"

    V0_11 = "v0.11"
    V0_14 = "v0.14"


SUPPORTED_RULES_VERSIONS = (RulesVersion.V0_11, RulesVersion.V0_14)
CURRENT_RULES_VERSION = RulesVersion.V0_14


def parse_rules_version(value: object) -> RulesVersion:
    """Parse a recognized rules identity without inferring from data shape."""

    if not isinstance(value, str):
        raise TypeError("rules version must be a string")
    try:
        return RulesVersion(value)
    except ValueError as error:
        supported = ", ".join(version.value for version in SUPPORTED_RULES_VERSIONS)
        raise ValueError(
            f"unsupported rules version {value!r}; expected one of: {supported}"
        ) from error


def assert_current_rules_version(version: RulesVersion) -> None:
    """Fail closed when recognized historical rules are used as current rules."""

    if not isinstance(version, RulesVersion):
        raise TypeError("version must be a RulesVersion")
    if version is not CURRENT_RULES_VERSION:
        raise ValueError(
            f"rules version {version.value} is recognized but current is "
            f"{CURRENT_RULES_VERSION.value}"
        )


class GamePhase(StrEnum):
    """High-level game phase used by policy layers."""

    __canonical_name__ = "arena-hero.game-phase.v1"

    EARLY_EXPANSION = "early_expansion"
    BALANCED = "balanced"
    MILITARY = "military"


@dataclass(frozen=True, slots=True)
class PhaseConfig:
    """Immutable thresholds for pure phase transitions."""

    __canonical_name__ = "arena-hero.phase-config.v1"

    military_population: int = 18
    threat_enemies_near_core: int = 2
    balanced_population: int = 5
    balanced_resources: int = 20

    def __post_init__(self) -> None:
        values = {
            "military_population": self.military_population,
            "threat_enemies_near_core": self.threat_enemies_near_core,
            "balanced_population": self.balanced_population,
            "balanced_resources": self.balanced_resources,
        }
        for name, raw_value in values.items():
            value = _require_int(name, raw_value)
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
            if value > _MAX_SAFE_INTEGER:
                raise ValueError(f"{name} exceeds the cross-language safe-integer range")
        if self.balanced_population > self.military_population:
            raise ValueError("balanced_population cannot exceed military_population")


DEFAULT_PHASE_CONFIG = PhaseConfig()


@dataclass(frozen=True, slots=True)
class PhaseInputs:
    """Immutable observed quantities used to classify the next phase."""

    __canonical_name__ = "arena-hero.phase-inputs.v1"

    population: int
    resources: int
    enemies_near_core: int

    def __post_init__(self) -> None:
        values = {
            "population": self.population,
            "resources": self.resources,
            "enemies_near_core": self.enemies_near_core,
        }
        for name, raw_value in values.items():
            value = _require_int(name, raw_value)
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
            if value > _MAX_SAFE_INTEGER:
                raise ValueError(f"{name} exceeds the cross-language safe-integer range")


def transition_phase(
    current: GamePhase,
    inputs: PhaseInputs,
    *,
    config: PhaseConfig = DEFAULT_PHASE_CONFIG,
    forced: GamePhase | None = None,
) -> GamePhase:
    """Return the next phase without retaining or mutating machine state."""

    if not isinstance(current, GamePhase):
        raise TypeError("current must be a GamePhase")
    if not isinstance(inputs, PhaseInputs):
        raise TypeError("inputs must be PhaseInputs")
    if not isinstance(config, PhaseConfig):
        raise TypeError("config must be PhaseConfig")
    if forced is not None:
        if not isinstance(forced, GamePhase):
            raise TypeError("forced must be a GamePhase or None")
        return forced
    if (
        inputs.enemies_near_core >= config.threat_enemies_near_core
        or inputs.population >= config.military_population
    ):
        return GamePhase.MILITARY
    if (
        inputs.population >= config.balanced_population
        and inputs.resources >= config.balanced_resources
    ):
        return GamePhase.BALANCED
    return GamePhase.EARLY_EXPANSION
